"""Conversation sessions, chat-session links, agent message history, and deleting/exporting a session's data.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bot import db as _db


def _get_or_create_session(
    conn: sqlite3.Connection,
    instance_id: Optional[int],
    chat_id: Optional[Any],
    first_text: str = "",
) -> Optional[int]:
    """Buckets activity for (instance_id, chat_id) into a session row, reusing
    the most recent one if it's still "current" (last activity within
    SESSION_GAP_MINUTES). Must be called with `_lock` already held by the
    caller — this issues its own execute() calls on the shared connection,
    not a separate one, to stay inside the caller's transaction.

    Returns None when instance_id is unset (pre-multi-instance/"legacy"
    activity) — nothing meaningful to bucket a session under."""
    if instance_id is None:
        return None
    chat_key = str(chat_id) if chat_id is not None else None
    now = _db._now()
    row = conn.execute(
        "SELECT id, last_activity_at FROM sessions WHERE instance_id=? AND "
        "(chat_id=? OR (chat_id IS NULL AND ? IS NULL)) ORDER BY last_activity_at DESC LIMIT 1",
        (instance_id, chat_key, chat_key),
    ).fetchone()
    if row:
        last = datetime.fromisoformat(row["last_activity_at"])
        if datetime.now(timezone.utc) - last <= timedelta(minutes=_db.SESSION_GAP_MINUTES):
            conn.execute(
                "UPDATE sessions SET last_activity_at=?, item_count=item_count+1 WHERE id=?",
                (now, row["id"]),
            )
            return row["id"]
    title = (first_text or "").strip().replace("\n", " ")[:60] or "New session"
    cur = conn.execute(
        "INSERT INTO sessions (instance_id, chat_id, title, started_at, last_activity_at, item_count) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (instance_id, chat_key, title, now, now),
    )
    return cur.lastrowid


def get_session(session_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def list_sessions(
    instance_id: Optional[int] = None,
    q: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    clauses = []
    params: list[Any] = []
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
    if q:
        clauses.append("title LIKE ?")
        params.append(f"%{q}%")
    if since:
        clauses.append("last_activity_at>=?")
        params.append(since)
    if until:
        clauses.append("last_activity_at<=?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM sessions {where} ORDER BY last_activity_at DESC LIMIT ?", params
    ).fetchall()


def link_chat_session(
    instance_id: int, chat_id: Any, key: str, title: Optional[str] = None, thread_id: Optional[Any] = None
) -> int:
    """Archives whichever chat_sessions row is currently active for this
    (instance_id, chat_id, thread_id) and inserts a fresh active row
    pointing at `key`. Used by both /new (a freshly created backend key)
    and /resume (an old key pulled back out of history) — the only
    difference between them is where `key` came from. thread_id is a
    Telegram forum-topic id (see /topic) — None means the chat's root
    session, same as before that feature existed, so every non-topic chat
    is unaffected."""
    conn = _db.get_conn()
    now = _db._now()
    tid = str(thread_id) if thread_id is not None else None
    with _db._lock:
        conn.execute(
            "UPDATE chat_sessions SET archived_at=? WHERE instance_id=? AND chat_id=? AND thread_id IS ? AND archived_at IS NULL",
            (now, instance_id, str(chat_id), tid),
        )
        cur = conn.execute(
            "INSERT INTO chat_sessions (instance_id, chat_id, thread_id, desktop_session_key, title, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (instance_id, str(chat_id), tid, key, title, now, now),
        )
        conn.commit()
        return cur.lastrowid


def get_active_chat_session(instance_id: int, chat_id: Any, thread_id: Optional[Any] = None) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    tid = str(thread_id) if thread_id is not None else None
    return conn.execute(
        "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? AND thread_id IS ? AND archived_at IS NULL",
        (instance_id, str(chat_id), tid),
    ).fetchone()


def touch_active_chat_session(instance_id: int, chat_id: Any, thread_id: Optional[Any] = None) -> None:
    conn = _db.get_conn()
    tid = str(thread_id) if thread_id is not None else None
    with _db._lock:
        conn.execute(
            "UPDATE chat_sessions SET last_used_at=? WHERE instance_id=? AND chat_id=? AND thread_id IS ? AND archived_at IS NULL",
            (_db._now(), instance_id, str(chat_id), tid),
        )
        conn.commit()


def get_chat_session(chat_session_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM chat_sessions WHERE id=?", (chat_session_id,)).fetchone()


def list_chat_sessions(
    instance_id: int, chat_id: Optional[Any] = None, limit: int = 20, thread_id: Optional[Any] = None
) -> list[sqlite3.Row]:
    """Every link ever made for this instance (optionally narrowed to one
    chat, and further to one forum topic within it) — active one first
    (it's always most recent), then history."""
    conn = _db.get_conn()
    if chat_id is not None and thread_id is not None:
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? AND thread_id IS ? ORDER BY created_at DESC LIMIT ?",
            (instance_id, str(chat_id), str(thread_id), limit),
        ).fetchall()
    if chat_id is not None:
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? ORDER BY created_at DESC LIMIT ?",
            (instance_id, str(chat_id), limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM chat_sessions WHERE instance_id=? ORDER BY created_at DESC LIMIT ?",
        (instance_id, limit),
    ).fetchall()


