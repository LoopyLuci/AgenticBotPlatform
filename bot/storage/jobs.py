"""Jobs, their tool events and children, ephemeral sessions, SSH recordings and model toggles.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from bot import db as _db


def create_job(
    action_type: str,
    backend: str,
    user_id: int,
    prompt: str,
    instance_id: Optional[int] = None,
    swarm_run_id: Optional[str] = None,
    chat_id: Optional[Any] = None,
) -> int:
    conn = _db.get_conn()
    with _db._lock:
        chat_key = str(chat_id) if chat_id is not None else None
        session_id = _db._get_or_create_session(conn, instance_id, chat_key, prompt)
        cur = conn.execute(
            "INSERT INTO jobs (action_type, backend, status, user_id, prompt, created_at, instance_id, swarm_run_id, chat_id, session_id) "
            "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (action_type, backend, user_id, prompt, _db._now(), instance_id, swarm_run_id, chat_key, session_id),
        )
        conn.commit()
        job_id = cur.lastrowid
    _db._notify_job_changed(job_id)
    return job_id


def mark_job_running(job_id: int, backend: Optional[str] = None) -> None:
    conn = _db.get_conn()
    with _db._lock:
        if backend:
            conn.execute(
                "UPDATE jobs SET status='running', backend=?, started_at=? WHERE id=?",
                (backend, _db._now(), job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?", (_db._now(), job_id)
            )
        conn.commit()
    _db._notify_job_changed(job_id)


def mark_job_retrying(job_id: int, backend: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE jobs SET status='retrying', backend=? WHERE id=?", (backend, job_id)
        )
        conn.commit()
    _db._notify_job_changed(job_id)


def mark_job_done(
    job_id: int,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
    tokens: Optional[int] = None,
) -> None:
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute("SELECT started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        finished = _db._now()
        duration_ms = None
        if row and row["started_at"]:
            started = datetime.fromisoformat(row["started_at"])
            duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        conn.execute(
            "UPDATE jobs SET status=?, result=?, error=?, tokens=?, finished_at=?, duration_ms=? "
            "WHERE id=?",
            (status, result, error, tokens, finished, duration_ms, job_id),
        )
        conn.commit()
    _db._notify_job_changed(job_id)


def get_job(job_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(
    limit: int = 50,
    status: Optional[str] = None,
    instance_id: Optional[int] = None,
    swarm_run_id: Optional[str] = None,
) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    clauses = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status=?")
        params.append(status)
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
    if swarm_run_id is not None:
        clauses.append("swarm_run_id=?")
        params.append(swarm_run_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", params).fetchall()


def get_latest_job(instance_id: int, action_type: str) -> Optional[sqlite3.Row]:
    """Most recent job for this instance+action_type — used right after a
    synchronous dispatch call (e.g. dispatch_swarm_goal's router.ask())
    returns, to resolve the job row that call just finished writing,
    without needing router.ask() to change its own return shape."""
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM jobs WHERE instance_id=? AND action_type=? ORDER BY id DESC LIMIT 1",
        (instance_id, action_type),
    ).fetchone()


def log_job_tool_event(job_id: int, event_type: str, tool_name: str, payload: dict) -> int:
    conn = _db.get_conn()
    with _db._lock:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM job_tool_events WHERE job_id=?", (job_id,)
        ).fetchone()
        seq = seq_row["next_seq"]
        cur = conn.execute(
            "INSERT INTO job_tool_events (job_id, seq, event_type, tool_name, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, seq, event_type, tool_name, json.dumps(payload), _db._now()),
        )
        conn.commit()
        event_id = cur.lastrowid
    _db._notify_job_tool_event(job_id)
    return event_id


def list_job_tool_events(job_id: int) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM job_tool_events WHERE job_id=? ORDER BY seq ASC", (job_id,)
    ).fetchall()


def create_ssh_recording(connection_name: str, command: str) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO ssh_session_recordings (connection_name, command, status, started_at) "
            "VALUES (?, ?, 'recording', ?)",
            (connection_name, command, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def set_ssh_recording_status(recording_id: int, status: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        if status == "stopped":
            conn.execute(
                "UPDATE ssh_session_recordings SET status=?, stopped_at=? WHERE id=?",
                (status, _db._now(), recording_id),
            )
        else:
            conn.execute("UPDATE ssh_session_recordings SET status=? WHERE id=?", (status, recording_id))
        conn.commit()


def get_ssh_recording(recording_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM ssh_session_recordings WHERE id=?", (recording_id,)).fetchone()


def list_ssh_recordings() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM ssh_session_events e WHERE e.recording_id = r.id) AS event_count "
        "FROM ssh_session_recordings r ORDER BY r.started_at DESC"
    ).fetchall()


def delete_ssh_recording(recording_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM ssh_session_events WHERE recording_id=?", (recording_id,))
        conn.execute("DELETE FROM ssh_session_recordings WHERE id=?", (recording_id,))
        conn.commit()


def log_ssh_session_event(recording_id: int, event_type: str, payload: dict) -> int:
    conn = _db.get_conn()
    with _db._lock:
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM ssh_session_events WHERE recording_id=?",
            (recording_id,),
        ).fetchone()
        seq = seq_row["next_seq"]
        cur = conn.execute(
            "INSERT INTO ssh_session_events (recording_id, seq, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (recording_id, seq, event_type, json.dumps(payload), _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_ssh_session_events(recording_id: int) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM ssh_session_events WHERE recording_id=? ORDER BY seq ASC", (recording_id,)
    ).fetchall()


def set_job_children(job_id: int, children: list[dict]) -> None:
    """Full replace of a job's parsed child breakdown — written once, when
    the dispatch that produced it completes."""
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM job_children WHERE job_id=?", (job_id,))
        now = _db._now()
        conn.executemany(
            "INSERT INTO job_children (job_id, child_index, goal, model, status, result_excerpt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    job_id,
                    c.get("index", i),
                    c.get("goal", ""),
                    c.get("model", ""),
                    c.get("status", ""),
                    c.get("result_excerpt", ""),
                    now,
                )
                for i, c in enumerate(children)
            ],
        )
        conn.commit()
    _db._notify_job_children_set(job_id)


def list_job_children(job_id: int) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM job_children WHERE job_id=? ORDER BY child_index ASC", (job_id,)
    ).fetchall()


def create_ephemeral_session(parent_instance_id: Optional[int], backend: str, model: str, goal: str) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO ephemeral_sessions (parent_instance_id, backend, model, goal, status, created_at) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            (parent_instance_id, backend, model, goal, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def finish_ephemeral_session(session_id: int, status: str, result: Optional[str] = None) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE ephemeral_sessions SET status=?, result=?, finished_at=? WHERE id=?",
            (status, result, _db._now(), session_id),
        )
        conn.commit()


def get_ephemeral_session(session_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM ephemeral_sessions WHERE id=?", (session_id,)).fetchone()


def list_ephemeral_sessions(parent_instance_id: Optional[int] = None, limit: int = 50) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if parent_instance_id is not None:
        return conn.execute(
            "SELECT * FROM ephemeral_sessions WHERE parent_instance_id=? ORDER BY id DESC LIMIT ?",
            (parent_instance_id, limit),
        ).fetchall()
    return conn.execute("SELECT * FROM ephemeral_sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def set_model_toggle(provider: str, model_id: str, enabled: bool) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO model_toggles (provider, model_id, enabled, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider, model_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
            (provider, model_id, 1 if enabled else 0, _db._now()),
        )
        conn.commit()


def bulk_set_model_toggles(provider: str, updates: dict[str, bool]) -> None:
    """Set many per-model overrides for one provider in a single
    transaction — backs the Models page's "Turn Off All Paid"/"Turn On
    All Paid Models" bulk actions, which would otherwise cost one
    commit per model (a real provider can have hundreds)."""
    if not updates:
        return
    conn = _db.get_conn()
    ts = _db._now()
    with _db._lock:
        conn.executemany(
            "INSERT INTO model_toggles (provider, model_id, enabled, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider, model_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
            [(provider, model_id, 1 if enabled else 0, ts) for model_id, enabled in updates.items()],
        )
        conn.commit()


def list_model_toggles(provider: Optional[str] = None) -> list[sqlite3.Row]:
    """Every EXPLICIT toggle row (both enabled and disabled) — the Models
    page needs this to render correct switch positions; a model with no
    row here is enabled by default and simply isn't represented."""
    conn = _db.get_conn()
    if provider is not None:
        return conn.execute("SELECT * FROM model_toggles WHERE provider=?", (provider,)).fetchall()
    return conn.execute("SELECT * FROM model_toggles").fetchall()


def disabled_model_ids(provider: str) -> set[str]:
    """One query per provider per call site — the shape every filtering
    call site (bot.models.live_custom_models()/custom_models_with_pricing())
    actually uses, rather than a per-model lookup."""
    conn = _db.get_conn()
    rows = conn.execute(
        "SELECT model_id FROM model_toggles WHERE provider=? AND enabled=0", (provider,)
    ).fetchall()
    return {r["model_id"] for r in rows}
