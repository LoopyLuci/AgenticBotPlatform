"""Read another agent product's configuration and turn it into ABP's (roadmap P9).

    python -m abp_import claude-code [--project DIR] [--user-home DIR] [--apply]
    python -m abp_import opencode    [--project DIR] [--user-home DIR] [--apply]
    python -m abp_import hermes      [--source DIR] [--apply] ...      (see agents.py)
    python -m abp_import openclaw    [--source DIR] [--apply] ...

**A dry run by default**: it prints what it would set up, what it could not translate and why, and changes nothing until
`--apply`. Importing never widens what the agent may do on its own:

* permissions become ABP permission rules (`native_agent.permissions.rules`). A blanket allow such as a bare `Bash` (run
  anything) is **not** imported as an allow - it is reported and skipped - and a "bypass permissions" default mode is never
  imported. Allow / ask / deny rules that name a command or path pattern are.
* hooks (Claude Code's `command` hooks) become ABP hooks with the tool names translated (`Bash` -> `run_shell`, ...). Only
  the three tool events carry a tool matcher; a session event's matcher filters *session starts*, which ABP has no notion of,
  so it is dropped (with a note) rather than losing the hook.
* MCP servers become external MCP servers, **untrusted by default** in ABP whatever they were in the other product, and
  their tool descriptions are pinned on first connection like any other. `${VAR}` in a server's environment is read from this
  process's environment.

Where the files are, as the products themselves look for them:

* Claude Code: `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`), the project's `.claude/settings.json`,
  `.claude/settings.local.json`, `.mcp.json`, and `~/.claude.json` - where `claude mcp add` keeps a project's user-scope
  servers, the tools it allowed there, and the `.mcp.json` servers this project has switched off.
* OpenCode: `$OPENCODE_CONFIG` (the file it was launched with), `$XDG_CONFIG_HOME`/`~/.config/opencode/opencode.json(c)`,
  then the project's `opencode.json(c)`.

Not imported, and said so in the report: model and provider settings and API keys (set them in config/providers.yaml), themes and
keybindings, agents / skills / commands (ABP already reads `.claude/` and `.opencode/` folders directly), folders outside the
workspace (`permissions.additionalDirectories`, OpenCode's `permission.external_directory` - a bot's workspace is its own folder),
the SSE transport (ABP speaks stdio and streamable HTTP), and anything whose meaning could not be established. Hermes and OpenClaw
carry much more (providers and keys, a persona, memories, skill libraries, scheduled jobs, chat channels): their importers are in
agents.py and use the extra plan fields below.

Server environment values (which may hold tokens) are copied into ABP's database only on --apply and are never printed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Claude Code / OpenCode tool names -> ABP tools (matches bot/agent_runtime/agent_defs.ALIASES, but for rules and hooks).
# Claude Code's PowerShell tool is ABP's shell too: it is the only shell ABP has, so a `PowerShell(...)` permission is the
# nearest rule there is (translate_rule says so once per file - the dialect is not the same).
TOOLS = {
    "bash": ["run_shell"], "shell": ["run_shell"], "powershell": ["run_shell"],
    "bashoutput": ["shell_output"], "killshell": ["shell_kill"], "killbash": ["shell_kill"],
    "read": ["read_file", "list_dir"], "edit": ["edit_file", "multi_edit", "apply_patch"],
    "multiedit": ["multi_edit"], "write": ["write_file"], "patch": ["apply_patch"], "grep": ["grep"], "glob": ["glob"], "ls": ["list_dir"],
    "list": ["list_dir"], "webfetch": ["web_fetch"], "websearch": ["web_search"], "todowrite": ["todo_write"], "todoread": ["todo_read"],
    "task": ["spawn_subagent"], "notebookedit": ["edit_file"],
}
FILE_TOOLS = frozenset({"read_file", "list_dir", "edit_file", "multi_edit", "apply_patch", "write_file"})
MODE_MAP = {"default": "default", "acceptedits": "accept_edits", "plan": "plan"}
HOOK_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "SubagentStop",
               "PreCompact", "Notification"}
# ABP judges a hook's matcher against the tool being called, and only for these three (bot/agent_runtime/hooks.py).
TOOL_HOOK_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure"})
_ENV_REF = re.compile(r"\$\{(\w+)\}")


@dataclass
class Plan:
    source: str
    rules: list[dict] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    mode: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    # Hermes / OpenClaw (agents.py). Secret values (api_key, credentials) are never printed.
    providers: list[dict] = field(default_factory=list)      # {name, base_url, protocol, api_key, catalog_id, origin}
    router_also: list[str] = field(default_factory=list)     # "provider/model" refs the model router may pick
    skill_dirs: list[dict] = field(default_factory=list)     # {path, count}: linked in place
    agent: Optional[dict] = None                             # {name, instructions}: becomes a bot's custom instructions
    memories: list[dict] = field(default_factory=list)       # {kind, content}
    schedules: list[dict] = field(default_factory=list)      # {name, prompt, interval_s, first_run_at, platform, chat_id}
    channels: list[dict] = field(default_factory=list)       # {platform, credentials, allowed_user_ids}

    def empty(self) -> bool:
        return not (self.rules or self.hooks or self.mcp_servers or self.mode or self.providers or self.router_also
                    or self.skill_dirs or self.agent or self.memories or self.schedules or self.channels)


def _load(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)              # opencode.jsonc allows comments
    text = re.sub(r",(\s*[}\]])", r"\1", text)                     # ... and trailing commas
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _tool_names(name: str) -> list[str]:
    n = name.strip()
    if n.lower().startswith("mcp__"):                             # mcp__server__tool -> mcp_<server>_<tool>
        parts = n.split("__", 2)
        return [f"mcp_{parts[1]}_{parts[2]}"] if len(parts) == 3 else [f"mcp_{parts[1]}_*"] if len(parts) == 2 else []
    return TOOLS.get(n.lower().replace("_", ""), [])


def _path_key(path: Any) -> str:
    """One spelling of a folder, so `Z:/Projects/X`, `z:\\Projects\\X` and `z:/Projects/X/` all compare equal.
    Claude Code's own store keys projects by path and writes both cases."""
    text = str(path).replace("\\", "/").rstrip("/")
    return re.sub(r"^([A-Za-z]):/", lambda m: f"{m.group(1).lower()}:/", text).lower()