def list_topic_sessions(instance_id: int, chat_id: Any) -> list[sqlite3.Row]:
    """Every forum topic in this group that has its own active session —
    for /topic list."""
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM chat_sessions WHERE instance_id=? AND chat_id=? AND thread_id IS NOT NULL AND archived_at IS NULL "
        "ORDER BY last_used_at DESC",
        (instance_id, str(chat_id)),
    ).fetchall()


def set_chat_session_title(chat_session_id: int, title: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE chat_sessions SET title=? WHERE id=?", (title, chat_session_id))
        conn.commit()


def append_agent_message(session_key: str, role: str, content: Any) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO agent_messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_key, role, json.dumps(content), _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_agent_messages(session_key: str, limit: int = 2000) -> list[dict]:
    """Oldest-first, ready to feed straight into the Anthropic Messages API
    as the `messages` list (each row's content is already in that shape —
    see agent_messages' schema comment)."""
    conn = _db.get_conn()
    # The NEWEST `limit` messages, oldest first. (It used to return the first 200, so a long agent session
    # silently lost its most recent turns.)
    rows = conn.execute(
        "SELECT role, content FROM agent_messages WHERE session_key=? ORDER BY id DESC LIMIT ?",
        (session_key, limit),
    ).fetchall()
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]


def clear_agent_messages(session_key: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM agent_messages WHERE session_key=?", (session_key,))
        conn.commit()


def compress_agent_messages(session_key: str, *, keep_last_n: int, digest_role: str, digest_content: Any) -> None:
    """Replaces every message OLDER than the last [keep_last_n] with one
    synthetic digest entry — see bot/agent_runtime/compression.py. Rows
    are re-inserted (not updated in place) so the new AUTOINCREMENT ids
    stay in the correct chronological order: SQLite's id counter only
    ever increases, so simply deleting the old rows and inserting the
    digest afterward would give the digest a HIGHER id than the kept
    "recent" rows it's meant to precede — re-inserting the kept rows
    after it is what keeps ORDER BY id ASC correct post-compression."""
    conn = _db.get_conn()
    with _db._lock:
        rows = conn.execute(
            "SELECT role, content FROM agent_messages WHERE session_key=? ORDER BY id ASC", (session_key,)
        ).fetchall()
        to_keep = rows[-keep_last_n:] if keep_last_n > 0 else []
        conn.execute("DELETE FROM agent_messages WHERE session_key=?", (session_key,))
        now = _db._now()
        conn.execute(
            "INSERT INTO agent_messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_key, digest_role, json.dumps(digest_content), now),
        )
        for row in to_keep:
            conn.execute(
                "INSERT INTO agent_messages (session_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_key, row["role"], row["content"], now),
            )
        conn.commit()


def count_legacy_items(instance_id: int) -> int:
    """Rows predating the sessions feature (session_id IS NULL) for one
    instance — surfaced as a synthetic "Before sessions" bucket rather than
    backfilled, since bucketing thousands of historical rows on upgrade is a
    materially riskier migration than this file's usual additive ALTERs."""
    conn = _db.get_conn()
    msgs = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE instance_id=? AND session_id IS NULL", (instance_id,)
    ).fetchone()["c"]
    jobs = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE instance_id=? AND session_id IS NULL", (instance_id,)
    ).fetchone()["c"]
    return msgs + jobs


def get_legacy_items(instance_id: int) -> dict[str, list[sqlite3.Row]]:
    conn = _db.get_conn()
    return {
        "messages": conn.execute(
            "SELECT * FROM messages WHERE instance_id=? AND session_id IS NULL ORDER BY id ASC",
            (instance_id,),
        ).fetchall(),
        "jobs": conn.execute(
            "SELECT * FROM jobs WHERE instance_id=? AND session_id IS NULL ORDER BY id ASC",
            (instance_id,),
        ).fetchall(),
    }


