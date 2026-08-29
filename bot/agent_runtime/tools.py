"""Real tool execution for the agent-loop engine's tool-use turns — shell
commands, file read/write, directory listing, and read-only git
inspection. Every call is sandboxed to one working directory (the
session's `cwd`, either an explicit /project path or a per-instance
default workspace created on demand) — relative paths that try to escape
it via `..` are rejected, and absolute paths outside it are rejected too.
That boundary applies uniformly whether or not the workspace itself was
explicitly chosen by the bot's operator, since "don't let the model read
or write outside the one directory it's supposed to be working in" is a
sane rule either way.

`run_shell` is intentionally unrestricted *within* that directory (same
shape as Claude Code's own Bash tool) — it's gated by the approval flow
in approval.py, not by a command allowlist here, since a fixed
allowlist would break normal agentic work (git, package managers,
compilers) for no real safety gain once a human has already approved it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

WORKSPACES_ROOT = PROJECT_ROOT / "data" / "agent_workspaces"

SHELL_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 8000
MAX_READ_CHARS = 20000

# Tools that can change state or run arbitrary code — gated behind
# approval.py. Everything else (reads) runs immediately.
DANGEROUS_TOOLS = {"run_shell", "write_file"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "run_shell",
        "description": "Run a shell command in the session's working directory. Requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to run."}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file's contents, relative to the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (overwrite) a text file, relative to the working directory. Creates parent directories as needed. Requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and subdirectories at a path relative to the working directory (default: the working directory itself).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Defaults to '.'"}},
        },
    },
    {
        "name": "git_status",
        "description": "Show `git status --short` for the working directory.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "git_diff",
        "description": "Show `git diff` (unstaged changes) for the working directory.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_memory",
        "description": (
            "Remember a fact for future sessions with this chat — persists across /new. "
            "May require human approval before it takes effect; the tool result says which."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "The fact to remember, in plain English."}},
            "required": ["content"],
        },
    },
    {
        "name": "read_skill",
        "description": "Load the full content of one of your available skills by name (see the system prompt's skill list).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
]


TOOL_SCHEMA_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS)


def all_tool_schemas() -> list[dict[str, Any]]:
    """Built-in tool schemas plus every tool a loaded plugin has
    registered (bot/plugins.py) — what a backend should actually offer
    the model this turn."""
    from bot import plugins as plugin_registry

    return TOOL_SCHEMAS + plugin_registry.tool_schemas()


def is_dangerous(name: str) -> bool:
    from bot import plugins as plugin_registry

    return name in DANGEROUS_TOOLS or plugin_registry.is_dangerous_tool(name)


class ToolError(Exception):
    pass


def resolve_workspace(instance_id: int, cwd_override: Optional[str]) -> Path:
    if cwd_override:
        path = Path(cwd_override).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = (WORKSPACES_ROOT / str(instance_id)).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_path(workspace: Path, rel_path: str) -> Path:
    candidate = (workspace / rel_path).resolve() if not Path(rel_path).is_absolute() else Path(rel_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise ToolError(f"path {rel_path!r} is outside the working directory ({workspace})")
    return candidate


async def _run_subprocess(args: list[str], cwd: Path) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            " ".join(args) if len(args) > 1 else args[0],
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"timed out after {SHELL_TIMEOUT_S}s")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    text = out.decode(errors="replace")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n… truncated ({len(text)} chars total)"
    return f"{text}\n[exit code {proc.returncode}]"


async def execute_tool(name: str, tool_input: dict, *, workspace: Path, instance_id: Optional[int] = None) -> str:
    if name == "run_shell":
        command = tool_input.get("command") or ""
        if not command.strip():
            raise ToolError("command can't be empty")
        return await _run_subprocess([command], workspace)

    if name == "read_file":
        path = _safe_path(workspace, tool_input.get("path") or "")
        if not path.exists():
            raise ToolError(f"{tool_input.get('path')!r} does not exist")
        if not path.is_file():
            raise ToolError(f"{tool_input.get('path')!r} is not a file")
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n… truncated ({len(text)} chars total)"
        return text

    if name == "write_file":
        path = _safe_path(workspace, tool_input.get("path") or "")
        content = tool_input.get("content", "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {tool_input.get('path')}"

    if name == "list_dir":
        path = _safe_path(workspace, tool_input.get("path") or ".")
        if not path.exists():
            raise ToolError(f"{tool_input.get('path', '.')!r} does not exist")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    if name == "git_status":
        return await _run_subprocess(["git", "status", "--short"], workspace)

    if name == "git_diff":
        return await _run_subprocess(["git", "diff"], workspace)

    if name == "save_memory":
        from bot import memory as bot_memory

        content = (tool_input.get("content") or "").strip()
        if not content:
            raise ToolError("content can't be empty")
        if instance_id is None:
            raise ToolError("save_memory needs an instance context")
        entry_id, approved = bot_memory.remember(instance_id, content, source="tool")
        return (
            f"Saved as memory #{entry_id} (approved, active now)."
            if approved
            else f"Saved as memory #{entry_id}, pending human approval (/memory approve {entry_id})."
        )

    if name == "read_skill":
        from bot import skills as bot_skills

        skill_name = (tool_input.get("name") or "").strip()
        if instance_id is None:
            raise ToolError("read_skill needs an instance context")
        content = bot_skills.get_content(instance_id, skill_name)
        if content is None:
            raise ToolError(f"no skill named {skill_name!r} — see the system prompt's skill list")
        return content

    from bot import plugins as plugin_registry

    if plugin_registry.has_tool(name):
        try:
            return await plugin_registry.execute_tool(name, tool_input, workspace=workspace, instance_id=instance_id)
        except KeyError:
            pass

    raise ToolError(f"unknown tool {name!r}")
