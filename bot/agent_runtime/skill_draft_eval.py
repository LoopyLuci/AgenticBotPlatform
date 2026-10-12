"""An automatic, advisory check of a skill draft, shown next to it before a person decides (roadmap P4).

    GET /api/skills/drafts/{name}/evaluation        /skills check-draft <name>

It is deterministic and uses no model: the draft is only read, never changed, approved or rejected.
**The result is advice.** A person still approves or rejects; "pass" does not install anything.

Checks:
  * format      - front matter, name, a real description, instructions of a sensible length (the draft lint);
  * secrets     - the text contains nothing a secret scanner would redact;
  * scanner     - the skill-pack scanner's patterns (pipe-to-shell, decode-and-exec, credential folders, ...);
  * size        - within the skill budget (draft limit and the SKILL.md limit);
  * authority   - no instruction that tries to change permissions, approvals or the tool list, or to read credential files;
  * triggers    - the description matches the draft's own example tasks and does not match unrelated tasks.
"""

from __future__ import annotations

import re
from typing import Optional

from bot import skill_install, skill_packs
from bot.agent_runtime import skill_learning

# Tasks no real skill should claim. A description that matches these is too broad to be useful.
UNRELATED_TASKS = (
    "bake a chocolate cake for a birthday",
    "translate a poem into French",
    "book a flight to Lisbon",
    "tune an acoustic guitar",
)
# Token shapes a person would not want in a skill, on top of the secrets this installation knows by value.
_TOKEN_SHAPES = re.compile(r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
                           r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_STOP = frozenset("""a an the and or but for with from into onto this that these those when what which how you your use used using
    to of in on at by is are was be it its as if then than so do does not no can will should must may about after before over
    under any all each some more most other such via per out up down off new one two task tasks skill skills agent""".split())

# (id, regex, what it means). On top of the scanner's own patterns: things that try to widen the agent's authority.
_AUTHORITY = [(rx_id, re.compile(rx, re.I), why) for rx_id, rx, why in [
    ("permissions", r"\b(bypass|skip|disable|turn off|switch off|ignore)\b[^\n.]{0,40}\b(permissions?|approvals?|sandbox|confirmations?|guards?)\b",
     "tells the agent to bypass or disable permissions, approvals or the sandbox"),
    ("permissions", r"\b(auto-?approve|yolo|dangerously|bypass-?permissions?|allow all tools|grant (yourself|the agent)|elevate)\b",
     "tries to widen what the agent may do"),
    ("permissions", r"\b(edit|modify|change|overwrite|write to)\b[^\n.]{0,40}(permissions?\.(ya?ml|json)|backends\.yaml|\.claude[/\\]settings|settings\.json|allowed[-_ ]tools)",
     "tells the agent to edit permission or tool settings"),
    ("tools", r"\ballowed[-_]tools\s*:", "declares its own tool list in the body"),
    ("tools", r"\b(enable|unlock|use) (every|all|any) tools?\b", "asks for every tool"),
    ("credentials", r"(\.env\b|credentials?\.(json|ya?ml|txt)|secrets?\.(json|ya?ml|txt)|\.gnupg|keychain|/etc/shadow|\.git-credentials|api[_ -]?keys? (file|folder))",
     "refers to a credential or secrets file"),
    ("credentials", r"(~|\$HOME|%USERPROFILE%|C:\\Users\\[^\\\s]+)[/\\]\.[a-z]", "refers to a hidden folder in a home directory"),
]]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _STOP}


def _triggers(name: str, description: str, task: str) -> bool:
    """Would this skill's name and description be offered for `task`? Same idea as the pack search: shared content words."""
    return bool(_words(name.replace("-", " ") + " " + description) & _words(task))


def example_tasks(body: str) -> list[str]:
    """The draft's own example tasks: `Example: ...` / `e.g. ...` lines, and bullets under a heading that mentions examples or when to use."""
    found: list[str] = []
    under = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            under = bool(re.search(r"example|when to use|use (this )?when|trigger", s, re.I))
            continue
        m = re.match(r"^(?:[-*]\s*)?(?:example(?: task)?s?|e\.g\.?)\s*[:\-]\s*(.+)$", s, re.I)
        if m:
            found.append(m.group(1).strip(" \"'`"))
        elif under and re.match(r"^[-*\d.]+\s+\S", s):
            found.append(re.sub(r"^[-*\d.]+\s+", "", s).strip(" \"'`"))
    return found[:8]


def _check(id_: str, status: str, detail: str) -> dict:
    return {"id": id_, "status": status, "detail": detail}        # status: pass | warn | fail | skip