def get_session_items(session_id: int) -> dict[str, list[sqlite3.Row]]:
    conn = _db.get_conn()
    return {
        "messages": conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall(),
        "jobs": conn.execute(
            "SELECT * FROM jobs WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall(),
    }


def _delete_attachment_files(rows) -> None:
    """Best-effort cleanup of the attachment/thumbnail files backing a set
    of message rows about to be deleted — avoids leaving orphaned files
    under data/attachments behind every chat delete. Never raises: a
    missing or already-gone file just means there's nothing to clean up."""
    from bot import attachments

    for row in rows:
        keys = row.keys() if hasattr(row, "keys") else row
        for col, root in (("attachment_path", attachments.ATTACHMENTS_DIR), ("thumbnail_path", attachments.THUMBS_DIR)):
            rel = row[col] if col in keys and row[col] else None
            if not rel:
                continue
            try:
                full = (root / rel).resolve()
                if full.is_relative_to(root.resolve()) and full.is_file():
                    full.unlink()
            except OSError:
                pass


def delete_session(session_id: int) -> bool:
    """Permanently deletes one Sessions-tab bucket and every message/job
    filed under it (plus their attachment files). Returns False if the
    session doesn't exist."""
    conn = _db.get_conn()
    with _db._lock:
        if conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
            return False
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM messages WHERE session_id=?", (session_id,)
        ).fetchall()
        _db._delete_attachment_files(msg_rows)
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()
        return True


def clear_legacy_items(instance_id: int) -> int:
    """Deletes every pre-sessions-feature message/job for `instance_id`
    (session_id IS NULL — the "Before sessions" bucket) plus their
    attachment files. Returns the number of messages removed."""
    conn = _db.get_conn()
    with _db._lock:
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM messages WHERE instance_id=? AND session_id IS NULL",
            (instance_id,),
        ).fetchall()
        _db._delete_attachment_files(msg_rows)
        cur = conn.execute("DELETE FROM messages WHERE instance_id=? AND session_id IS NULL", (instance_id,))
        conn.execute("DELETE FROM jobs WHERE instance_id=? AND session_id IS NULL", (instance_id,))
        conn.commit()
        return cur.rowcount


def export_session_data(session_id: int) -> Optional[dict]:
    """One session's full backup document — metadata plus every message
    and job filed under it, plain-dict rows ready to JSON-serialize."""
    session = _db.get_session(session_id)
    if session is None:
        return None
    items = _db.get_session_items(session_id)
    return {
        "session": dict(session),
        "messages": [dict(r) for r in items["messages"]],
        "jobs": [dict(r) for r in items["jobs"]],
    }


def export_legacy_data(instance_id: int) -> dict:
    items = _db.get_legacy_items(instance_id)
    return {
        "session": {"id": f"legacy-{instance_id}", "instance_id": instance_id, "title": "Before sessions"},
        "messages": [dict(r) for r in items["messages"]],
        "jobs": [dict(r) for r in items["jobs"]],
    }


def delete_chat_messages(instance_id: int, chat_id: Any, platform: Optional[str] = None) -> int:
    """Deletes every logged message for one Chat-tab conversation (a given
    bot instance + platform-native chat id), plus their attachment files.
    Scoped to `messages` only — the Sessions/Jobs history for that same
    chat is a separate view (Sessions tab) and is left untouched, matching
    exactly what the Chat tab itself displays. Returns the number removed."""
    conn = _db.get_conn()
    with _db._lock:
        clauses = ["instance_id=?", "chat_id=?"]
        params: list[Any] = [instance_id, str(chat_id)]
        if platform is not None:
            clauses.append("platform=?")
            params.append(platform)
        where = " AND ".join(clauses)
        msg_rows = conn.execute(f"SELECT attachment_path, thumbnail_path FROM messages WHERE {where}", params).fetchall()
        _db._delete_attachment_files(msg_rows)
        cur = conn.execute(f"DELETE FROM messages WHERE {where}", params)
        conn.commit()
        return cur.rowcount


def delete_instance_messages(instance_id: int) -> int:
    """Deletes every logged message for one bot instance across every chat
    it's ever talked to, plus their attachment files — the Chat tab shows
    one merged timeline per instance regardless of chat_id, so "clear this
    bot's history" means all of it, not one chat_id. Returns the number
    removed."""
    conn = _db.get_conn()
    with _db._lock:
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM messages WHERE instance_id=?", (instance_id,)
        ).fetchall()
        _db._delete_attachment_files(msg_rows)
        cur = conn.execute("DELETE FROM messages WHERE instance_id=?", (instance_id,))
        conn.commit()
        return cur.rowcount


def export_instance_messages_data(instance_id: int) -> list[dict]:
    conn = _db.get_conn()
    rows = conn.execute("SELECT * FROM messages WHERE instance_id=? ORDER BY id ASC", (instance_id,)).fetchall()
    return [dict(r) for r in rows]


def export_chat_messages_data(instance_id: int, chat_id: Any, platform: Optional[str] = None) -> list[dict]:
    conn = _db.get_conn()
    clauses = ["instance_id=?", "chat_id=?"]
    params: list[Any] = [instance_id, str(chat_id)]
    if platform is not None:
        clauses.append("platform=?")
        params.append(platform)
    where = " AND ".join(clauses)
    rows = conn.execute(f"SELECT * FROM messages WHERE {where} ORDER BY id ASC", params).fetchall()
    return [dict(r) for r in rows]
