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

import re
from pathlib import Path
from typing import Optional

from bot import db

MAX_INSTALL_CHARS = 20000

# Same shape a file's stem naturally produces (install() derives name from
# Path.stem) — enforced explicitly for create(), which has no file to
# derive a safe name from, so a caller-supplied name needs its own check.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SkillError(Exception):
    pass


def _description_from(text: str) -> str:
    lines = text.splitlines()
    return (lines[0].strip().lstrip("#").strip() if lines else "") or "(no description)"


def install(instance_id: Optional[int], path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise SkillError(f"{path!r} does not exist or isn't a file")
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_INSTALL_CHARS:
        raise SkillError(f"{path!r} is too large ({len(text)} chars, max {MAX_INSTALL_CHARS})")
    description = _description_from(text)
    name = p.stem
    db.install_skill(instance_id, name, description, text)
    return {"name": name, "description": description}


def create(
    instance_id: Optional[int], name: str, description: str, content: str, *, global_: bool = False
) -> dict:
    """Registers a skill directly from text — no file-path prerequisite,
    unlike install(). This is what lets an agent author a new skill
    on the spot (from what it just learned, or what the user asked for)
    without the awkward write_file-into-workspace-then-install_skill
    two-step that was the only path before this existed. Skills are
    inert descriptive text, never executed, so this carries none of
    install_plugin's code-execution risk and needs no approval gate.
    `global_=True` makes it visible to every instance (db.install_skill's
    existing instance_id=None convention) — callers exposing this to an
    agent must apply their own cross-instance trust check first (see
    agent_control.py), same as update_agent_config's cross-instance
    guard."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise SkillError(f"{name!r} isn't a valid skill name — use letters, digits, _ or - only (max 64 chars)")
    content = content or ""
    if len(content) > MAX_INSTALL_CHARS:
        raise SkillError(f"content is too large ({len(content)} chars, max {MAX_INSTALL_CHARS})")
    description = (description or "").strip() or _description_from(content)
    target_instance_id = None if global_ else instance_id
    db.install_skill(target_instance_id, name, description, content)
    return {"name": name, "description": description, "global": global_}


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
