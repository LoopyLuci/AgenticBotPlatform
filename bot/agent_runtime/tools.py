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
import contextvars
import json as _json_module
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT


def _json_dumps(value: Any) -> str:
    return _json_module.dumps(value)

# Ambient recursion depth for delegate_to_instance — a ContextVar rather
# than a parameter threaded through execute_tool()'s signature, since a
# nested router.ask() call from inside a tool call runs as a direct
# `await` within the same task (not a separate one), so a ContextVar set
# for the duration of that nested call is naturally visible to it and
# nowhere else. Mirrors Hermes's own delegate_task using contextvars for
# the same "how deep are we already" question (see the Hermes-swarm
# plan's Phase 4 research).
_delegation_depth: contextvars.ContextVar[int] = contextvars.ContextVar("delegation_depth", default=0)

WORKSPACES_ROOT = PROJECT_ROOT / "data" / "agent_workspaces"

SHELL_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 8000
MAX_READ_CHARS = 20000

# Tools that can change state or run arbitrary code — gated behind
# approval.py. Everything else (reads) runs immediately. update_agent_config
# is included because it can change ANOTHER instance's identity/behavior/
# can_target list — a compromised or misbehaving agent using it to
# re-route its own or another instance's permissions is exactly the kind
# of consequential change write_file already requires a human for.
DANGEROUS_TOOLS = {
    "run_shell", "write_file", "update_agent_config",
    # Per the user's explicit choice: agent-authored plugin code is
    # trusted, unsandboxed, full-process-privilege Python (see
    # docs/adr/0007-plugins-are-trusted-local-code.md) — the same trust
    # boundary run_shell already accepts. Creating one writes and
    # immediately activates new code; re-enabling a previously-disabled
    # one reactivates existing code. Both go through the same human
    # approval gate as run_shell/write_file, never auto-installed.
    "create_plugin", "enable_plugin",
}

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
    {
        "name": "install_skill",
        "description": (
            "Register a file already in your own working directory as a new skill you can load later with "
            "read_skill — its first line becomes the one-line description shown in your own skill list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path within your working directory to the skill file."}},
            "required": ["path"],
        },
    },
    {
        "name": "list_skills",
        "description": "List every skill currently available to you (name + one-line description).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_skill",
        "description": (
            "Author a brand-new skill directly from text — no file needed first. Use this to save something "
            "you just learned, or a set of instructions the user asked you to remember for future use with "
            "read_skill. Skills are plain descriptive text, never executed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short identifier — letters, digits, _ or - only."},
                "description": {"type": "string", "description": "One-line summary shown in skill listings."},
                "content": {"type": "string", "description": "The full skill text read_skill will return."},
                "global_": {
                    "type": "boolean",
                    "description": "Make this visible to every bot instance, not just yours. Requires manager persona.",
                },
            },
            "required": ["name", "content"],
        },
    },
    {
        "name": "remove_skill",
        "description": "Delete one of your own skills by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "create_plugin",
        "description": (
            "Author a brand-new callable TOOL by writing real Python code — for capabilities plain instructions "
            "or a skill can't provide. This is trusted, unsandboxed, full-privilege code (the same trust level "
            "as run_shell) and REQUIRES human approval before it can run. The file must define a module-level "
            "setup(api) function calling api.register_tool(name, description, input_schema, handler) and/or "
            "api.register_command(...) — see an existing plugin under data/plugins/ for the exact shape."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Plugin name — lowercase letters/digits/underscore, starting with a letter."},
                "description": {"type": "string", "description": "One-line summary of what this plugin adds."},
                "code": {"type": "string", "description": "The full plugin.py source code."},
            },
            "required": ["name", "code"],
        },
    },
    {
        "name": "enable_plugin",
        "description": "Re-activate a previously-disabled plugin's tools/commands. Requires human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "disable_plugin",
        "description": "Deactivate a plugin's tools/commands without deleting it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "remove_plugin",
        "description": "Permanently delete a plugin.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_plugins",
        "description": "List every installed plugin (name, description, enabled state, tools/commands it registers).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "kanban_add_card",
        "description": "Add a card to one of your kanban boards (created automatically the first time it's named).",
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string", "description": "Board name — e.g. 'default'."},
                "column": {"type": "string", "description": "Column name — e.g. 'todo', 'doing', 'done'."},
                "text": {"type": "string", "description": "The card's text."},
            },
            "required": ["board", "column", "text"],
        },
    },
    {
        "name": "kanban_list_cards",
        "description": "List every card on one of your kanban boards, grouped by column.",
        "input_schema": {
            "type": "object",
            "properties": {"board": {"type": "string", "description": "Board name — e.g. 'default'."}},
            "required": ["board"],
        },
    },
    {
        "name": "kanban_move_card",
        "description": "Move one of your kanban cards to a different column.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "integer"},
                "column": {"type": "string", "description": "The column to move it to."},
            },
            "required": ["card_id", "column"],
        },
    },
    {
        "name": "schedule_command",
        "description": (
            "Schedule a prompt to run for you repeatedly, like /cron — e.g. a recurring status check or "
            "reminder. Runs through the exact same agent loop (same tools, same approval gating) as a "
            "manually-typed message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "A short label for what this is, e.g. 'status_check'."},
                "prompt": {"type": "string", "description": "The prompt to run on each firing."},
                "interval": {"type": "string", "description": "e.g. '30s', '10m', '2h', '1d', or a bare number of seconds."},
                "chat_id": {"type": "string", "description": "Which chat to post results into. Omit for instance-wide (no specific chat)."},
                "thread_id": {"type": "string", "description": "Optional thread/topic id within that chat."},
                "max_runs": {"type": "integer", "description": "Optional cap on how many times this fires before it stops itself."},
            },
            "required": ["kind", "prompt", "interval"],
        },
    },
    {
        "name": "list_schedules",
        "description": "List your own scheduled commands (from schedule_command or /cron).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_schedule",
        "description": "Pause one of your scheduled commands without deleting it.",
        "input_schema": {
            "type": "object",
            "properties": {"schedule_id": {"type": "integer"}},
            "required": ["schedule_id"],
        },
    },
    {
        "name": "remove_schedule",
        "description": "Permanently remove one of your scheduled commands.",
        "input_schema": {
            "type": "object",
            "properties": {"schedule_id": {"type": "integer"}},
            "required": ["schedule_id"],
        },
    },
    {
        "name": "delegate_to_instance",
        "description": (
            "Ask another registered bot instance (Claude- or Hermes-backed — any backend) a question and "
            "wait for its reply, the same 'manager delegates to a worker' pattern Hermes's own delegate_task "
            "gives Hermes agents. Only instances your own can_target list allows are reachable when the "
            "dashboard's agent_control mode is 'allowlist'; under the default 'trust_all' mode any instance "
            "is reachable. Bounded by agent_runtime.max_delegation_depth to prevent an accidental delegation "
            "cycle between two instances that target each other."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_instance": {"type": "string", "description": "Target bot_instances.id (or exact name)."},
                "prompt": {"type": "string", "description": "The question/task to send it."},
            },
            "required": ["target_instance", "prompt"],
        },
    },
    {
        "name": "spawn_subagent",
        "description": (
            "Spawn one or more disposable, ephemeral sub-agents to work on independent subtasks in parallel — "
            "the same 'decompose and delegate' pattern Hermes Agent's own delegate_task tool gives Hermes "
            "agents. Unlike delegate_to_instance (which addresses ONE specific, persistent registered bot "
            "instance), these workers are throwaway: no bot_instances row is created for them, and their "
            "history isn't kept beyond this call. role='leaf' (default) workers can't spawn further "
            "sub-agents, reconfigure other instances, save memory, or write shared project context; "
            "role='orchestrator' keeps those abilities, bounded by agent_runtime.max_delegation_depth. "
            "Pass provider+model together to run children on a specific (e.g. free) model; omit both to "
            "inherit your own backend/model. By default this call blocks until every child finishes and "
            "returns {dispatch_id, children: [{index, goal, model, status: 'ok'|'error', result_excerpt}]}. "
            "Pass background=true to return immediately instead — {dispatch_id, children: [{index, goal}]} "
            "— so you can keep working in this same turn while they run; check on them with list_subagents, "
            "nudge one with steer_subagent, or cancel one with stop_subagent, using the dispatch_id this call "
            "returns. Either way, the dispatch_id stays valid afterward for list_subagents to look results up "
            "again later, including from a later message if this turn ends first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "One entry per subtask — all run in parallel, bounded by max_children.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "The subtask's own goal/prompt."},
                            "output_schema": {
                                "type": "object",
                                "description": "Optional JSON Schema the child's final answer must match "
                                "(validated, with one bounded retry on failure).",
                            },
                        },
                        "required": ["goal"],
                    },
                },
                "role": {"type": "string", "enum": ["leaf", "orchestrator"], "description": "Defaults to 'leaf'."},
                "provider": {"type": "string", "description": "Named provider from config/providers.yaml. Give together with model, or omit both."},
                "model": {"type": "string", "description": "Model id at that provider. Give together with provider, or omit both to inherit your own."},
                "max_children": {"type": "integer", "description": "Caps parallelism for this call, further capped by native_agent.max_concurrent_children."},
                "background": {"type": "boolean", "description": "Return immediately with a dispatch_id instead of waiting for every child to finish. Defaults to false."},
            },
            "required": ["tasks"],
        },
    },
    {
        "name": "list_subagents",
        "description": (
            "Check on spawn_subagent children — omit dispatch_id to list every dispatch you currently have "
            "(useful after starting one or more background dispatches), or pass one dispatch_id for that "
            "batch's live per-child status ('running'/'ok'/'error'/'stopped') and results so far. Works for "
            "both background and already-finished blocking dispatches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dispatch_id": {"type": "string", "description": "A dispatch_id returned by an earlier spawn_subagent call. Omit to list all of yours."},
            },
        },
    },
    {
        "name": "steer_subagent",
        "description": (
            "Send a mid-turn nudge to one still-running spawn_subagent child — the same idea as the /steer "
            "command a human can send you, given to a child you spawned. Delivered before that child's next "
            "tool call; has no effect on a child that has already finished (use list_subagents to check first "
            "if unsure)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dispatch_id": {"type": "string", "description": "The dispatch this child belongs to."},
                "child_index": {"type": "integer", "description": "Which child within that dispatch (its 'index' from spawn_subagent/list_subagents)."},
                "message": {"type": "string", "description": "The nudge to send."},
            },
            "required": ["dispatch_id", "child_index", "message"],
        },
    },
    {
        "name": "stop_subagent",
        "description": (
            "Cancel one still-running spawn_subagent child before it finishes on its own — its ephemeral "
            "session is marked 'stopped', distinct from a natural 'error'. Has no effect on a child that has "
            "already finished."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dispatch_id": {"type": "string", "description": "The dispatch this child belongs to."},
                "child_index": {"type": "integer", "description": "Which child within that dispatch (its 'index' from spawn_subagent/list_subagents)."},
            },
            "required": ["dispatch_id", "child_index"],
        },
    },
    {
        "name": "consult_models",
        "description": (
            "Get a second (or third, etc.) opinion by asking multiple models the same question in "
            "parallel — mixture-of-agents consensus, the same capability Hermes Agent's own MoA mode "
            "gives it, as an on-demand tool instead of an always-on mode. Pass 2+ references to ask; "
            "omit aggregator to see every reference's raw labeled answer yourself, or give one to have "
            "it synthesize them into a single final answer. Omit a reference's provider for the built-in "
            "Claude API; otherwise name a provider from config/providers.yaml. One reference failing "
            "doesn't fail the whole call — its own labeled block just notes the failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "references": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "provider": {"type": "string", "description": "Omit for the built-in Claude API."},
                            "model": {"type": "string"},
                        },
                        "required": ["model"],
                    },
                },
                "aggregator": {
                    "type": "object",
                    "description": "Optional — if given, one more call synthesizes every reference's answer into one.",
                    "properties": {
                        "provider": {"type": "string"},
                        "model": {"type": "string"},
                    },
                    "required": ["model"],
                },
            },
            "required": ["question", "references"],
        },
    },
    {
        "name": "get_my_profile",
        "description": (
            "Your own identity as a small markdown document: name, backend, persona, model override, "
            "which other instances you can target, and your own custom instructions. Call this whenever "
            "you need to state your own bot_instances name (e.g. as source_instance for delegate_to_instance "
            "or the botserver MCP tools) rather than guessing it."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_agent_config",
        "description": (
            "Update another bot instance's name, custom instructions, persona, model, or can_target list "
            "on the fly — the lever a manager needs to (re)configure the workers it runs, without going "
            "through the dashboard. Only instances your can_target list allows are editable when agent_control "
            "is in 'allowlist' mode (same gate delegate_to_instance uses); under 'trust_all' any instance is "
            "editable. Only the fields you pass are changed — omit anything you don't want to touch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_instance": {"type": "string", "description": "Target bot_instances.id (or exact name)."},
                "name": {"type": "string", "description": "New display name."},
                "custom_instructions": {"type": "string", "description": "Replaces the target's entire custom_instructions text."},
                "persona": {"type": "string", "description": "One of bot/personas.py's presets."},
                "model": {"type": "string", "description": "Per-instance model override (backend-specific format)."},
                "can_target": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "Replaces the target's own can_target list (which OTHER instances it, in turn, may reach).",
                },
            },
            "required": ["target_instance"],
        },
    },
    {
        "name": "read_project_context",
        "description": (
            "Read a shared, cross-instance markdown document — the way a swarm of workers and their manager "
            "keep something like project status or shared notes in sync, independent of any one instance's "
            "own conversation history. Every registered instance can read every doc. Call list_project_context "
            "first if you don't know what docs exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Doc name, e.g. 'status'."}},
            "required": ["name"],
        },
    },
    {
        "name": "write_project_context",
        "description": (
            "Create or replace a shared, cross-instance markdown document (see read_project_context) — "
            "e.g. post a project-status update every worker and the manager can read. Keep it concise "
            "(a few KB max) — this is meant to be read on every turn cheaply, not as a general file store."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Doc name, e.g. 'status'."},
                "content": {"type": "string", "description": "Full markdown content — replaces whatever was there."},
            },
            "required": ["name", "content"],
        },
    },
    {
        "name": "list_project_context",
        "description": "List every shared context doc's name, size, and when/who last updated it.",
        "input_schema": {"type": "object", "properties": {}},
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

    if name == "install_skill":
        from bot import skills as bot_skills

        if instance_id is None:
            raise ToolError("install_skill needs an instance context")
        rel_path = tool_input.get("path") or ""
        # Same workspace sandbox read_file/write_file enforce — without
        # this, install_skill would be a path outside the intended
        # boundary: read any file on disk into a "skill" here, then
        # read_skill it back out, regardless of the workspace sandbox
        # every other file-reading tool respects.
        safe_path = _safe_path(workspace, rel_path)
        try:
            return _json_dumps(bot_skills.install(instance_id, str(safe_path)))
        except bot_skills.SkillError as exc:
            raise ToolError(str(exc))

    if name == "list_skills":
        from bot import skills as bot_skills

        if instance_id is None:
            raise ToolError("list_skills needs an instance context")
        return _json_dumps(bot_skills.list_for_instance(instance_id))

    if name == "create_skill":
        from bot import bot_instances, skills as bot_skills

        if instance_id is None:
            raise ToolError("create_skill needs an instance context")
        global_ = bool(tool_input.get("global_"))
        if global_:
            # A global skill is visible to every instance at once — there's
            # no single "target" for agent_control.can_target's per-instance
            # allowlist to check, so this reuses the same "manager" trust
            # convention persona=manager already carries elsewhere rather
            # than inventing a new wildcard allowlist concept.
            caller = bot_instances.get_instance(instance_id)
            if not caller or caller.get("persona") != "manager":
                raise ToolError("creating a global skill requires a manager-persona instance")
        try:
            result = bot_skills.create(
                instance_id,
                tool_input.get("name") or "",
                tool_input.get("description") or "",
                tool_input.get("content") or "",
                global_=global_,
            )
        except bot_skills.SkillError as exc:
            raise ToolError(str(exc))
        return _json_dumps(result)

    if name == "remove_skill":
        from bot import skills as bot_skills

        if instance_id is None:
            raise ToolError("remove_skill needs an instance context")
        skill_name = (tool_input.get("name") or "").strip()
        removed = bot_skills.remove(instance_id, skill_name)
        return f"Removed skill {skill_name!r}." if removed else f"No skill named {skill_name!r} found."

    if name == "create_plugin":
        from bot.envfile import PROJECT_ROOT
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        code = tool_input.get("code") or ""
        if not plugin_name or not code.strip():
            raise ToolError("name and code are required")
        # plugins.install() derives its registered name from the FILE's
        # own stem (Path(path).stem), not its parent directory — the file
        # must be named <name>.py, not a fixed "plugin.py", or every
        # agent-authored plugin would register as the same literal name
        # "plugin" regardless of what was actually requested.
        plugin_dir = PROJECT_ROOT / "data" / "plugins" / plugin_name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        plugin_path = plugin_dir / f"{plugin_name}.py"
        plugin_path.write_text(code, encoding="utf-8")
        try:
            info = plugin_registry.install(str(plugin_path))
        except plugin_registry.PluginError as exc:
            raise ToolError(str(exc))
        return _json_dumps(info)

    if name == "enable_plugin":
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        try:
            return _json_dumps(plugin_registry.enable(plugin_name))
        except plugin_registry.PluginError as exc:
            raise ToolError(str(exc))

    if name == "disable_plugin":
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        try:
            return _json_dumps(plugin_registry.disable(plugin_name))
        except plugin_registry.PluginError as exc:
            raise ToolError(str(exc))

    if name == "remove_plugin":
        from bot import plugins as plugin_registry

        plugin_name = (tool_input.get("name") or "").strip()
        removed = plugin_registry.remove(plugin_name)
        return f"Removed plugin {plugin_name!r}." if removed else f"No plugin named {plugin_name!r} found."

    if name == "list_plugins":
        from bot import plugins as plugin_registry

        return _json_dumps(plugin_registry.list_plugins())

    if name == "kanban_add_card":
        from bot import kanban

        if instance_id is None:
            raise ToolError("kanban_add_card needs an instance context")
        board = tool_input.get("board") or "default"
        column = tool_input.get("column") or "todo"
        text = tool_input.get("text") or ""
        try:
            return _json_dumps(kanban.add_card(instance_id, board, column, text))
        except kanban.KanbanError as exc:
            raise ToolError(str(exc))

    if name == "kanban_list_cards":
        from bot import kanban

        if instance_id is None:
            raise ToolError("kanban_list_cards needs an instance context")
        board = tool_input.get("board") or "default"
        return _json_dumps(kanban.list_cards(instance_id, board))

    if name == "kanban_move_card":
        from bot import kanban

        if instance_id is None:
            raise ToolError("kanban_move_card needs an instance context")
        card_id = tool_input.get("card_id")
        column = tool_input.get("column") or "todo"
        if card_id is None:
            raise ToolError("card_id is required")
        try:
            return _json_dumps(kanban.move_card(instance_id, int(card_id), column))
        except kanban.KanbanError as exc:
            raise ToolError(str(exc))

    if name == "schedule_command":
        from bot import scheduler

        if instance_id is None:
            raise ToolError("schedule_command needs an instance context")
        kind = tool_input.get("kind") or ""
        prompt = tool_input.get("prompt") or ""
        interval = tool_input.get("interval") or ""
        chat_id = tool_input.get("chat_id")
        thread_id = tool_input.get("thread_id")
        max_runs = tool_input.get("max_runs")
        if not prompt.strip():
            raise ToolError("prompt is required")
        try:
            interval_s = scheduler.parse_duration(interval)
            sched_id = scheduler.create(
                instance_id, chat_id, kind, prompt, interval_s, max_runs=max_runs, thread_id=thread_id,
            )
        except scheduler.ScheduleError as exc:
            raise ToolError(str(exc))
        return _json_dumps({"id": sched_id})

    if name == "list_schedules":
        from bot import scheduler

        if instance_id is None:
            raise ToolError("list_schedules needs an instance context")
        return _json_dumps(scheduler.list_for_chat(instance_id, chat_id=None))

    if name in ("pause_schedule", "remove_schedule"):
        from bot import db, scheduler

        if instance_id is None:
            raise ToolError(f"{name} needs an instance context")
        sched_id = tool_input.get("schedule_id")
        if sched_id is None:
            raise ToolError("schedule_id is required")
        row = db.get_scheduled_command(int(sched_id))
        # scheduler.pause/remove take no instance_id themselves — this
        # ownership check is what stops one instance from touching
        # another's schedule via this tool, since scheduler.py's own
        # functions don't enforce that boundary at all.
        if row is None or row["instance_id"] != instance_id:
            raise ToolError(f"schedule {sched_id} not found")
        if name == "pause_schedule":
            scheduler.pause(int(sched_id))
        else:
            scheduler.remove(int(sched_id))
        return "ok"

    if name == "delegate_to_instance":
        from bot import agent_control, bot_instances, db
        from bot.backends.base import BackendError
        from bot.config import config
        from bot.router import router

        if instance_id is None:
            raise ToolError("delegate_to_instance needs an instance context")
        target_ref = tool_input.get("target_instance")
        prompt = (tool_input.get("prompt") or "").strip()
        if not target_ref or not prompt:
            raise ToolError("both target_instance and prompt are required")

        max_depth = config.current.get("agent_runtime", {}).get("max_delegation_depth", 2)
        depth = _delegation_depth.get()
        if depth >= max_depth:
            raise ToolError(
                f"delegation depth limit reached (depth={depth}, max_delegation_depth={max_depth}) — "
                "raise agent_runtime.max_delegation_depth in config/backends.yaml if deeper nesting is required"
            )

        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        if not agent_control.can_target(instance_id, target["id"]):
            raise ToolError(f"instance {instance_id} is not permitted to target {target['name']!r} under the current allowlist")

        source = bot_instances.get_instance(instance_id)
        source_name = source["name"] if source else f"instance {instance_id}"
        # Matches the ask_instance/dispatch_swarm_goal audit shape exactly
        # (found missing here while building the delegation-activity
        # dashboard panel — this call was invisible in that panel's audit-
        # log-backed data source until this was added) so all three
        # cross-instance delegation paths show up the same way.
        db.log_audit(actor=f"agent:{source_name}", action="agent_delegate", detail=f"-> {target['name']}: {prompt[:120]}")

        token = _delegation_depth.set(depth + 1)
        try:
            result = await router.ask(prompt, action_type="agent_delegate", instance_id=target["id"])
        except BackendError as exc:
            raise ToolError(f"delegation to {target['name']!r} failed: {exc}")
        finally:
            _delegation_depth.reset(token)
        return result.text

    if name == "spawn_subagent":
        from bot.agent_runtime import subagents
        from bot.backends.base import BackendError

        tasks = tool_input.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ToolError("tasks must be a non-empty array of {goal, output_schema?} objects")
        role = tool_input.get("role", "leaf")
        if role not in ("leaf", "orchestrator"):
            raise ToolError("role must be 'leaf' or 'orchestrator'")
        provider = tool_input.get("provider")
        model = tool_input.get("model")
        if bool(provider) != bool(model):
            raise ToolError("provider and model must both be given, or both omitted")
        max_children = tool_input.get("max_children")
        background = bool(tool_input.get("background", False))
        try:
            result = await subagents.run_batch(
                tasks, role=role, provider=provider, model=model,
                max_children=max_children, parent_instance_id=instance_id,
                background=background,
            )
        except BackendError as exc:
            raise ToolError(str(exc))
        return _json_dumps(result)

    if name == "list_subagents":
        from bot.agent_runtime import subagent_registry

        dispatch_id = tool_input.get("dispatch_id")
        if dispatch_id:
            return _json_dumps(subagent_registry.describe(dispatch_id, parent_instance_id=instance_id))
        return _json_dumps(subagent_registry.describe_all(instance_id))

    if name == "steer_subagent":
        from bot.agent_runtime import subagent_registry

        dispatch_id = tool_input.get("dispatch_id")
        child_index = tool_input.get("child_index")
        message = (tool_input.get("message") or "").strip()
        if not dispatch_id or child_index is None or not message:
            raise ToolError("dispatch_id, child_index, and message are all required")
        subagent_registry.steer(dispatch_id, int(child_index), message, parent_instance_id=instance_id)
        return "steered"

    if name == "stop_subagent":
        from bot.agent_runtime import subagent_registry

        dispatch_id = tool_input.get("dispatch_id")
        child_index = tool_input.get("child_index")
        if not dispatch_id or child_index is None:
            raise ToolError("dispatch_id and child_index are both required")
        subagent_registry.stop(dispatch_id, int(child_index), parent_instance_id=instance_id)
        return "stopped"

    if name == "consult_models":
        from bot.agent_runtime import moa
        from bot.backends.base import BackendError

        question = (tool_input.get("question") or "").strip()
        references = tool_input.get("references")
        if not question:
            raise ToolError("question is required")
        if not isinstance(references, list) or not references:
            raise ToolError("references must be a non-empty array of {provider?, model} objects")
        aggregator = tool_input.get("aggregator")
        try:
            return await moa.consult(question, references, aggregator)
        except BackendError as exc:
            raise ToolError(str(exc))

    if name == "get_my_profile":
        from bot import bot_instances

        if instance_id is None:
            raise ToolError("get_my_profile needs an instance context")
        profile = bot_instances.render_profile_markdown(instance_id)
        if profile is None:
            raise ToolError(f"instance {instance_id} not found")
        return profile

    if name == "update_agent_config":
        from bot import agent_control, bot_instances

        if instance_id is None:
            raise ToolError("update_agent_config needs an instance context")
        target_ref = tool_input.get("target_instance")
        if not target_ref:
            raise ToolError("target_instance is required")
        target = agent_control.resolve_instance(target_ref)
        if target is None:
            raise ToolError(f"no bot instance found matching {target_ref!r}")
        if not agent_control.can_target(instance_id, target["id"]):
            raise ToolError(f"instance {instance_id} is not permitted to reconfigure {target['name']!r} under the current allowlist")

        fields = {
            k: tool_input[k]
            for k in ("name", "custom_instructions", "persona", "model", "can_target")
            if k in tool_input
        }
        if not fields:
            raise ToolError("nothing to update — pass at least one of name/custom_instructions/persona/model/can_target")
        try:
            bot_instances.update_instance(target["id"], actor=f"agent:{instance_id}", **fields)
        except bot_instances.ValidationError as exc:
            raise ToolError(str(exc))
        updated = bot_instances.render_profile_markdown(target["id"])
        return f"Updated {target['name']!r} (id {target['id']}).\n\n{updated}"

    if name == "read_project_context":
        from bot import shared_context

        doc_name = (tool_input.get("name") or "").strip()
        if not doc_name:
            raise ToolError("name is required")
        try:
            doc = shared_context.read_doc(doc_name)
        except shared_context.SharedContextError as exc:
            raise ToolError(str(exc))
        if doc is None:
            return f"No shared context doc named {doc_name!r} yet — use write_project_context to create it."
        return doc["content"]

    if name == "write_project_context":
        from bot import shared_context

        doc_name = (tool_input.get("name") or "").strip()
        content = tool_input.get("content", "")
        if not doc_name:
            raise ToolError("name is required")
        actor = f"agent:{instance_id}" if instance_id is not None else "agent"
        try:
            shared_context.write_doc(doc_name, content, actor)
        except shared_context.SharedContextError as exc:
            raise ToolError(str(exc))
        return f"Saved shared context doc {doc_name!r} ({len(content)} chars)."

    if name == "list_project_context":
        from bot import shared_context

        docs = shared_context.list_docs()
        if not docs:
            return "(no shared context docs yet)"
        return "\n".join(f"- {d['name']} ({d['size']} chars, updated {d['updated_at']} by {d['updated_by']})" for d in docs)

    from bot import plugins as plugin_registry

    if plugin_registry.has_tool(name):
        try:
            return await plugin_registry.execute_tool(name, tool_input, workspace=workspace, instance_id=instance_id)
        except KeyError:
            pass

    raise ToolError(f"unknown tool {name!r}")
