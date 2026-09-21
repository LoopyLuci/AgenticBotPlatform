"""Named agents defined in Markdown (roadmap P4).

An agent definition is a Markdown file with YAML front matter and a prompt:

    ---
    name: reviewer
    description: Reviews a change for bugs and missing tests. Read-only.
    tools: [read_file, grep, glob, git_diff]        # or  Read, Grep, Glob   (Claude Code names)
    model: openrouter/anthropic/claude-haiku        # optional: provider/model
    mode: plan                                      # optional: only "plan" (read-only) is honoured
    isolation: worktree                             # optional: run in its own git worktree
    ---
    You review code changes. Look for ...

`spawn_subagent` runs one when a task names it (`{"goal": "...", "agent": "reviewer"}`): the
child gets the definition's prompt, only its tools, and (optionally) its model, in a session
of its own so the parent's context stays clean.

**Where they are found** (later wins on a name clash, but built-ins and your own files always
beat a repository's): built-ins, then `<ABP data>/agents/*.md`, then in the working directory
`.claude/agents/*.md`, `.opencode/agent/*.md` and `.abp/agents/*.md` - so agents written for
Claude Code or OpenCode work as they are.

**What a definition may and may not do.** Files in a repository are text from wherever the
repository came from, so a definition can only make a child *safer or narrower*:
* `tools` can only remove tools from what a child would have anyway - never add one, and never
  grant approval;
* `mode` is honoured only as `plan` (read-only); anything else is ignored;
* `model` must name a provider you have configured, or it is ignored;
* the prompt is added to the child's own system prompt and cannot change permissions.
Problems (a missing description, an unknown field value) are listed by `list_agents`.

Tool names from Claude Code (`Read`, `Grep`, `Glob`, `Bash`, `Edit`, `Write`, `WebFetch`,
`WebSearch`, `TodoWrite`) are translated; OpenCode's `tools: {write: false}` form is understood.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bot.agent_runtime.agent_defs")

MAX_PROMPT_CHARS = 12_000
MAX_FILE_BYTES = 60_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

READ_TOOLS = ("read_file", "list_dir", "grep", "glob", "repo_map", "code_search", "session_search", "git_status",
              "git_diff", "todo_read", "read_skill", "list_skills", "read_skill_file", "model_info", "find_models")
# Claude Code / OpenCode tool names -> ours
ALIASES: dict[str, tuple[str, ...]] = {
    "read": ("read_file", "list_dir"), "grep": ("grep", "code_search"), "glob": ("glob", "repo_map"),
    "bash": ("run_shell", "shell_output", "shell_list", "shell_kill"), "shell": ("run_shell", "shell_output", "shell_list", "shell_kill"),
    "edit": ("edit_file", "multi_edit", "apply_patch"), "write": ("write_file",), "patch": ("apply_patch",),
    "webfetch": ("web_fetch",), "websearch": ("web_search",), "todowrite": ("todo_write", "todo_read"),
    "todoread": ("todo_read",), "list": ("list_dir",), "search": ("grep", "code_search"),
}


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    prompt: str
    tools: Optional[frozenset] = None          # None = every tool a child would normally get
    model: Optional[str] = None                # "provider/model"
    mode: Optional[str] = None                 # "plan" only
    isolation: Optional[str] = None            # "worktree"
    source: str = "built-in"                   # built-in | user | project
    origin: str = ""                           # the file
    problems: tuple = ()


BUILTINS: dict[str, AgentDef] = {d.name: d for d in [
    AgentDef(
        "explore", "Fast, read-only search of a codebase: finds files, symbols and answers questions about how things work.",
        "You are a code explorer. Answer the question you were given by searching and reading the project - use repo_map, "
        "code_search, grep, glob and read_file. Do not change anything. Finish with a short answer that names the files and "
        "line numbers you relied on; do not paste large blocks of code.",
        tools=frozenset(READ_TOOLS), mode="plan"),
    AgentDef(
        "plan", "Turns a request into a concrete implementation plan without changing anything.",
        "You are a planner. Read the relevant code, then write a numbered plan: the files to change, what changes in each, the "
        "order, how to verify, and the risks. Do not change anything. If something is unclear, list it as an open question.",
        tools=frozenset(READ_TOOLS) | {"todo_write"}, mode="plan"),
    AgentDef(
        "reviewer", "Reviews the current changes for bugs, missing tests and unclear code. Read-only.",
        "You are a careful code reviewer. Look at the uncommitted changes (git_diff, git_status) and the code around them. Report "
        "real problems first (bugs, unhandled cases, security issues, missing tests), each with file and line and why it matters; "
        "then smaller suggestions. Say plainly if you found nothing wrong. Do not change anything.",
        tools=frozenset(READ_TOOLS), mode="plan"),
    AgentDef(
        "general", "General-purpose worker for a multi-step task with the normal tools.",
        "You are a worker given one self-contained task. Do it fully, verify the result, and report what you did and what you found "
        "in a few sentences.", tools=None),
]}


def _data_dir() -> Optional[Path]:
    try:
        from bot import envfile

        home = getattr(envfile, "ABP_HOME_ACTIVE", None)
        return Path(home) if home else Path(envfile.PROJECT_ROOT) / "data"
    except Exception:  # noqa: BLE001
        return None


def _split_front_matter(text: str) -> tuple[dict, str, Optional[str]]:
    """(metadata, body, problem)."""
    import yaml

    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text, "no front matter (the file should start with ---)"
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)(.*)$", text, re.S)
    if not m:
        return {}, text, "front matter is not closed with ---"
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        return {}, m.group(2), f"front matter is not valid YAML ({str(exc).splitlines()[0][:80]})"
    if not isinstance(meta, dict):
        return {}, m.group(2), "front matter must be a mapping"
    return meta, m.group(2), None


def _tool_set(raw) -> tuple[Optional[frozenset], list[str]]:
    """Translate a tools declaration into ABP tool names. Returns (set or None for 'all', problems)."""
    problems: list[str] = []
    if raw is None:
        return None, problems

    from bot.agent_runtime import tools as agent_tools, toolspec

    exact = set(agent_tools.TOOL_SCHEMA_NAMES) | set(toolspec.registered_names())

    def expand(name: str) -> set[str]:
        name = name.strip()
        if name in exact:                          # one of our own tool names, spelled as ours: taken literally
            return {name}
        key = re.sub(r"[^a-z]", "", name.lower())
        if key in ALIASES:                         # a Claude Code / OpenCode name: translated
            return set(ALIASES[key])
        return {name} if name else set()

    if isinstance(raw, dict):                       # OpenCode: {write: false, bash: false}
        denied: set[str] = set()
        allowed: set[str] = set()
        for k, v in raw.items():
            (allowed if v else denied).update(expand(str(k)))
        if allowed and not denied:
            return frozenset(allowed), problems
        from bot.agent_runtime import tools as agent_tools, toolspec

        every = set(agent_tools.TOOL_SCHEMA_NAMES) | set(toolspec.registered_names())
        return frozenset(every - denied), problems
    if isinstance(raw, str):
        raw = [t for t in re.split(r"[,\s]+", raw) if t]
    if not isinstance(raw, list):
        return None, ["tools must be a list, a comma-separated string or a mapping"]
    out: set[str] = set()
    for item in raw:
        out.update(expand(str(item)))
    return frozenset(out), problems


def parse(text: str, *, source: str, origin: str = "", fallback_name: str = "") -> Optional[AgentDef]:
    meta, body, problem = _split_front_matter(text)
    problems = [problem] if problem else []
    name = str(meta.get("name") or fallback_name or "").strip().lower()
    if not NAME_RE.match(name):
        return None
    description = " ".join(str(meta.get("description") or "").split())[:300]
    if not description:
        problems.append("no description, so the model cannot tell when to use it")
    tools, tool_problems = _tool_set(meta.get("tools"))
    problems += tool_problems
    model = str(meta.get("model") or "").strip() or None
    if model and ("/" not in model or model.lower() in ("inherit", "sonnet", "opus", "haiku")):
        model = None                                   # aliases from other tools: use the default model
    mode = str(meta.get("mode") or meta.get("permissionMode") or "").strip().lower() or None
    if mode and mode not in ("plan", "read-only", "readonly"):
        problems.append(f"mode {mode!r} is ignored (only 'plan' is honoured)")
        mode = None
    elif mode:
        mode = "plan"
    isolation = str(meta.get("isolation") or "").strip().lower() or None
    if isolation and isolation != "worktree":
        problems.append(f"isolation {isolation!r} is ignored (only 'worktree' exists)")
        isolation = None
    prompt = body.strip()[:MAX_PROMPT_CHARS]
    if not prompt:
        problems.append("the prompt is empty")
    return AgentDef(name, description, prompt, tools, model, mode, isolation, source, origin, tuple(problems))


def _load_dir(folder: Path, source: str, found: dict[str, AgentDef]) -> None:
    try:
        files = sorted(folder.glob("*.md"))
    except OSError:
        return
    for path in files:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        d = parse(text, source=source, origin=str(path), fallback_name=path.stem)
        if d is None:
            logger.warning("ignored agent file %s: no usable name", path)
            continue
        found[d.name] = d


def discover(workspace: Optional[Path] = None) -> dict[str, AgentDef]:
    """Every agent visible from `workspace`. Built-ins and the user's own files cannot be shadowed by a repository."""
    found: dict[str, AgentDef] = dict(BUILTINS)
    project: dict[str, AgentDef] = {}
    if workspace is not None:
        root = Path(workspace)
        for rel in (".claude/agents", ".opencode/agent", ".opencode/agents", ".abp/agents"):
            _load_dir(root / rel, "project", project)
    user: dict[str, AgentDef] = {}
    data = _data_dir()
    if data is not None:
        _load_dir(data / "agents", "user", user)
    for name, d in project.items():
        if name not in BUILTINS:
            found[name] = d
    found.update(user)
    return found


