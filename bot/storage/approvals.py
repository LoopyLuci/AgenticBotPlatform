"""Pending tool approvals and standing tool grants.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from bot import db as _db


def create_pending_approval(instance_id: int, chat_id: Any, session_key: str, tool_name: str, tool_input: dict) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO pending_approvals (instance_id, chat_id, session_key, tool_name, tool_input, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (instance_id, str(chat_id), session_key, tool_name, json.dumps(tool_input), _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_approval(approval_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM pending_approvals WHERE id=?", (approval_id,)).fetchone()


def list_pending_approvals(instance_id: int, chat_id: Optional[Any] = None) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if chat_id is not None:
        return conn.execute(
            "SELECT * FROM pending_approvals WHERE instance_id=? AND chat_id=? AND status='pending' ORDER BY id ASC",
            (instance_id, str(chat_id)),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM pending_approvals WHERE instance_id=? AND status='pending' ORDER BY id ASC", (instance_id,)
    ).fetchall()


def resolve_pending_approval(approval_id: int, status: str, resolved_by: Optional[str]) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE pending_approvals SET status=?, resolved_at=?, resolved_by=? WHERE id=?",
            (status, _db._now(), resolved_by, approval_id),
        )
        conn.commit()


def expire_orphaned_approvals(keep: Optional[set] = None) -> int:
    """Mark every pending approval expired except the ids in `keep` (the ones a running turn still waits on). An
    approval's waiter lives only in the process that asked: one left pending by a run that ended or a process that
    died can never be answered, and would otherwise sit in every pending list for good."""
    conn = _db.get_conn()
    keep = set(keep or ())
    with _db._lock:
        ids = [r["id"] for r in conn.execute("SELECT id FROM pending_approvals WHERE status='pending'").fetchall()
               if r["id"] not in keep]
        for i in ids:
            conn.execute("UPDATE pending_approvals SET status='expired', resolved_at=?, resolved_by=NULL WHERE id=?",
                         (_db._now(), i))
        conn.commit()
    return len(ids)


def grant_tool_approval(instance_id: int, tool_name: str, session_key: Optional[str]) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO tool_approvals (instance_id, session_key, tool_name, granted_at) VALUES (?, ?, ?, ?)",
            (instance_id, session_key, tool_name, _db._now()),
        )
        conn.commit()


def has_tool_approval(instance_id: int, session_key: str, tool_name: str) -> bool:
    conn = _db.get_conn()
    row = conn.execute(
        "SELECT 1 FROM tool_approvals WHERE instance_id=? AND tool_name=? AND (session_key IS NULL OR session_key=?) LIMIT 1",
        (instance_id, tool_name, session_key),
    ).fetchone()
    return row is not None
