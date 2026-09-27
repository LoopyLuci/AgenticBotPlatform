"""Swarms and swarm runs.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from bot import db as _db


def create_swarm(name: str, strategy: str, config_json: str, enabled: bool = True) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO swarms (name, strategy, config, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, strategy, config_json, 1 if enabled else 0, _db._now(), _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def update_swarm(swarm_id: int, **fields: Any) -> None:
    columns, params = [], []
    for key in ("name", "strategy", "config", "enabled"):
        if key in fields:
            columns.append(f"{key}=?")
            params.append((1 if fields[key] else 0) if key == "enabled" else fields[key])
    if not columns:
        return
    columns.append("updated_at=?")
    params.append(_db._now())
    params.append(swarm_id)
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(f"UPDATE swarms SET {', '.join(columns)} WHERE id=?", params)
        conn.commit()


def delete_swarm(swarm_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM swarms WHERE id=?", (swarm_id,))
        conn.commit()


def get_swarm(swarm_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM swarms WHERE id=?", (swarm_id,)).fetchone()


def list_swarms() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM swarms ORDER BY id").fetchall()


def create_swarm_run(swarm_id: int, swarm_run_id: str, prompt: str, requested_by: str = "") -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO swarm_runs (swarm_id, swarm_run_id, status, prompt, requested_by, created_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (swarm_id, swarm_run_id, prompt, requested_by, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def update_swarm_run(
    swarm_run_id: str,
    status: Optional[str] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
    steps_json: Optional[str] = None,
    finished: bool = False,
) -> None:
    columns, params = [], []
    if status is not None:
        columns.append("status=?")
        params.append(status)
    if result is not None:
        columns.append("result=?")
        params.append(result)
    if error is not None:
        columns.append("error=?")
        params.append(error)
    if steps_json is not None:
        columns.append("steps=?")
        params.append(steps_json)
    conn = _db.get_conn()
    with _db._lock:
        if finished:
            row = conn.execute("SELECT created_at FROM swarm_runs WHERE swarm_run_id=?", (swarm_run_id,)).fetchone()
            finished_at = _db._now()
            duration_ms = None
            if row and row["created_at"]:
                started = datetime.fromisoformat(row["created_at"])
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            columns += ["finished_at=?", "duration_ms=?"]
            params += [finished_at, duration_ms]
        if not columns:
            return
        params.append(swarm_run_id)
        conn.execute(f"UPDATE swarm_runs SET {', '.join(columns)} WHERE swarm_run_id=?", params)
        conn.commit()


def get_swarm_run(swarm_run_id: str) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM swarm_runs WHERE swarm_run_id=?", (swarm_run_id,)).fetchone()


def list_swarm_runs(swarm_id: Optional[int] = None, limit: int = 50) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if swarm_id is not None:
        return conn.execute(
            "SELECT * FROM swarm_runs WHERE swarm_id=? ORDER BY id DESC LIMIT ?", (swarm_id, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM swarm_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
