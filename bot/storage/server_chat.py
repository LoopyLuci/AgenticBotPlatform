"""Server chat: conversations, participants and messages between devices.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Optional

from bot import db as _db


def clear_server_chat_messages(conversation_id: int) -> int:
    """Deletes every message in one Server Chat conversation, plus their
    attachment/thumbnail files. The conversation row itself stays — group
    and direct rooms are structural (auto-recreated on device pairing),
    only their history is being cleared. Returns the number removed."""
    conn = _db.get_conn()
    with _db._lock:
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM server_chat_messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchall()
        _db._delete_attachment_files(msg_rows)
        cur = conn.execute("DELETE FROM server_chat_messages WHERE conversation_id=?", (conversation_id,))
        conn.commit()
        return cur.rowcount


def delete_server_chat_conversation(conversation_id: int) -> bool:
    """Fully removes one Server Chat conversation — every message, their
    attachment/thumbnail files, and the conversation row itself (unlike
    clear_server_chat_messages, which only empties a conversation's
    history and keeps the row). The caller (see the dashboard route) is
    responsible for refusing this on the group room — a shared room
    can't be "deleted" out from under every other device the way a
    direct 1:1 conversation can. Returns False if it didn't exist."""
    conn = _db.get_conn()
    with _db._lock:
        if conn.execute("SELECT 1 FROM server_chat_conversations WHERE id=?", (conversation_id,)).fetchone() is None:
            return False
        msg_rows = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM server_chat_messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchall()
        _db._delete_attachment_files(msg_rows)
        conn.execute("DELETE FROM server_chat_messages WHERE conversation_id=?", (conversation_id,))
        conn.execute("DELETE FROM server_chat_conversations WHERE id=?", (conversation_id,))
        conn.commit()
        return True


def export_server_chat_data(conversation_id: int) -> list[dict]:
    conn = _db.get_conn()
    rows = conn.execute(
        "SELECT * FROM server_chat_messages WHERE conversation_id=? ORDER BY id ASC", (conversation_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def ensure_server_chat_group() -> int:
    """The single permanent "Server Chat" room every device sees — created
    once, reused forever after."""
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute("SELECT id FROM server_chat_conversations WHERE kind='group' LIMIT 1").fetchone()
        if row is not None:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO server_chat_conversations (kind, participant_a, participant_b, created_at) "
            "VALUES ('group', NULL, NULL, ?)",
            (_db._now(),),
        )
        conn.commit()
        return cur.lastrowid


def ensure_direct_conversation(device_a: int, device_b: int) -> int:
    """One row per unordered device pair — participant_a is always the
    smaller id so (3, 7) and (7, 3) resolve to the same conversation."""
    lo, hi = (device_a, device_b) if device_a <= device_b else (device_b, device_a)
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute(
            "SELECT id FROM server_chat_conversations WHERE kind='direct' AND participant_a=? AND participant_b=?",
            (lo, hi),
        ).fetchone()
        if row is not None:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO server_chat_conversations (kind, participant_a, participant_b, created_at) "
            "VALUES ('direct', ?, ?, ?)",
            (lo, hi, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def backfill_server_chat_conversations() -> None:
    """Fills in direct conversations for devices paired before Server Chat
    existed — a full mesh across desktop + every non-revoked device.
    ensure_direct_conversation() is idempotent, so this is safe to call on
    every startup, not just once."""
    keys = [r["id"] for r in _db.list_api_keys(kind="device") if not r["revoked_at"]]
    devices = [_db.SERVER_CHAT_DESKTOP_DEVICE_ID] + keys
    for i, a in enumerate(devices):
        for b in devices[i + 1:]:
            _db.ensure_direct_conversation(a, b)


def create_conversations_for_new_device(device_id: int) -> None:
    """Called right after a new paired device's key is created — opens a
    direct conversation between it and the desktop app, and between it and
    every other already-paired (non-revoked) device, so every device can
    reach every other device from the moment it's added without anyone
    having to manually start a chat."""
    _db.ensure_direct_conversation(device_id, _db.SERVER_CHAT_DESKTOP_DEVICE_ID)
    for row in _db.list_api_keys(kind="device"):
        if row["id"] != device_id and not row["revoked_at"]:
            _db.ensure_direct_conversation(device_id, row["id"])


def is_conversation_participant(conversation_id: int, device_id: int) -> bool:
    conn = _db.get_conn()
    row = conn.execute("SELECT kind, participant_a, participant_b FROM server_chat_conversations WHERE id=?", (conversation_id,)).fetchone()
    if row is None:
        return False
    if row["kind"] == "group":
        return True
    return device_id in (row["participant_a"], row["participant_b"])


def list_server_chat_conversations(device_id: int) -> list[dict]:
    """Every conversation `device_id` can see: the group room, plus every
    direct conversation it's a participant of. Each is annotated with a
    display title (the room name, or the other device's current label)
    and its own last-message preview so a device list can render without
    N+1 follow-up requests."""
    conn = _db.get_conn()
    rows = conn.execute(
        "SELECT * FROM server_chat_conversations WHERE kind='group' OR participant_a=? OR participant_b=? "
        "ORDER BY id",
        (device_id, device_id),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d["kind"] == "group":
            d["title"] = "Server Chat"
            d["peer_device_id"] = None
        else:
            peer_id = d["participant_b"] if d["participant_a"] == device_id else d["participant_a"]
            d["title"] = _db.device_label(peer_id)
            d["peer_device_id"] = peer_id
        last = conn.execute(
            "SELECT text, ts, attachment_name FROM server_chat_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
            (d["id"],),
        ).fetchone()
        d["last_message"] = dict(last) if last else None
        out.append(d)
    return out


def create_server_chat_message(
    conversation_id: int,
    sender_device_id: int,
    text: str,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
    attachment_mime: Optional[str] = None,
    attachment_size: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
    *,
    kind: str = "message",
    approval_id: Optional[int] = None,
) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO server_chat_messages "
            "(conversation_id, sender_device_id, ts, text, attachment_path, attachment_name, "
            "attachment_mime, attachment_size, thumbnail_path, kind, approval_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, sender_device_id, _db._now(), text, attachment_path, attachment_name,
             attachment_mime, attachment_size, thumbnail_path, kind, approval_id),
        )
        conn.commit()
        return cur.lastrowid


def list_server_chat_messages(conversation_id: int, after_id: int = 0, limit: int = 100) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM server_chat_messages WHERE conversation_id=? AND id > ? ORDER BY id LIMIT ?",
        (conversation_id, after_id, limit),
    ).fetchall()


def get_server_chat_message(message_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM server_chat_messages WHERE id=?", (message_id,)).fetchone()


def delete_server_chat_message(message_id: int) -> bool:
    """Deletes one Server Chat message and its attachment/thumbnail files
    (if any). The caller (see the dashboard route) is responsible for
    checking the requester actually sent this message before calling
    this — this function itself does no permission check, same division
    of responsibility as every other bare db.py delete helper. Returns
    False if the message didn't exist (already deleted, bad id)."""
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute(
            "SELECT attachment_path, thumbnail_path FROM server_chat_messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is None:
            return False
        _db._delete_attachment_files([row])
        conn.execute("DELETE FROM server_chat_messages WHERE id=?", (message_id,))
        conn.commit()
        return True
