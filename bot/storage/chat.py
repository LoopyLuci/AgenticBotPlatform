"""Platform chat messages.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from bot import db as _db


def log_message(
    chat_id: Any,
    direction: str,
    source: str,
    text: str,
    platform: str = "telegram",
    user_id: Optional[Any] = None,
    username: str = "",
    instance_id: Optional[int] = None,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
    attachment_mime: Optional[str] = None,
    attachment_size: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
) -> int:
    conn = _db.get_conn()
    with _db._lock:
        session_id = _db._get_or_create_session(conn, instance_id, chat_id, text)
        cur = conn.execute(
            "INSERT INTO messages (ts, platform, chat_id, user_id, username, direction, source, text, instance_id, "
            "attachment_path, attachment_name, attachment_mime, attachment_size, thumbnail_path, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_db._now(), platform, str(chat_id), str(user_id) if user_id is not None else None, username, direction, source, text, instance_id,
             attachment_path, attachment_name, attachment_mime, attachment_size, thumbnail_path, session_id),
        )
        conn.commit()
        message_id = cur.lastrowid
    _db._notify_message_logged(message_id)
    return message_id


def get_message(message_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()


def delete_message(message_id: int) -> bool:
    """Deletes one row from this bot's local message history (plus its
    attachment file, if any) — a log-management action on AgenticBotPlatform's
    own stored copy, not a retraction from the real Telegram/Discord/etc
    conversation (this app has no such capability against those
    platforms). Unlike Server Chat's delete_server_chat_message, there's
    no sender-restriction here: this is the operator's own bot's history,
    already gated by _require_token_or_api_key same as every other
    /api/chat/* route. Returns False if the message didn't exist."""
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute("SELECT attachment_path FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            return False
        _db._delete_attachment_files([row])
        conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
        conn.commit()
        return True


def list_messages(
    limit: int = 100,
    platform: Optional[str] = None,
    chat_id: Optional[Any] = None,
    after_id: Optional[int] = None,
    instance_id: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Oldest-first, capped to the most recent `limit` — the natural order
    for a chat view (append at the bottom). `after_id` supports incremental
    polling: only rows newer than the last one the caller already has."""
    conn = _db.get_conn()
    clauses = []
    params: list[Any] = []
    if platform is not None:
        clauses.append("platform=?")
        params.append(platform)
    if chat_id is not None:
        clauses.append("chat_id=?")
        params.append(str(chat_id))
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
    if after_id is not None:
        clauses.append("id>?")
        params.append(after_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if after_id is not None:
        # incremental: everything newer, oldest-first, no need to cap+reverse
        params.append(limit)
        return conn.execute(
            f"SELECT * FROM messages {where} ORDER BY id ASC LIMIT ?", params
        ).fetchall()
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM messages {where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    return list(reversed(rows))
