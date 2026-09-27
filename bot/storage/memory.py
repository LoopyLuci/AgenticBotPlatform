"""Agent memory entries.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Optional

from bot import db as _db


def create_memory_entry(instance_id: int, content: str, source: str = "user", status: str = "pending",
                        kind: str = "fact") -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO memory_entries (instance_id, content, status, source, created_at, kind) VALUES (?, ?, ?, ?, ?, ?)",
            (instance_id, content, status, source, _db._now(), kind),
        )
        conn.commit()
        return cur.lastrowid


def get_memory_entry(entry_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM memory_entries WHERE id=?", (entry_id,)).fetchone()


def list_memory_entries(instance_id: int, status: Optional[str] = None) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if status is not None:
        return conn.execute(
            "SELECT * FROM memory_entries WHERE instance_id=? AND status=? ORDER BY id DESC", (instance_id, status)
        ).fetchall()
    return conn.execute("SELECT * FROM memory_entries WHERE instance_id=? ORDER BY id DESC", (instance_id,)).fetchall()


def touch_memory_entry(entry_id: int) -> None:
    """A memory was confirmed again (someone saved it a second time): it is fresh, and used."""
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE memory_entries SET uses = uses + 1, last_used = ? WHERE id = ?", (_db._now(), entry_id))
        conn.commit()


def delete_memory_entry(entry_id: int, instance_id: int) -> bool:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("DELETE FROM memory_entries WHERE id = ? AND instance_id = ?", (entry_id, instance_id))
        conn.commit()
        return cur.rowcount > 0


def resolve_memory_entry(entry_id: int, status: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE memory_entries SET status=?, resolved_at=? WHERE id=?", (status, _db._now(), entry_id)
        )
        conn.commit()
