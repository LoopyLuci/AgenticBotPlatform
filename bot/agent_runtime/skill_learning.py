"""Turning a solved task into a skill draft - for a person to review (roadmap P4).

    native_agent:
      skill_learning:
        enabled: false          # OFF by default: it costs one extra model call after a long task
        min_tool_calls: 8       # only a task that took real work is worth learning from

After a turn that ends normally having used at least `min_tool_calls` tools, the agent is asked,
once and with no tools: "did this reveal a reusable procedure? If so write it as a SKILL.md,
otherwise say NONE". A reply that is a well-formed skill becomes a **draft**.

**A draft is never a skill until a person approves it.** Nothing self-installs, and drafts are
not shown to the model. Before it is even offered for review a draft is checked: valid front
matter and name, a real description, a size limit, no scripts or code that would trip the skill
scanner (skill_install.py), secrets removed, and a note if it resembles a skill you already
have. Approve or reject with `/skills drafts`, `/skills approve-draft <name>` and
`/skills reject-draft <name>` (or `/api/skills/drafts`).

The draft is text the model wrote from a conversation that may have included untrusted content
(a web page, a file from a repository), so approval is the moment a person checks that it
contains a procedure and not someone else's instructions.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from bot import skill_install, skill_packs
from bot.agent_runtime.errors import ToolError
from bot.agent_runtime.state import state_dir

MAX_BODY_CHARS = 6000
MAX_TRANSCRIPT_CHARS = 14_000
SIMILAR_OVERLAP = 0.7
ASK = (
    "You have just finished a task. Decide whether it revealed a reusable procedure - a sequence of steps, a set of "
    "conventions or a set of pitfalls - that would help you or another agent do a similar task faster or more reliably "
    "next time. Be strict: most tasks reveal nothing worth saving.\n\n"
    "If nothing is worth saving, reply with exactly: NONE\n\n"
    "Otherwise reply with ONLY a skill file in this exact format (no commentary before or after):\n"
    "---\nname: short-lowercase-hyphenated-name\ndescription: One or two sentences on WHEN to use this and what it does.\n---\n"
    "Concise, general instructions in Markdown: the steps, the reasons, the things that went wrong. Do not include this "
    "conversation's specifics (names, secrets, one-off paths). Do not include scripts or shell pipelines.\n\n"
    "The task, in brief:\n"
)


def _cfg() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("skill_learning")) or {}
    except Exception:  # noqa: BLE001
        return {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def min_tool_calls() -> int:
    try:
        return max(1, int(_cfg().get("min_tool_calls", 8)))
    except (TypeError, ValueError):
        return 8


def drafts_dir() -> Path:
    return state_dir("skill_drafts")


def lint(text: str) -> tuple[Optional[dict], list[str]]:
    """(parsed draft or None, problems). A draft with problems is rejected before a person sees it."""
    from bot.agent_runtime.agent_defs import _split_front_matter
    from bot.agent_runtime import secrets_guard

    text = secrets_guard.redact(text.strip())
    meta, body, problem = _split_front_matter(text)
    problems = [problem] if problem else []
    name = str(meta.get("name") or "").strip().lower()
    if not skill_packs.NAME_RE.match(name):
        problems.append("the name must be lowercase letters, digits and hyphens")
    description = " ".join(str(meta.get("description") or "").split())
    if len(description) < 20:
        problems.append("the description is missing or too short to say when to use the skill")
    body = body.strip()
    if len(body) < 40:
        problems.append("the instructions are empty or too short")
    if len(body) > MAX_BODY_CHARS:
        problems.append(f"the instructions are longer than {MAX_BODY_CHARS} characters")
    for severity, rx, why in skill_install._PATTERNS:
        if severity == "block" and rx.search(text):
            problems.append(f"it {why}")
    if "<script" in text.lower():
        problems.append("it contains a <script> tag")
    if problems:
        return None, problems
    similar = []
    for s in skill_packs.discover(None).values():
        a, b = set(re.findall(r"[a-z]{4,}", description.lower())), set(re.findall(r"[a-z]{4,}", s.description.lower()))
        if s.name == name or (a and b and len(a & b) / len(a | b) >= SIMILAR_OVERLAP):
            similar.append(s.name)
    return {"name": name, "description": description, "text": text, "similar_to": similar}, []


def save_draft(text: str, *, session: str = "", run_id: str = "") -> Optional[dict]:
    parsed, problems = lint(text)
    if parsed is None:
        return None
    path = drafts_dir() / f"{parsed['name']}.json"
    parsed.update(created=time.time(), session=session, run=run_id)
    path.write_text(json.dumps(parsed, indent=1), encoding="utf-8")
    return parsed


def list_drafts() -> list[dict]:
    out = []
    for f in sorted(drafts_dir().glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _draft(name: str) -> dict:
    if not re.match(r"^[a-z0-9-]{1,64}$", name):
        raise ToolError("that is not a draft name")
    f = drafts_dir() / f"{name}.json"
    if not f.is_file():
        raise ToolError(f"no draft named {name!r}")
    return json.loads(f.read_text(encoding="utf-8"))


def approve_draft(name: str) -> dict:
    """A person approves: the draft becomes a skill pack in the user's own folder."""
    d = _draft(name)
    root = skill_packs.user_root()
    if root is None:
        raise ToolError("no folder to install skills into")
    dest = root / d["name"]
    if dest.exists():
        raise ToolError(f"you already have a skill named {d['name']!r}")
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(d["text"].strip() + "\n", encoding="utf-8")
    (drafts_dir() / f"{name}.json").unlink(missing_ok=True)
    return {"installed": d["name"], "path": str(dest)}


def reject_draft(name: str) -> bool:
    _draft(name)
    (drafts_dir() / f"{name}.json").unlink(missing_ok=True)
    return True


async def maybe_draft(*, transport, model: str, history: list, session: str, tool_calls: int, run_id: str = "",
                      timeout_s: float = 60.0) -> Optional[dict]:
    """Called after a turn finished normally. Never raises."""
    if not enabled() or tool_calls < min_tool_calls():
        return None
    try:
        from bot.agent_runtime.compression import _render_transcript

        transcript = _render_transcript(history)[-MAX_TRANSCRIPT_CHARS:]
        request = transport.user_message(ASK + transcript)
        response = await transport.send(model=model, history=[request], tool_schemas=[], max_tokens=1500, timeout_s=timeout_s)
        reply = (response.text or "").strip()
        if not reply or reply.upper().startswith("NONE"):
            return None
        reply = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", reply)
        return save_draft(reply, session=session, run_id=run_id)
    except Exception:  # noqa: BLE001 - learning is a bonus; it must never affect the turn
        return None