def _relative_to(path: str, root: str) -> Optional[str]:
    """`path` as a root-relative posix path, or None when it is not inside root."""
    low, base = path.lower().rstrip("/"), root.lower().rstrip("/")
    return path[len(base) + 1:] if low.startswith(base + "/") else None


def _file_pattern(spec: str, project: Optional[Path]) -> str:
    """A Claude-style file specifier as an ABP `match`. Claude Code writes a Windows folder as `//c/Users/...` and mixes
    separators; ABP matches a file tool's subject relative to the workspace when the path is inside it, and as written
    otherwise (bot/agent_runtime/permissions.subjects)."""
    p = spec.strip().replace("\\", "/")
    while p.startswith("//"):
        p = p[1:]                                                        # //c/Users -> /c/Users
    if re.match(r"^/[A-Za-z]/", p):
        p = p[1].upper() + ":" + p[2:]                                    # /c/Users -> C:/Users
    if project is not None and re.match(r"^[A-Za-z]:/", p):
        rel = _relative_to(p, str(project).replace("\\", "/"))
        if rel is not None:
            return rel
    return p[2:] if p.startswith("./") else p


def _pattern(tool: str, spec: str, project: Optional[Path] = None) -> str:
    """The ABP `match` for a Claude-style specifier: '<prefix>:*' is a prefix match; 'domain:x' is a host;
    a file tool's path is rewritten to the form ABP matches on (a shell command is left alone, `./build.sh`
    included - it is matched against the command as the agent wrote it)."""
    spec = spec.strip()
    if tool == "web_fetch" and spec.lower().startswith("domain:"):
        return spec[7:].strip()
    if spec.endswith(":*"):
        return spec[:-2] + "*"
    return _file_pattern(spec, project) if tool in FILE_TOOLS else spec


_RULE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*(?:\((.*)\))?\s*$", re.S)


def _claude_tool(entry: str) -> str:
    """The Claude Code tool a permission entry names ('' when the entry cannot be read)."""
    m = _RULE.match(entry or "")
    return m.group(1) if m else ""


