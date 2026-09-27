"""Scheduled commands (/cron, /loop, /heartbeat).

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from bot import db as _db


def create_scheduled_command(
    instance_id: int, chat_id: Any, kind: str, prompt: str, interval_s: int,
    next_run_at: str, max_runs: Optional[int] = None, thread_id: Optional[Any] = None,
) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO scheduled_commands (instance_id, chat_id, thread_id, kind, prompt, interval_s, next_run_at, max_runs, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (instance_id, str(chat_id), str(thread_id) if thread_id is not None else None,
             kind, prompt, interval_s, next_run_at, max_runs, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def get_scheduled_command(sched_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM scheduled_commands WHERE id=?", (sched_id,)).fetchone()


def list_scheduled_commands(
    instance_id: int, chat_id: Optional[Any] = None, thread_id: Optional[Any] = None
) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if chat_id is not None and thread_id is not None:
        return conn.execute(
            "SELECT * FROM scheduled_commands WHERE instance_id=? AND chat_id=? AND thread_id IS ? ORDER BY id",
            (instance_id, str(chat_id), str(thread_id)),
        ).fetchall()
    if chat_id is not None:
        return conn.execute(
            "SELECT * FROM scheduled_commands WHERE instance_id=? AND chat_id=? ORDER BY id", (instance_id, str(chat_id))
        ).fetchall()
    return conn.execute("SELECT * FROM scheduled_commands WHERE instance_id=? ORDER BY id", (instance_id,)).fetchall()


def list_due_scheduled_commands(now_iso: str) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM scheduled_commands WHERE enabled=1 AND next_run_at<=? "
        "AND (max_runs IS NULL OR run_count<max_runs)",
        (now_iso,),
    ).fetchall()


def mark_scheduled_command_ran(sched_id: int, next_run_at: str) -> None:
    """Called right after DISPATCH (not after the dispatched turn actually
    finishes — a background=True turn's real outcome arrives later via
    agent_engine.run_turn's on_result callback), so this deliberately does
    NOT touch consecutive_failures — see reset_scheduled_command_failures()
    below, called only from that later, real-outcome callback."""
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE scheduled_commands SET last_run_at=?, next_run_at=?, run_count=run_count+1 WHERE id=?",
            (_db._now(), next_run_at, sched_id),
        )
        conn.commit()


def reset_scheduled_command_failures(sched_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE scheduled_commands SET consecutive_failures=0 WHERE id=?", (sched_id,))
        conn.commit()


def record_scheduled_command_failure(sched_id: int, error: str) -> int:
    """Increments this row's failure streak and stores the latest error
    message — returns the new streak count so the caller (bot/scheduler.py's
    _fire()) can decide whether it just crossed the auto-disable threshold
    without a second query."""
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE scheduled_commands SET consecutive_failures=consecutive_failures+1, last_error=? WHERE id=?",
            (error, sched_id),
        )
        conn.commit()
        row = conn.execute("SELECT consecutive_failures FROM scheduled_commands WHERE id=?", (sched_id,)).fetchone()
        return row["consecutive_failures"] if row else 0


def set_scheduled_command_enabled(sched_id: int, enabled: bool) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE scheduled_commands SET enabled=? WHERE id=?", (1 if enabled else 0, sched_id))
        conn.commit()


def delete_scheduled_command(sched_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM scheduled_commands WHERE id=?", (sched_id,))
        conn.commit()


def delete_scheduled_commands_for_instance(instance_id: int) -> int:
    """Called when a bot instance is deleted — unlike jobs/messages (kept
    intentionally as history), a scheduled command is a *live* recurring
    task with nothing left to run against once its instance is gone, so it
    has to be cleaned up rather than preserved. Without this, an orphaned
    row keeps coming due forever: the scheduler has no natural reason to
    ever stop polling a row that still says enabled=1. Returns how many
    were removed, for the caller's audit log."""
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("DELETE FROM scheduled_commands WHERE instance_id=?", (instance_id,))
        conn.commit()
        return cur.rowcount
