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


SEED_DOC_NAME = "how-to-use-context"

# A worked example, not just a description — an agent encountering this
# system cold (via list_project_context) sees the exact tool calls to
# make, not just prose about what the feature is for.
SEED_DOC_CONTENT = """# How to use shared project context

This is a shared, cross-instance markdown store. Any registered agent —
Claude via MCP, an `api`-backend agent via its own tools, or a Hermes
agent with swarm tools enabled — can read and write these documents to
stay in sync with every other agent working on the same project.

## Tools

- `list_project_context()` — see what docs exist, their size, and who
  last touched them.
- `read_project_context(name)` — read one doc's full content.
- `write_project_context(name, content)` — create or **replace** a doc.
  Writing always overwrites the whole document (no partial-append), so
  include everything that should survive, not just what changed.

## Conventions

- Keep each doc small (under 8KB) and focused on one concern — this is
  meant to be read on every turn cheaply, not a general file store or a
  place to dump logs.
- A `status` doc is the most common use: a manager posts an update after
  assigning work, a worker posts an update when it finishes a piece, so
  everyone reads the same current state instead of stale assumptions.
- Prefer several small docs (`status`, `architecture`, `decisions`) over
  one giant one — cheaper for an agent to read only what it actually
  needs right now.

## Worked example

A manager assigning work might write:

    write_project_context("status", '''# Status

- Manager: dispatched 3 subtasks to workers A/B/C
- Worker A: done, wrote src/foo.py
- Worker B: in progress
- Worker C: blocked on missing API key
''')

Any other agent then calls `read_project_context("status")` to see that
exact update — no need to ask the manager directly or guess.

(This doc itself is a seed example, safe to overwrite once you have real
project content to put here instead.)
"""


def seed_default_docs() -> None:
    """Writes the how-to-use-context example doc if — and only if — it
    doesn't already exist. Called once at db.init_db() time (see that
    function) so a fresh install always has a working example for an
    agent to discover via list_project_context, without ever clobbering
    a doc an operator or agent has since customized or deleted on
    purpose (existence, not content, is the only thing checked)."""
    if read_doc(SEED_DOC_NAME) is not None:
        return
    write_doc(SEED_DOC_NAME, SEED_DOC_CONTENT, actor="system")
