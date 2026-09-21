"""Permission rules for tool calls (roadmap P2).

A rule says allow, ask or deny for a tool (by name, by permission class, or `*`),
optionally narrowed by a pattern on what the call touches:

    native_agent:
      permissions:
        mode: default            # default | plan | accept_edits | bypass
        allow_bypass: false      # bypass is ignored unless a host turns this on
        locked: false            # true: per-agent settings cannot change rules or mode
        rules:
          - {decision: allow, tool: run_shell, match: "git status*"}
          - {decision: allow, tool: run_shell, match: "python -m pytest*"}
          - {decision: deny,  tool: run_shell, match: "rm *"}
          - {decision: ask,   tool: "class:write", match: "config/**"}
          - {decision: allow, tool: web_fetch, match: "docs.python.org"}

What a rule matches against ("the subject"): the command for run_shell, the path(s)
for file tools, the host for web_fetch, the query for web_search.

How a decision is reached, in order:
  1. any matching deny rule            -> deny
  2. plan mode and the tool can change something -> deny
  3. any matching ask rule             -> ask
  4. a matching allow rule             -> allow
  5. accept_edits mode and an edit tool -> allow
  6. bypass mode (if permitted)        -> allow
  7. otherwise                          -> default (the tool's own approval setting)
Admin tools are never allowed by a rule or a mode: a person approves each one.
If the session has read untrusted content (a web page, an untrusted MCP server), any
"allow" for a tool that can change something becomes "ask" - see taint.py.

Allow rules for run_shell are deliberately strict: a command containing a shell control
operator (; | & < > ` $( or a newline) never matches an allow pattern, so an allowance for
`git status*` cannot be used to smuggle in `git status; rm -rf ~`. Deny rules are the
opposite: they match any segment of a compound command.
"""

from __future__ import annotations

import contextvars
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import urlparse

from bot.agent_runtime import toolspec

DECISIONS = ("allow", "ask", "deny")
MODES = ("default", "plan", "accept_edits", "bypass")
EDIT_TOOLS = frozenset({"edit_file", "multi_edit", "apply_patch", "write_file"})
_OPERATORS = re.compile(r"[;|&<>`\n]|\$\(")


@dataclass(frozen=True)
class Rule:
    decision: str
    tool: str
    match: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"rule decision must be one of {DECISIONS}, not {self.decision!r}")
        if not self.tool:
            raise ValueError("a rule needs a tool (a name, class:<permission> or *)")


@dataclass(frozen=True)
class Verdict:
    decision: str                 # allow | ask | deny | default
    reason: str = ""
    source: str = ""              # which rule or mode decided it

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def parse_rules(raw: Any) -> list[Rule]:
    """Rules from config or settings. A malformed entry is skipped, never fatal - a typo in a
    settings file must not take the agent down - but the skipped entries are reported by
    `validate_rules` so a settings screen can show them."""
    rules, _ = _parse(raw)
    return rules


def validate_rules(raw: Any) -> list[str]:
    return _parse(raw)[1]


def _parse(raw: Any) -> tuple[list[Rule], list[str]]:
    rules, problems = [], []
    for i, entry in enumerate(raw or [], 1):
        try:
            if not isinstance(entry, dict):
                raise ValueError("a rule is a mapping")
            rules.append(Rule(str(entry.get("decision", "")), str(entry.get("tool", "")),
                              str(entry.get("match", "") or ""), str(entry.get("note", "") or "")))
        except ValueError as exc:
            problems.append(f"rule {i}: {exc}")
    return rules, problems


