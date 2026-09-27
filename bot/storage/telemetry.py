"""Connection events, telemetry, MCP events, the audit log and config history.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Optional

from bot import db as _db


def log_connection_event(component: str, event: str, detail: str = "") -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO connections_log (ts, component, event, detail) VALUES (?, ?, ?, ?)",
            (_db._now(), component, event, detail),
        )
        conn.commit()


def log_telemetry(component: str, metric: str, value: float) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO telemetry_events (ts, component, metric, value) VALUES (?, ?, ?, ?)",
            (_db._now(), component, metric, value),
        )
        conn.commit()


def log_mcp_event(server: str, event: str, detail: str = "") -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO mcp_events (ts, server, event, detail) VALUES (?, ?, ?, ?)",
            (_db._now(), server, event, detail),
        )
        conn.commit()


def log_audit(actor: str, action: str, detail: str = "", job_id: Optional[int] = None) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail, job_id) VALUES (?, ?, ?, ?, ?)",
            (_db._now(), actor, action, detail, job_id),
        )
        conn.commit()
        return cur.lastrowid


def set_audit_log_job_id(audit_id: int, job_id: int) -> None:
    """Backfills job_id onto an audit_log row written before the job it
    describes existed — e.g. a swarm_dispatch row logged right before
    router.ask() creates the actual jobs row. See log_audit's job_id
    param for the normal (known-at-log-time) path."""
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE audit_log SET job_id=? WHERE id=?", (job_id, audit_id))
        conn.commit()


def list_audit_log(actions: Optional[list[str]] = None, limit: int = 50) -> list[sqlite3.Row]:
    """Recent audit_log rows, optionally filtered to a set of `action`
    values — used by the dashboard's delegation-activity panel to show
    only cross-instance calls (agent_ask/swarm_dispatch/agent_delegate)
    out of every audit event this app logs."""
    conn = _db.get_conn()
    if actions:
        placeholders = ",".join("?" for _ in actions)
        return conn.execute(
            f"SELECT * FROM audit_log WHERE action IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (*actions, limit),
        ).fetchall()
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def record_config_version(version: int, actor: str, summary: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO config_history (ts, version, actor, summary) VALUES (?, ?, ?, ?)",
            (_db._now(), version, actor, summary),
        )
        conn.commit()


def list_config_history(limit: int = 20) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM config_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
