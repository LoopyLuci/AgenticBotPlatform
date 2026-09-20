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


KINDS = ("user", "feedback", "project", "reference", "fact")
_KIND_TITLES = {
    "user": "About the user", "feedback": "How to work with them (their corrections and preferences)",
    "project": "About the work", "reference": "Where to find things", "fact": "Other things to remember",
}
DUPLICATE_OVERLAP = 0.85       # share of words two memories must have in common to count as the same one
DECAY_DAYS = 180.0


def _norm(text: str) -> str:
    text = text.replace("'", "").replace("’", "")          # user's and users are the same word
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def _numbers(normalised: str) -> list[str]:
    return [t for t in normalised.split() if any(c.isdigit() for c in t)]


def _age_days(row) -> float:
    import datetime as _dt

    stamp = (row["last_used"] if "last_used" in row.keys() else None) or row["created_at"]
    try:
        then = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (_dt.datetime.now(_dt.timezone.utc) - then).total_seconds() / 86400)
    except ValueError:
        return 0.0


def _score(row) -> float:
    """How readily a memory earns a place in the prompt: fresh and re-confirmed ones first. Old ones fade
    from the prompt but are never deleted - only a person deletes a memory."""
    import math

    uses = row["uses"] if "uses" in row.keys() else 0
    return math.exp(-_age_days(row) / DECAY_DAYS) + 0.1 * min(int(uses or 0), 10)


def find_duplicate(instance_id: int, content: str) -> Optional[dict]:
    """An existing pending or approved memory that says the same thing, if any."""
    target = _norm(content)
    if not target:
        return None
    words = set(target.split())
    for row in db.list_memory_entries(instance_id):
        if row["status"] not in ("pending", "approved"):
            continue
        other = _norm(row["content"])
        if other == target:
            return dict(row)
        # Near-identical wording counts as the same memory - but never when the numbers differ
        # ("the port is 8080" and "the port is 8081" are different facts), and never when a single
        # word of a short sentence differs ("notes about billing" vs "notes about invoices").
        other_words = set(other.split())
        overlap = len(words & other_words) / max(len(words | other_words), 1)
        if _numbers(target) == _numbers(other) and overlap >= DUPLICATE_OVERLAP:
            return dict(row)
    return None


def remember_full(instance_id: int, content: str, source: str = "user", kind: str = "fact") -> dict:
    """Record a memory. Returns {"id", "approved", "duplicate", "kind"}. Saying something again does not
    create a second copy: the existing memory is refreshed instead, which keeps it from fading."""
    content = content.strip()
    kind = kind if kind in KINDS else "fact"
    existing = find_duplicate(instance_id, content)
    if existing is not None:
        db.touch_memory_entry(existing["id"])
        return {"id": existing["id"], "approved": existing["status"] == "approved", "duplicate": True,
                "kind": existing.get("kind", "fact")}
    approved = not approval_required(instance_id)
    entry_id = db.create_memory_entry(instance_id, content, source=source, status="approved" if approved else "pending",
                                      kind=kind)
    return {"id": entry_id, "approved": approved, "duplicate": False, "kind": kind}


def remember(instance_id: int, content: str, source: str = "user", kind: str = "fact") -> tuple[int, bool]:
    """Records a memory. Returns (id, approved) — approved is True and the entry is immediately live if the
    gate is off, otherwise it's pending and waits for /memory approve <id>."""
    result = remember_full(instance_id, content, source=source, kind=kind)
    return result["id"], result["approved"]


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


def forget(instance_id: int, entry_id: int) -> bool:
    """Delete a memory outright. Only a person calls this (a command or the dashboard); the agent has no tool for it."""
    return db.delete_memory_entry(entry_id, instance_id)


def pending(instance_id: int) -> list[dict]:
    return [dict(r) for r in db.list_memory_entries(instance_id, status="pending")]


def listing(instance_id: int, kind: Optional[str] = None) -> list[dict]:
    rows = [dict(r) for r in db.list_memory_entries(instance_id, status="approved")]
    return [r for r in rows if kind is None or r.get("kind") == kind]


def approved_summary(instance_id: int) -> str:
    """The approved memories for the system prompt, grouped by kind, freshest and most-confirmed first,
    within a size budget — empty string if there are none."""
    rows = db.list_memory_entries(instance_id, status="approved")
    if not rows:
        return ""
    ranked = sorted(rows, key=_score, reverse=True)
    chosen, used = [], 0
    for r in ranked[:MAX_SUMMARY_ENTRIES]:
        cost = len(r["content"]) + 4
        if used + cost > MAX_SUMMARY_CHARS and chosen:
            break
        chosen.append(r)
        used += cost
    lines = ["Long-term memory (things you've been told to remember across sessions):"]
    for kind in KINDS:
        group = [r for r in chosen if (r["kind"] if "kind" in r.keys() else "fact") == kind]
        if group:
            lines.append(f"{_KIND_TITLES[kind]}:")
            lines.extend(f"- {r['content']}" for r in group)
    if len(chosen) < len(rows):
        lines.append(f"({len(rows) - len(chosen)} older memories are not shown; session_search can find past conversations.)")
    return "\n".join(lines)