def translate_rule(decision: str, entry: str, source: str, plan: Plan, project: Optional[Path] = None) -> list[str]:
    """Adds the ABP rules one Claude-style permission entry means, and returns the tools they name."""
    m = _RULE.match(entry or "")
    if not m:
        plan.warnings.append(f"{source}: could not read the permission {entry!r}")
        return []
    name, spec = m.group(1), m.group(2)
    tools = _tool_names(name)
    if not tools:
        plan.warnings.append(f"{source}: {entry!r} names a tool ABP does not have ({name}); skipped")
        return []
    specific_mcp_tool = name.lower().startswith("mcp__") and name.count("__") >= 2        # one named tool, not "everything"
    if decision == "allow" and not spec and not specific_mcp_tool:
        plan.warnings.append(f"{source}: allow {entry!r} would let the agent use {name} without asking for anything; not imported (name a pattern to import it)")
        return []
    for tool in tools:
        rule = {"decision": decision, "tool": tool, "match": _pattern(tool, spec, project) if spec else "", "note": f"imported from {plan.source}"}
        if rule not in plan.rules:
            plan.rules.append(rule)
    return tools


def _hook_matcher(matcher: str, plan: Plan, where: str) -> Optional[str]:
    """Claude matchers are regexes over tool names ('Edit|Write'); ABP's are the same form over ABP's names."""
    if not matcher or matcher == "*":
        return None
    out: list[str] = []
    for part in matcher.split("|"):
        names = _tool_names(part)
        if not names:
            plan.warnings.append(f"{where}: hook matcher {part!r} names a tool ABP does not have")
            continue
        out.extend(names)
    return "|".join(dict.fromkeys(out)) or "__no_match__"


def translate_hooks(hooks: Any, plan: Plan, source: str) -> None:
    if not isinstance(hooks, dict):
        return
    untranslatable: set[str] = set()
    for event, groups in hooks.items():
        if event not in HOOK_EVENTS:
            plan.warnings.append(f"{source}: hook event {event!r} does not exist in ABP; skipped")
            continue
        tool_event = event in TOOL_HOOK_EVENTS
        said_source = False
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                plan.warnings.append(f"{source}: a {event} hook entry that is not an object was skipped")
                continue
            raw = str(group.get("matcher") or "")
            # A session event's matcher names session sources ('startup|resume|clear|compact'), not tools. ABP fires these
            # hooks unconditionally, so translating it would either lose the hook or match nothing: drop it and say so.
            matcher = _hook_matcher(raw, plan, source) if tool_event else None
            if not tool_event and raw and raw != "*" and not said_source:
                said_source = True
                plan.warnings.append(f"{source}: the {event} matcher {raw!r} chooses which session starts run the hook; ABP "
                                     f"has no such filter, so the hook runs on every {event}")
            for h in group.get("hooks") or []:
                if not isinstance(h, dict) or h.get("type", "command") != "command" or not h.get("command"):
                    plan.warnings.append(f"{source}: a {event} hook that is not a command hook was skipped")
                    continue
                untranslatable.update(k for k in ("timeout", "statusMessage") if k in h)
                entry = {"event": event, "matcher": matcher, "command": str(h["command"])}
                if matcher != "__no_match__" and entry not in plan.hooks:
                    plan.hooks.append(entry)
    if untranslatable:
        plan.warnings.append(f"{source}: a hook's {', '.join(sorted(untranslatable))} is not imported "
                             "(an ABP hook is given 30 seconds and no progress message)")


def _server_env(spec: dict, plan: Plan, source: str, name: str) -> dict[str, str]:
    """A server's environment. `${VAR}` is read from this process's environment, like the products read it; the value is
    never printed. Anything still holding a `${...}` that is not one reference is not expanded."""
    raw = spec.get("env") or spec.get("environment") or {}
    if not isinstance(raw, dict):
        plan.warnings.append(f"{source}: MCP server {name!r} has an 'env' that is not an object; skipped")
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        text = "" if value is None else str(value).strip()
        ref = _ENV_REF.fullmatch(text)
        if ref:
            text = os.environ.get(ref.group(1), "")
            if not text:
                plan.warnings.append(f"{source}: MCP server {name!r} env {key} is ${{{ref.group(1)}}}, which is not set here; "
                                     "imported with an empty value")
        elif "${" in text:
            plan.warnings.append(f"{source}: MCP server {name!r} env {key} is not a single ${{VAR}} reference; not imported")
            text = ""
        out[str(key)] = text
    return out


