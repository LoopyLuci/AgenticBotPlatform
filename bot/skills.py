"""A local, file-backed skill store — reusable instructions/snippets the
agent loop can pull in on demand via the read_skill tool
(bot/agent_runtime/tools.py) instead of a networked marketplace.

This is a deliberately smaller scope than the real Hermes Agent's own
skills system (search/browse/audit against a hosted hub, install
approval workflow, bundles) — BotServer has no marketplace equivalent to
point that at. What's here is real and complete for what it is: drop a
skill file on disk, `/skills install <path>` registers its contents,
`/skills list` shows what's available, and the model can request the
full content of any of them by name mid-conversation via read_skill
rather than every skill's full text being stuffed into every system
prompt regardless of relevance.

A skill file's first line becomes its one-line description (shown in
/skills list and in the system-prompt summary every api-backend turn
gets); the rest is the content returned by read_skill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from bot import db

MAX_INSTALL_CHARS = 20000


class SkillError(Exception):
    pass


def install(instance_id: Optional[int], path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise SkillError(f"{path!r} does not exist or isn't a file")
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_INSTALL_CHARS:
        raise SkillError(f"{path!r} is too large ({len(text)} chars, max {MAX_INSTALL_CHARS})")
    lines = text.splitlines()
    description = (lines[0].strip().lstrip("#").strip() if lines else "") or "(no description)"
    name = p.stem
    db.install_skill(instance_id, name, description, text)
    return {"name": name, "description": description}


def remove(instance_id: Optional[int], name: str) -> bool:
    existing = db.get_skill(instance_id, name)
    if existing is None:
        return False
    db.delete_skill(instance_id, name)
    return True


def list_for_instance(instance_id: int) -> list[dict]:
    return [dict(r) for r in db.list_skills(instance_id)]


def get_content(instance_id: int, name: str) -> Optional[str]:
    row = db.get_skill(instance_id, name)
    return row["content"] if row else None


def summary(instance_id: int) -> str:
    """One line per available skill for the api backend's system prompt —
    empty string if none, so callers can always append it."""
    rows = list_for_instance(instance_id)
    if not rows:
        return ""
    lines = ["Available skills (use the read_skill tool to load one's full content by name):"]
    for r in rows:
        lines.append(f"- {r['name']}: {r['description']}")
    return "\n".join(lines)
