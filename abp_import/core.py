"""Read another agent product's configuration and turn it into ABP's (roadmap P9).

    python -m abp_import claude-code [--project DIR] [--user-home DIR] [--apply]
    python -m abp_import opencode    [--project DIR] [--user-home DIR] [--apply]

**A dry run by default**: it prints what it would set up, what it could not translate and why, and changes nothing until
`--apply`. Importing never widens what the agent may do on its own:

* permissions become ABP permission rules (`native_agent.permissions.rules`). A blanket allow such as a bare `Bash` (run
  anything) is **not** imported as an allow - it is reported and skipped - and a "bypass permissions" default mode is never
  imported. Allow / ask / deny rules that name a command or path pattern are.
* hooks (Claude Code's `command` hooks) become ABP hooks with the tool names translated (`Bash` -> `run_shell`, ...).
* MCP servers become external MCP servers, **untrusted by default** in ABP whatever they were in the other product, and
  their tool descriptions are pinned on first connection like any other.

Not imported, and said so in the report: model and provider settings and API keys (set them in config/providers.yaml), themes and
keybindings, agents / skills / commands (ABP already reads `.claude/` and `.opencode/` folders directly), and anything whose meaning
could not be established. The Hermes and OpenClaw importers are **not built**: their configuration formats were not checked.

Server environment values (which may hold tokens) are copied into ABP's database only on --apply and are never printed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Claude Code / OpenCode tool names -> ABP tools (matches bot/agent_runtime/agent_defs.ALIASES, but for rules and hooks).
TOOLS = {
    "bash": ["run_shell"], "shell": ["run_shell"], "read": ["read_file", "list_dir"], "edit": ["edit_file", "multi_edit", "apply_patch"],
    "multiedit": ["multi_edit"], "write": ["write_file"], "patch": ["apply_patch"], "grep": ["grep"], "glob": ["glob"], "ls": ["list_dir"],
    "list": ["list_dir"], "webfetch": ["web_fetch"], "websearch": ["web_search"], "todowrite": ["todo_write"], "todoread": ["todo_read"],
    "task": ["spawn_subagent"], "notebookedit": ["edit_file"],
}
MODE_MAP = {"default": "default", "acceptedits": "accept_edits", "plan": "plan"}
HOOK_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "SubagentStop",
               "PreCompact", "Notification"}


@dataclass
class Plan:
    source: str
    rules: list[dict] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    mode: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.rules or self.hooks or self.mcp_servers or self.mode)


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


def _pattern(tool: str, spec: str) -> str:
    """The ABP `match` for a Claude-style specifier: '<prefix>:*' is a prefix match; 'domain:x' is a host."""
    spec = spec.strip()
    if tool == "web_fetch" and spec.lower().startswith("domain:"):
        return spec[7:].strip()
    if spec.endswith(":*"):
        return spec[:-2] + "*"
    if spec.startswith("./"):
        return spec[2:]
    return spec


_RULE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*(?:\((.*)\))?\s*$", re.S)


def translate_rule(decision: str, entry: str, source: str, plan: Plan) -> None:
    m = _RULE.match(entry or "")
    if not m:
        plan.warnings.append(f"{source}: could not read the permission {entry!r}")
        return
    name, spec = m.group(1), m.group(2)
    tools = _tool_names(name)
    if not tools:
        plan.warnings.append(f"{source}: {entry!r} names a tool ABP does not have ({name}); skipped")
        return
    specific_mcp_tool = name.lower().startswith("mcp__") and name.count("__") >= 2        # one named tool, not "everything"
    if decision == "allow" and not spec and not specific_mcp_tool:
        plan.warnings.append(f"{source}: allow {entry!r} would let the agent use {name} without asking for anything; not imported (name a pattern to import it)")
        return
    for tool in tools:
        rule = {"decision": decision, "tool": tool, "match": _pattern(tool, spec) if spec else "", "note": f"imported from {plan.source}"}
        if rule not in plan.rules:
            plan.rules.append(rule)


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
    for event, groups in hooks.items():
        if event not in HOOK_EVENTS:
            plan.warnings.append(f"{source}: hook event {event!r} does not exist in ABP; skipped")
            continue
        for group in groups if isinstance(groups, list) else []:
            matcher = _hook_matcher(str((group or {}).get("matcher") or ""), plan, source)
            for h in (group or {}).get("hooks") or []:
                if not isinstance(h, dict) or h.get("type", "command") != "command" or not h.get("command"):
                    plan.warnings.append(f"{source}: a {event} hook that is not a command hook was skipped")
                    continue
                entry = {"event": event, "matcher": matcher, "command": str(h["command"])}
                if matcher != "__no_match__" and entry not in plan.hooks:
                    plan.hooks.append(entry)


def translate_mcp(servers: Any, plan: Plan, source: str) -> None:
    if not isinstance(servers, dict):
        return
    for name, spec in servers.items():
        if not isinstance(spec, dict) or spec.get("disabled") or spec.get("enabled") is False:
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
                                     "env": {str(k): str(v) for k, v in (spec.get("env") or spec.get("environment") or {}).items()}})
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


def claude_code(project: Path, home: Path) -> Plan:
    plan = Plan("Claude Code")
    for label, path in (("user settings", home / ".claude" / "settings.json"), ("project settings", project / ".claude" / "settings.json"),
                        ("local settings", project / ".claude" / "settings.local.json"), ("project MCP", project / ".mcp.json")):
        data = _load(path)
        if data is None:
            continue
        plan.files.append(str(path))
        perms = data.get("permissions") or {}
        for decision in ("deny", "ask", "allow"):                  # deny first so a later allow can never precede it
            for entry in perms.get(decision) or []:
                translate_rule(decision, str(entry), label, plan)
        mode = str(perms.get("defaultMode") or "").replace("_", "").lower()
        if mode in MODE_MAP and MODE_MAP[mode] != "default":
            plan.mode = MODE_MAP[mode]
        elif mode:
            plan.warnings.append(f"{label}: default mode {perms.get('defaultMode')!r} is not imported (bypassing permissions is never imported)")
        translate_hooks(data.get("hooks"), plan, label)
        translate_mcp(data.get("mcpServers"), plan, label)
        for key in ("env", "model", "apiKeyHelper", "statusLine"):
            if key in data:
                plan.warnings.append(f"{label}: '{key}' is not imported")
    return plan


def opencode(project: Path, home: Path) -> Plan:
    plan = Plan("OpenCode")
    candidates = [home / ".config" / "opencode" / "opencode.json", home / ".config" / "opencode" / "opencode.jsonc",
                  project / "opencode.json", project / "opencode.jsonc"]
    for path in candidates:
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
    for w in plan.warnings:
        lines.append(f"  note   {w}")
    if plan.empty():
        lines.append("Nothing to import.")
    return "\n".join(lines)


def apply(plan: Plan) -> dict:
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
            config.set_value(["native_agent", "permissions", "rules"], current + added, actor=f"import:{plan.source}")
        done["rules"] = len(added)
    if plan.mode:
        config.set_value(["native_agent", "permissions", "mode"], plan.mode, actor=f"import:{plan.source}")
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
        db.add_external_mcp_server(s["name"], s["transport"], command=s.get("command"), args_json=json.dumps(s.get("args", [])),
                                   env_json=json.dumps(s.get("env", {})), url=s.get("url"), auth_token=s.get("auth_token") or None)
        done["mcp_servers"] += 1
    return done