def resolve(name: str, workspace: Optional[Path] = None) -> Optional[AgentDef]:
    return discover(workspace).get(str(name or "").strip().lower())


def summary(workspace: Optional[Path] = None, limit: int = 12) -> str:
    defs = sorted(discover(workspace).values(), key=lambda d: (d.source != "built-in", d.name))[:limit]
    lines = ["Agents you can hand a task to with spawn_subagent (set \"agent\" on the task):"]
    for d in defs:
        lines.append(f"- {d.name}: {d.description or '(no description)'}" + (" [from this project]" if d.source == "project" else ""))
    return "\n".join(lines)


def restrict_tools(base: Optional[frozenset], d: AgentDef) -> Optional[frozenset]:
    """The child's tools: what it would have anyway, narrowed by the definition - never widened."""
    if d.tools is None:
        return base
    if base is None:
        from bot.agent_runtime import tools as agent_tools, toolspec

        base = frozenset(agent_tools.TOOL_SCHEMA_NAMES) | toolspec.registered_names()
    return base & d.tools


# ---- the list_agents tool -----------------------------------------------------------
async def _list_agents(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    import json

    rows = []
    for d in sorted(discover(workspace).values(), key=lambda x: (x.source != "built-in", x.name)):
        rows.append({"name": d.name, "description": d.description, "source": d.source,
                     "tools": sorted(d.tools) if d.tools is not None else "all", "model": d.model, "read_only": d.mode == "plan",
                     "isolation": d.isolation, "problems": list(d.problems)})
    return json.dumps(rows)


def register_all() -> None:
    from bot.agent_runtime import toolspec

    toolspec.register(
        {"name": "list_agents",
         "description": "List the named agents you can hand a task to (spawn_subagent with an \"agent\" on the task): built-ins "
                        "such as explore, plan and reviewer, plus any defined in this project or by the user.",
         "input_schema": {"type": "object", "properties": {}, "required": []}},
        toolspec.ToolSpec("list_agents", "read", read_only=True, concurrency_safe=True, origin="registered"), _list_agents)


register_all()