def translate_mcp(servers: Any, plan: Plan, source: str, skip: frozenset[str] = frozenset()) -> None:
    if not isinstance(servers, dict):
        return
    switched_off: list[str] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            plan.warnings.append(f"{source}: MCP server {name!r} is not an object; skipped")
            continue
        if spec.get("disabled") or spec.get("enabled") is False or str(name) in skip:
            switched_off.append(str(name))
            continue
        clean = re.sub(r"[^A-Za-z0-9_-]", "_", str(name))
        kind = str(spec.get("type") or ("stdio" if spec.get("command") else "http")).lower()
        if kind in ("stdio", "local"):
            command = spec.get("command")
            args = list(spec.get("args") or [])
            if isinstance(command, list):                          # OpenCode: command is the whole argv
                command, args = (command[0] if command else None), command[1:]
            if not command:
                plan.warnings.append(f"{source}: MCP server {name!r} has no command; skipped")
                continue
            plan.mcp_servers.append({"name": clean, "transport": "stdio", "command": str(command), "args": [str(a) for a in args],
                                     "env": _server_env(spec, plan, source, str(name))})
        elif kind in ("http", "remote", "streamable-http", "streamable_http"):
            if not spec.get("url"):
                plan.warnings.append(f"{source}: MCP server {name!r} has no url; skipped")
                continue
            token = ""
            auth = str((spec.get("headers") or {}).get("Authorization", ""))
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            elif spec.get("headers"):
                plan.warnings.append(f"{source}: MCP server {name!r} sends custom headers; only a bearer token is imported")
            plan.mcp_servers.append({"name": clean, "transport": "remote", "url": str(spec["url"]), "auth_token": token})
        else:
            plan.warnings.append(f"{source}: MCP server {name!r} uses the {kind!r} transport, which ABP does not support; skipped")
    if switched_off:
        plan.warnings.append(f"{source}: MCP server {', '.join(repr(n) for n in switched_off)} is switched off; not imported")


def claude_dir(home: Path) -> Path:
    """Claude Code keeps its user-level settings in ~/.claude, or wherever CLAUDE_CONFIG_DIR points (the product's own
    override). Like agents.py's HERMES_HOME, only trusted for the real home, so --user-home stays hermetic."""
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override and home == Path.home():
        return Path(os.path.expandvars(override)).expanduser()
    return home / ".claude"


def _claude_project_entry(project: Path, home: Path) -> Optional[dict]:
    """`claude mcp add` writes a project's user-scope MCP servers, the tools allowed there and the `.mcp.json` servers this
    project has switched off into ~/.claude.json, not into a settings file. Its keys are folder paths in either case."""
    projects = (_load(home / ".claude.json") or {}).get("projects")
    if not isinstance(projects, dict):
        return None
    want = _path_key(project)
    for key, entry in projects.items():
        if isinstance(entry, dict) and _path_key(key) == want:
            return entry
    return None


def claude_code(project: Path, home: Path) -> Plan:
    plan = Plan("Claude Code")
    entry = _claude_project_entry(project, home)
    switched_off = {str(n) for n in (entry or {}).get("disabledMcpjsonServers") or []}
    if entry is not None:
        plan.files.append(str(home / ".claude.json"))
    for label, path, skip in (("user settings", claude_dir(home) / "settings.json", frozenset()),
                              ("project settings", project / ".claude" / "settings.json", frozenset()),
                              ("local settings", project / ".claude" / "settings.local.json", frozenset()),
                              ("project MCP", project / ".mcp.json", frozenset(switched_off))):
        data = _load(path)
        if data is None:
            continue
        plan.files.append(str(path))
        perms = data.get("permissions") or {}
        if not isinstance(perms, dict):
            plan.warnings.append(f"{label}: 'permissions' is not an object; skipped")
            perms = {}
        shells = 0
        for decision in ("deny", "ask", "allow"):                  # deny first so a later allow can never precede it
            for entry_text in perms.get(decision) or []:
                if translate_rule(decision, str(entry_text), label, plan, project) and _claude_tool(str(entry_text)).lower() == "powershell":
                    shells += 1
        if shells:
            plan.warnings.append(f"{label}: {shells} PowerShell(...) rule(s) became run_shell rules - ABP's run_shell is the "
                                 "platform shell (cmd.exe on Windows), so PowerShell-only syntax in them will not run")
        extra = perms.get("additionalDirectories")
        if isinstance(extra, list) and extra:
            plan.warnings.append(f"{label}: {len(extra)} additionalDirectories (folders Claude Code may reach outside the "
                                 "project) have no ABP equivalent - a bot's workspace is its own folder; not imported")
        mode = str(perms.get("defaultMode") or "").replace("_", "").lower()
        if mode in MODE_MAP and MODE_MAP[mode] != "default":
            plan.mode = MODE_MAP[mode]
        elif mode:
            plan.warnings.append(f"{label}: default mode {perms.get('defaultMode')!r} is not imported (bypassing permissions is never imported)")
        translate_hooks(data.get("hooks"), plan, label)
        translate_mcp(data.get("mcpServers"), plan, label, skip)
        for key in ("env", "model", "apiKeyHelper", "statusLine"):
            if key in data:
                plan.warnings.append(f"{label}: '{key}' is not imported")
    if entry is not None:
        for tool in entry.get("allowedTools") or []:
            translate_rule("allow", str(tool), "claude.json allowedTools", plan, project)
        translate_mcp(entry.get("mcpServers"), plan, "claude.json mcpServers")
    return plan