def evaluate_text(text: str) -> dict:
    """Evaluate draft text. Pure: touches nothing."""
    from bot.agent_runtime import secrets_guard
    from bot.agent_runtime.agent_defs import _split_front_matter

    checks: list[dict] = []
    meta, body, problem = _split_front_matter(text.strip())
    name = str(meta.get("name") or "").strip().lower()
    description = " ".join(str(meta.get("description") or "").split())
    body = body.strip()

    # format (the draft lint, re-run)
    _, problems = skill_learning.lint(text)
    checks.append(_check("format", "fail", "; ".join(problems)) if problems else _check("format", "pass", "front matter, name, description and length are fine"))

    # secrets (lint redacts known values, so look at the raw text; also token shapes)
    checks.append(_check("secrets", "fail", "the text contains something that looks like a secret")
                  if secrets_guard.redact(text) != text or _TOKEN_SHAPES.search(text)
                  else _check("secrets", "pass", "no secrets found"))

    # scanner patterns (block and warn)
    blocks = [why for sev, rx, why in skill_install._PATTERNS if sev == "block" and rx.search(text)]
    warns = [why for sev, rx, why in skill_install._PATTERNS if sev == "warn" and rx.search(text)]
    scripts = ["it contains a <script> tag"] if "<script" in text.lower() else []
    if blocks or scripts:
        checks.append(_check("scanner", "fail", "; ".join(blocks + scripts)))
    elif warns:
        checks.append(_check("scanner", "warn", "; ".join(warns)))
    else:
        checks.append(_check("scanner", "pass", "none of the skill scanner's patterns match"))

    # size
    sizes = []
    if len(body) > skill_learning.MAX_BODY_CHARS:
        sizes.append(f"instructions are {len(body)} characters (draft limit {skill_learning.MAX_BODY_CHARS})")
    if len(text) > skill_packs.MAX_SKILL_MD:
        sizes.append(f"the file is {len(text)} characters (SKILL.md limit {skill_packs.MAX_SKILL_MD})")
    checks.append(_check("size", "fail", "; ".join(sizes)) if sizes else _check("size", "pass", f"{len(body)} characters of instructions, within budget"))

    # authority: permission or tool changes and credential files (anything in the body or description)
    hits: dict[str, list[str]] = {}
    for kind, rx, why in _AUTHORITY:
        if rx.search(text):
            hits.setdefault(kind, []).append(why)
    for kind, label in (("permissions", "permissions"), ("tools", "tools"), ("credentials", "credentials")):
        if hits.get(kind):
            checks.append(_check(f"authority.{kind}", "fail", "; ".join(hits[kind])))
        else:
            checks.append(_check(f"authority.{kind}", "pass", f"nothing tries to change {label}" if kind != "credentials" else "no credential files mentioned"))

    # triggers
    examples = example_tasks(body)
    if not name or not description:
        checks.append(_check("triggers", "skip", "no name or description to test"))
    else:
        missed = [t for t in examples if not _triggers(name, description, t)]
        over = [t for t in UNRELATED_TASKS if _triggers(name, description, t)]
        if over:
            checks.append(_check("triggers", "fail", "the description also matches unrelated tasks: " + "; ".join(over)))
        elif missed:
            checks.append(_check("triggers", "fail", "the description does not match its own example: " + "; ".join(missed)))
        elif not examples:
            checks.append(_check("triggers", "warn", "the draft has no example tasks to test the description against "
                                 "(add lines such as 'Example: ...'); it matches none of the unrelated test tasks"))
        else:
            checks.append(_check("triggers", "pass", f"the description matches all {len(examples)} example task(s) and none of the unrelated ones"))

    statuses = {c["status"] for c in checks}
    verdict = "fail" if "fail" in statuses else "review" if "warn" in statuses else "pass"
    return {"name": name, "verdict": verdict, "advisory": True, "examples": examples, "checks": checks,
            "note": "Advice only. Nothing is approved or rejected by this check; a person decides."}


def evaluate_draft(name: str) -> dict:
    """Evaluate a stored draft. Read-only; raises ToolError for an unknown draft."""
    draft = skill_learning._draft(name)
    out = evaluate_text(str(draft.get("text") or ""))
    out["name"] = out["name"] or name
    return out


def summary(result: Optional[dict]) -> str:
    if not result:
        return ""
    lines = [f"Check of draft {result['name']}: {result['verdict'].upper()} (advice only)"]
    lines += [f"  [{c['status']}] {c['id']}: {c['detail']}" for c in result["checks"]]
    return "\n".join(lines)
