"""Shared, cross-instance markdown context — the one place a swarm of
worker agents and their manager keep a small set of named documents in
sync (a project-status board, architecture notes, whatever the swarm
actually needs), independent of any single instance's own conversation
history or per-instance memory/kanban state.

Deliberately small and simple: named markdown strings in one SQLite
table (bot/db.py's shared_context_docs), no versioning, no per-doc ACLs
— every registered instance can read every doc, and any instance can
write any doc (the same single-trusted-operator model this project's
agent_control already accepts for cross-instance calls). Kept small on
purpose (MAX_DOC_CHARS) so reading it stays cheap enough to do on every
turn if an agent wants to — this is meant to be read constantly, not
paged through.
"""

from __future__ import annotations

from typing import Optional

from bot import db

MAX_DOC_CHARS = 8000
MAX_DOCS = 50


class SharedContextError(Exception):
    pass


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise SharedContextError("doc name can't be empty")
    if len(name) > 80:
        raise SharedContextError("doc name too long (max 80 chars)")
    if not all(c.isalnum() or c in "-_." for c in name):
        raise SharedContextError("doc name may only contain letters, digits, '-', '_', '.'")
    return name


def read_doc(name: str) -> Optional[dict]:
    row = db.get_context_doc(_validate_name(name))
    return dict(row) if row else None


def write_doc(name: str, content: str, actor: str) -> dict:
    name = _validate_name(name)
    if len(content) > MAX_DOC_CHARS:
        raise SharedContextError(f"content too large ({len(content)} chars, max {MAX_DOC_CHARS}) — keep shared context concise")
    existing = {r["name"] for r in db.list_context_docs()}
    if name not in existing and len(existing) >= MAX_DOCS:
        raise SharedContextError(f"already at the {MAX_DOCS}-doc limit — delete an unused doc first")
    db.set_context_doc(name, content, actor)
    return read_doc(name)


def list_docs() -> list[dict]:
    return [dict(r) for r in db.list_context_docs()]


def delete_doc(name: str) -> bool:
    return db.delete_context_doc(_validate_name(name))