def opencode_configs(project: Path, home: Path) -> list[Path]:
    """Where OpenCode keeps its configuration: `$OPENCODE_CONFIG` (the file it was launched with, as the swarm's own
    launcher does), then `$XDG_CONFIG_HOME`/`~/.config/opencode/opencode.json(c)`, then the project folder. The two
    environment overrides are honoured only for the real home, so --user-home stays hermetic."""
    out: list[Path] = []
    if home == Path.home():
        named = os.environ.get("OPENCODE_CONFIG", "").strip()
        if named:
            given = Path(os.path.expandvars(named)).expanduser()
            out += [given / n for n in ("opencode.json", "opencode.jsonc")] if given.is_dir() else [given]
        base = os.environ.get("XDG_CONFIG_HOME", "").strip()
        config_home = Path(os.path.expandvars(base)).expanduser() if base else home / ".config"
    else:
        config_home = home / ".config"
    out += [config_home / "opencode" / n for n in ("opencode.json", "opencode.jsonc")]
    out += [project / n for n in ("opencode.json", "opencode.jsonc")]
    return list(dict.fromkeys(out))


def opencode(project: Path, home: Path) -> Plan:
    plan = Plan("OpenCode")
    for path in opencode_configs(project, home):
        data = _load(path)
        if data is None:
            continue
        plan.files.append(str(path))
        label = str(path.name)
        translate_mcp(data.get("mcp"), plan, label)
        perms = data.get("permission") or {}
        for key, value in perms.items() if isinstance(perms, dict) else []:
            tools = _tool_names(key)
            if not tools:
                plan.warnings.append(f"{label}: permission for {key!r} has no ABP equivalent; skipped")
                continue
            entries = value if isinstance(value, dict) else {"*": value}
            for pattern, decision in entries.items():
                decision = str(decision).lower()
                if decision not in ("allow", "ask", "deny"):
                    plan.warnings.append(f"{label}: permission {key!r} for {pattern!r} is {decision!r}, which is not a "
                                         "decision ABP knows (allow / ask / deny); skipped")
                    continue
                if decision == "allow" and pattern == "*":
                    plan.warnings.append(f"{label}: blanket allow for {key!r} not imported (name a pattern to import it)")
                    continue
                for tool in tools:
                    rule = {"decision": decision, "tool": tool, "match": "" if pattern == "*" else pattern, "note": "imported from OpenCode"}
                    if rule not in plan.rules:
                        plan.rules.append(rule)
        for key in ("provider", "model", "small_model", "theme", "keybinds", "agent", "command", "instructions"):
            if key in data:
                plan.warnings.append(f"{label}: '{key}' is not imported" + (" (ABP reads agents and commands from .opencode/ folders itself)" if key in ("agent", "command") else ""))
    return plan


IMPORTERS = {"claude-code": claude_code, "opencode": opencode}


