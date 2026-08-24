"""Long-term memory — facts the agent loop (or a human) wants remembered
across sessions, gated behind a pending/approve/reject review unless the
gate is switched off for that instance. Approved entries get folded into
the api backend's system prompt on every turn (see approved_summary(),
used by bot/backends/api_backend.py) — this is the persistent-knowledge
counterpart to per-session conversation history (bot/db.py's
agent_messages), which resets on every /new.

The approval gate itself is a per-instance setting stored in the existing
bot_instances.action_overrides JSON blob (key "memory_approval", default
True) rather than a new column — same pattern bot/slash_access.py uses
for its own per-instance config.
"""

from __future__ import annotations

from typing import Optional

from bot import bot_instances, db

MAX_SUMMARY_ENTRIES = 30
MAX_SUMMARY_CHARS = 4000


def approval_required(instance_id: int) -> bool:
    instance = bot_instances.get_instance(instance_id)
    if instance is None:
        return True
    return bool((instance.get("action_overrides") or {}).get("memory_approval", True))


def set_approval_required(instance_id: int, required: bool, actor: str) -> None:
    instance = bot_instances.get_instance(instance_id)
    if instance is None:
        return
    overrides = dict(instance.get("action_overrides") or {})
    overrides["memory_approval"] = required
    bot_instances.update_instance(instance_id, action_overrides=overrides, actor=actor)


def remember(instance_id: int, content: str, source: str = "user") -> tuple[int, bool]:
    """Records a memory. Returns (id, approved) — approved is True and the
    entry is immediately live if the gate is off, otherwise it's pending
    and waits for /memory approve <id>."""
    content = content.strip()
    if approval_required(instance_id):
        entry_id = db.create_memory_entry(instance_id, content, source=source, status="pending")
        return entry_id, False
    entry_id = db.create_memory_entry(instance_id, content, source=source, status="approved")
    return entry_id, True


def approve(entry_id: int) -> Optional[dict]:
    row = db.get_memory_entry(entry_id)
    if row is None or row["status"] != "pending":
        return None
    db.resolve_memory_entry(entry_id, "approved")
    return dict(row)


def reject(entry_id: int) -> Optional[dict]:
    row = db.get_memory_entry(entry_id)
    if row is None or row["status"] != "pending":
        return None
    db.resolve_memory_entry(entry_id, "rejected")
    return dict(row)


def pending(instance_id: int) -> list[dict]:
    return [dict(r) for r in db.list_memory_entries(instance_id, status="pending")]


def approved_summary(instance_id: int) -> str:
    """A short bullet list of approved memories for the api backend's
    system prompt — empty string if there are none, so callers can just
    always append it without a special case."""
    rows = db.list_memory_entries(instance_id, status="approved")[:MAX_SUMMARY_ENTRIES]
    if not rows:
        return ""
    lines = ["Long-term memory (things you've been told to remember across sessions):"]
    for r in rows:
        lines.append(f"- {r['content']}")
    text = "\n".join(lines)
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS] + "\n… (truncated)"
    return text