# ---- subjects and matching -------------------------------------------------------
def subjects(tool: str, tool_input: dict, workspace: Optional[Path] = None) -> list[str]:
    """What the call touches, as strings a pattern can be matched against."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    if tool == "run_shell":
        return [str(inp.get("command", ""))]
    if tool == "apply_patch":
        return [_rel(p, workspace) for p in _patch_paths(str(inp.get("patch", "")))]
    if tool in ("edit_file", "multi_edit", "write_file", "read_file", "list_dir", "grep", "glob"):
        return [_rel(str(inp.get("path") or "."), workspace)]
    if tool == "web_fetch":
        return [(urlparse(str(inp.get("url", ""))).hostname or "").lower()]
    if tool == "web_search":
        return [str(inp.get("query", ""))]
    return []


def _patch_paths(patch: str) -> list[str]:
    out = []
    for line in patch.replace("\r\n", "\n").split("\n"):
        if line.startswith(("--- ", "+++ ")):
            name = line[4:].split("\t")[0].strip()
            if name != "/dev/null":
                out.append(name[2:] if name.startswith(("a/", "b/")) else name)
    return out or [""]


def _rel(path: str, workspace: Optional[Path]) -> str:
    p = str(path).replace("\\", "/")
    if workspace is not None:
        try:
            p = Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix() if Path(path).is_absolute() else p
        except ValueError:
            pass
    return str(PurePosixPath(p)) if p not in ("", ".") else "."


def _glob_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"[;|&\n]+", command) if s.strip()]


def _match_subject(tool: str, pattern: str, subject: str, *, strict: bool) -> bool:
    if tool == "run_shell":
        if strict:      # allow / ask: the whole command, and only a plain command
            return not _OPERATORS.search(subject) and fnmatch.fnmatchcase(subject.strip(), pattern)
        return any(fnmatch.fnmatchcase(seg, pattern) for seg in [subject.strip(), *_segments(subject)])
    if tool in ("web_fetch",):
        host = subject.lower()
        pat = pattern.lower()
        return fnmatch.fnmatchcase(host, pat) or host == pat.lstrip("*.")
    if tool == "web_search":
        return fnmatch.fnmatchcase(subject.lower(), pattern.lower())
    return bool(_glob_regex(pattern).match(subject))


def _tool_matches(rule: Rule, tool: str) -> bool:
    if rule.tool == "*" or rule.tool == tool:
        return True
    if rule.tool.startswith("class:"):
        return toolspec.spec_for(tool).permission == rule.tool[6:]
    return False


def rule_applies(rule: Rule, tool: str, subj: list[str]) -> bool:
    if not _tool_matches(rule, tool):
        return False
    if not rule.match:
        return True
    if not subj:
        return False
    strict = rule.decision != "deny"
    hits = [_match_subject(tool, rule.match, s, strict=strict) for s in subj]
    # deny/ask: touching one matching thing is enough. allow: every thing touched must match.
    return any(hits) if rule.decision in ("deny", "ask") else all(hits)


# ---- the decision -------------------------------------------------------------------
def decide(tool: str, tool_input: dict, *, rules: list[Rule], mode: str = "default", workspace: Optional[Path] = None,
           tainted: bool = False, allow_bypass: bool = False) -> Verdict:
    spec = toolspec.spec_for(tool)
    subj = subjects(tool, tool_input, workspace)
    is_admin = spec.permission == "admin"
    mode = mode if mode in MODES else "default"
    if mode == "bypass" and not allow_bypass:
        mode = "default"

    for rule in rules:
        if rule.decision == "deny" and rule_applies(rule, tool, subj):
            return Verdict("deny", rule.note or f"denied by rule ({rule.tool} {rule.match}".rstrip() + ")", "rule")
    changes_things = not spec.read_only and spec.needs_approval is not False
    if mode == "plan" and changes_things:
        return Verdict("deny", "plan mode: nothing may be changed or run until the plan is approved", "mode")
    for rule in rules:
        if rule.decision == "ask" and rule_applies(rule, tool, subj):
            return Verdict("ask", rule.note or "a rule asks for approval", "rule")
    if spec.always_ask:
        return Verdict("ask", "this tool hands something to a person, so a person is always asked", "tool")

    allowed, source = False, ""
    if not is_admin:
        for rule in rules:
            if rule.decision == "allow" and rule_applies(rule, tool, subj):
                allowed, source = True, "rule"
                break
        if not allowed and mode == "accept_edits" and tool in EDIT_TOOLS:
            allowed, source = True, "mode"
        if not allowed and mode == "bypass":
            allowed, source = True, "mode"
    if allowed:
        if tainted and changes_things:
            return Verdict("ask", "untrusted content (web or an untrusted tool) is in this conversation, so a person "
                                  "must approve changes", "taint")
        return Verdict("allow", "", source)
    if tainted and spec.permission in ("agent", "config") and not spec.read_only and spec.needs_approval is not False:
        # Delegating to a sub-agent or reconfiguring the agent would carry the untrusted content's
        # influence somewhere the taint does not follow, so a person must approve it.
        return Verdict("ask", "untrusted content is in this conversation, so a person must approve delegation "
                              "and configuration changes", "taint")
    return Verdict("default")


# ---- where rules and the mode come from ------------------------------------------------
# A caller (a sub-agent, an API request) may narrow the mode for one run; see effective().
mode_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("permission_mode", default=None)
_STRICTNESS = {"plan": 0, "default": 1, "accept_edits": 2, "bypass": 3}


def _config() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("permissions")) or {}
    except Exception:  # noqa: BLE001
        return {}


def is_locked() -> bool:
    return bool(_config().get("locked", False))


def instance_settings(instance_id: Optional[int]) -> dict:
    """{"mode": str | None, "rules": [raw rule dicts]} stored on the bot instance."""
    if instance_id is None:
        return {"mode": None, "rules": []}
    try:
        from bot import bot_instances

        inst = bot_instances.get_instance(instance_id)
    except Exception:  # noqa: BLE001
        inst = None
    stored = ((inst or {}).get("action_overrides") or {}).get("permissions") or {}
    return {"mode": stored.get("mode"), "rules": list(stored.get("rules") or [])}


def set_instance_settings(instance_id: int, *, mode: Optional[str] = None, rules: Optional[list] = None,
                          actor: str = "dashboard") -> dict:
    """Save an instance's permission mode and rules. Refused when the host has locked them."""
    from bot import bot_instances

    if is_locked():
        raise PermissionError("permissions are locked by the host configuration")
    if mode is not None and mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if rules is not None:
        problems = validate_rules(rules)
        if problems:
            raise ValueError("; ".join(problems))
    inst = bot_instances.get_instance(instance_id)
    if inst is None:
        raise KeyError(f"no bot instance {instance_id}")
    overrides = dict(inst.get("action_overrides") or {})
    stored = dict(overrides.get("permissions") or {})
    if mode is not None:
        stored["mode"] = mode
    if rules is not None:
        stored["rules"] = [dict(r) for r in rules]
    overrides["permissions"] = stored
    bot_instances.update_instance(instance_id, action_overrides=overrides, actor=actor)
    return instance_settings(instance_id)


def effective(instance_id: Optional[int]) -> tuple[str, list[Rule], bool]:
    """(mode, rules, allow_bypass) in force. Host rules always apply; per-instance rules are added
    unless the host locked permissions. A run-level mode may only make things stricter when locked."""
    cfg = _config()
    rules = parse_rules(cfg.get("rules"))
    mode = str(cfg.get("mode") or "default")
    if not is_locked():
        inst = instance_settings(instance_id)
        rules = rules + parse_rules(inst["rules"])
        mode = inst["mode"] or mode
    override = mode_var.get()
    if override in MODES and (not is_locked() or _STRICTNESS[override] <= _STRICTNESS.get(mode, 1)):
        mode = override
    if mode not in MODES:
        mode = "default"
    return mode, rules, bool(cfg.get("allow_bypass", False))


def evaluate(tool: str, tool_input: dict, *, workspace: Optional[Path], instance_id: Optional[int],
             session: str) -> Verdict:
    from bot.agent_runtime import taint

    mode, rules, allow_bypass = effective(instance_id)
    return decide(tool, tool_input, rules=rules, mode=mode, workspace=workspace,
                  tainted=taint.is_tainted(session), allow_bypass=allow_bypass)