def render(plan: Plan) -> str:
    lines = [f"Importing from {plan.source}: " + (", ".join(plan.files) if plan.files else "no configuration files found")]
    if plan.mode:
        lines.append(f"Default permission mode -> {plan.mode}")
    for r in plan.rules:
        lines.append(f"  rule   {r['decision']:<5} {r['tool']}" + (f"  match {r['match']!r}" if r["match"] else ""))
    for h in plan.hooks:
        lines.append(f"  hook   {h['event']}" + (f" [{h['matcher']}]" if h["matcher"] else "") + f": {h['command'][:70]}")
    for s in plan.mcp_servers:
        lines.append(f"  MCP    {s['name']} ({s['transport']}): " + (s.get("command", "") + " " + " ".join(s.get("args", [])) if s["transport"] == "stdio" else s["url"]).strip()[:80])
    for p in plan.providers:
        key = "with its API key" if p.get("api_key") else "no API key"
        lines.append(f"  model  provider {p['name']} -> {p['base_url']} ({key}; from {p.get('origin', plan.source)})")
    for ref in plan.router_also:
        lines.append(f"  model  the router may also pick {ref}")
    for d in plan.skill_dirs:
        lines.append(f"  skills link {d['path']} ({d['count']} skills, used in place)")
    if plan.agent:
        lines.append(f"  agent  a bot named {plan.agent['name']!r} with {len(plan.agent['instructions'])} characters of instructions")
    for kind in ("user", "fact"):
        n = sum(1 for m in plan.memories if m["kind"] == kind)
        if n:
            lines.append(f"  memory {n} {'things about you' if kind == 'user' else 'remembered facts'}")
    for s in plan.schedules:
        lines.append(f"  job    {s['name']!r} every {_every(s['interval_s'])} (created paused)")
    for c in plan.channels:
        lines.append(f"  chat   a {c['platform']} bot for {len(c['allowed_user_ids'])} allowed user(s) (created switched off)")
    for w in plan.warnings:
        lines.append(f"  note   {w}")
    if plan.empty():
        lines.append("Nothing to import.")
    return "\n".join(lines)


def _every(seconds: int) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}" + ("s" if n != 1 else "")
    return f"{seconds} seconds"


def apply(plan: Plan, *, instance_id: Optional[int] = None, with_secrets: bool = True) -> dict:
    """Write the plan into ABP. Returns counts. Rules go to native_agent.permissions.rules (appended, without duplicates, after
    any existing deny rules are kept in front); hooks and MCP servers skip anything already present."""
    from bot import db
    from bot.config import config

    from bot.agent_runtime import permissions

    done = {"rules": 0, "hooks": 0, "mcp_servers": 0, "mode": 0}
    if permissions.is_locked() and (plan.rules or plan.mode):
        raise PermissionError("this host's permission settings are locked (native_agent.permissions.locked), so imported rules and mode were not written")
    if plan.rules:
        current = list((((config.current.get("native_agent") or {}).get("permissions")) or {}).get("rules") or [])
        keys = {(r.get("decision"), r.get("tool"), r.get("match", "")) for r in current}
        added = [r for r in plan.rules if (r["decision"], r["tool"], r["match"]) not in keys]
        if added:
            config.set_values({("native_agent", "permissions", "rules"): current + added}, actor=f"import:{plan.source}")
        done["rules"] = len(added)
    if plan.mode:
        config.set_values({("native_agent", "permissions", "mode"): plan.mode}, actor=f"import:{plan.source}")
        done["mode"] = 1
    existing_hooks = {(h["event"], h["matcher"], h["command"]) for h in db.list_agent_hooks()}
    for h in plan.hooks:
        if (h["event"], h["matcher"], h["command"]) not in existing_hooks:
            db.add_agent_hook(h["event"], h["command"], matcher=h["matcher"])
            done["hooks"] += 1
    existing_servers = {r["name"] for r in db.list_external_mcp_servers()}
    for s in plan.mcp_servers:
        if s["name"] in existing_servers:
            continue
        env = s.get("env", {}) if with_secrets else {k: "" for k in s.get("env", {})}     # --no-secrets: names only
        db.add_external_mcp_server(s["name"], s["transport"], command=s.get("command"), args_json=json.dumps(s.get("args", [])),
                                   env_json=json.dumps(env), url=s.get("url"),
                                   auth_token=(s.get("auth_token") or None) if with_secrets else None)
        done["mcp_servers"] += 1
    from abp_import import agents

    done.update(agents.apply_extra(plan, instance_id=instance_id, with_secrets=with_secrets))
    return done
