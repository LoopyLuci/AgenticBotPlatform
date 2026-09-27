"""Per-instance agent settings and the emergency stop.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from bot import db as _db


def get_agent_settings_row(instance_id: Optional[int]) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM agent_settings WHERE instance_id IS ?", (instance_id,)
    ).fetchone()


def find_admin_instance_id() -> Optional[int]:
    conn = _db.get_conn()
    row = conn.execute(
        "SELECT instance_id FROM agent_settings WHERE is_admin_instance=1 AND instance_id IS NOT NULL LIMIT 1"
    ).fetchone()
    return row["instance_id"] if row else None


def set_agent_settings_row(instance_id: Optional[int], **fields: Any) -> None:
    """Merges only the given keys (any value, including None to explicitly
    clear a field back to "fall through") into this instance_id's row,
    creating it if it doesn't exist yet."""
    unknown = set(fields) - set(_db._AGENT_SETTINGS_COLUMNS)
    if unknown:
        raise ValueError(f"unknown agent_settings field(s): {sorted(unknown)}")
    conn = _db.get_conn()
    with _db._lock:
        existing = conn.execute("SELECT * FROM agent_settings WHERE instance_id IS ?", (instance_id,)).fetchone()
        if existing is None:
            columns = ["instance_id"] + list(fields.keys()) + ["updated_at"]
            placeholders = ", ".join("?" for _ in columns)
            values = [instance_id] + list(fields.values()) + [_db._now()]
            conn.execute(f"INSERT INTO agent_settings ({', '.join(columns)}) VALUES ({placeholders})", values)
        else:
            set_clause = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
            conn.execute(
                f"UPDATE agent_settings SET {set_clause} WHERE instance_id IS ?",
                list(fields.values()) + [_db._now(), instance_id],
            )
        conn.commit()


def get_estop_state() -> dict[str, Any]:
    conn = _db.get_conn()
    row = conn.execute("SELECT * FROM estop_state WHERE id=1").fetchone()
    if row is None:
        return {"engaged": False, "reason": None, "actor": None, "changed_at": None}
    return {"engaged": bool(row["engaged"]), "reason": row["reason"], "actor": row["actor"], "changed_at": row["changed_at"]}


def set_estop_state(engaged: bool, reason: Optional[str], actor: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO estop_state (id, engaged, reason, actor, changed_at) VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET engaged=excluded.engaged, reason=excluded.reason, "
            "actor=excluded.actor, changed_at=excluded.changed_at",
            (1 if engaged else 0, reason, actor, _db._now()),
        )
        conn.commit()
