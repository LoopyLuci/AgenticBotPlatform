"""Overview, usage, insights and database statistics, export, vacuum and retention pruning.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bot import db as _db


# Time filters are ranges on the raw ISO column, never date(created_at)=... (a
# function on the column defeats every index and scanned the whole jobs table on
# each 5-second dashboard poll). Bounds use the stored format exactly
# (UTC, 'T' separator), which SQLite's datetime('now', ...) did not.
def _since(**delta: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


def _today() -> tuple[str, str]:
    d = datetime.now(timezone.utc).date()
    return d.isoformat(), (d + timedelta(days=1)).isoformat()


def prune_old_data(days: int) -> dict[str, int]:
    """Deletes rows older than `days` from the highest-volume,
    lowest-long-term-value tables — see config/backends.yaml's `retention`
    comment for the reasoning and bot/retention.py for the daily
    background task that calls this. Deliberately narrow: audit_log (a
    security trail), chat/session history, and config_history are never
    touched here — this is only for tables that exist to answer "what
    happened recently," not "what happened ever." `jobs` additionally
    excludes anything not yet in a terminal state, so a long-running or
    stuck job can never be deleted out from under itself regardless of
    its age. Returns {table: rows_deleted}."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = _db.get_conn()
    removed: dict[str, int] = {}
    with _db._lock:
        cur = conn.execute(
            "DELETE FROM jobs WHERE created_at<? AND status NOT IN ('queued','running','retrying')",
            (cutoff,),
        )
        removed["jobs"] = cur.rowcount
        cur = conn.execute("DELETE FROM telemetry_events WHERE ts<?", (cutoff,))
        removed["telemetry_events"] = cur.rowcount
        cur = conn.execute("DELETE FROM connections_log WHERE ts<?", (cutoff,))
        removed["connections_log"] = cur.rowcount
        cur = conn.execute("DELETE FROM support_bot_classifications WHERE ts<?", (cutoff,))
        removed["support_bot_classifications"] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM ephemeral_sessions WHERE created_at<? AND status != 'running'", (cutoff,)
        )
        removed["ephemeral_sessions"] = cur.rowcount
        conn.commit()
    return removed


def get_overview() -> dict[str, Any]:
    conn = _db.get_conn()
    running = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='running'").fetchone()["c"]
    queued = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='queued'").fetchone()["c"]
    today = _today()
    completed_today = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='success' AND created_at >= ? AND created_at < ?", today
    ).fetchone()["c"]
    failed_today = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='failed' AND created_at >= ? AND created_at < ?", today
    ).fetchone()["c"]
    week = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs WHERE status IN ('success','failed') AND created_at >= ? GROUP BY status",
        (_since(days=7),),
    ).fetchall()
    succ = sum(r["c"] for r in week if r["status"] == "success")
    fail = sum(r["c"] for r in week if r["status"] == "failed")
    success_rate = round(100 * succ / (succ + fail), 1) if (succ + fail) else 100.0
    avg_dur = conn.execute(
        "SELECT AVG(duration_ms) a FROM jobs WHERE status='success' AND created_at >= ?", (_since(days=1),)
    ).fetchone()["a"]
    tokens_today = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) t FROM jobs WHERE created_at >= ? AND created_at < ?", today
    ).fetchone()["t"]
    # Which bots have an agent working right now: one entry per bot instance with at least one running job.
    # Jobs with no instance (the pre-multi-instance / global path) count together as one "default" bot.
    active = conn.execute(
        "SELECT j.instance_id AS instance_id, b.name AS name, COUNT(*) AS jobs "
        "FROM jobs j LEFT JOIN bot_instances b ON b.id = j.instance_id "
        "WHERE j.status='running' GROUP BY j.instance_id ORDER BY jobs DESC, b.name"
    ).fetchall()
    active_bots = [
        {"instance_id": r["instance_id"], "name": r["name"] or "default", "jobs": r["jobs"]} for r in active
    ]
    bots_enabled = conn.execute("SELECT COUNT(*) c FROM bot_instances WHERE enabled=1").fetchone()["c"]
    return {
        "jobs_running": running,
        "bots_running_agents": len(active_bots),
        "active_bots": active_bots,
        "bots_enabled": bots_enabled,
        "jobs_queued": queued,
        "completed_today": completed_today,
        "failed_today": failed_today,
        "success_rate_7d": success_rate,
        "avg_duration_ms": round(avg_dur) if avg_dur else 0,
        "tokens_today": tokens_today,
    }


def get_usage_summary(instance_id: int) -> dict[str, Any]:
    """Per-instance token/job usage for /usage — get_overview() above is
    global, this is the one bot's own numbers."""
    conn = _db.get_conn()
    row_today = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) tok, COUNT(*) n FROM jobs WHERE instance_id=? AND created_at >= ? AND created_at < ?",
        (instance_id, *_today()),
    ).fetchone()
    row_total = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) tok, COUNT(*) n FROM jobs WHERE instance_id=?", (instance_id,)
    ).fetchone()
    return {
        "tokens_today": row_today["tok"],
        "jobs_today": row_today["n"],
        "tokens_total": row_total["tok"],
        "jobs_total": row_total["n"],
    }


def get_insights(instance_id: int, days: int = 7) -> dict[str, Any]:
    """Daily job counts/success-rate/tokens for /insights [days]."""
    conn = _db.get_conn()
    rows = conn.execute(
        "SELECT date(created_at) d, status, COUNT(*) n, COALESCE(SUM(tokens),0) tok "
        "FROM jobs WHERE instance_id=? AND created_at >= ? GROUP BY d, status ORDER BY d",
        (instance_id, _since(days=days)),
    ).fetchall()
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = by_day.setdefault(r["d"], {"success": 0, "failed": 0, "other": 0, "tokens": 0})
        entry["tokens"] += r["tok"]
        if r["status"] == "success":
            entry["success"] = r["n"]
        elif r["status"] == "failed":
            entry["failed"] = r["n"]
        else:
            entry["other"] += r["n"]
    messages_row = conn.execute(
        "SELECT COUNT(*) n FROM messages WHERE instance_id=? AND direction='in' AND ts >= ?",
        (instance_id, _since(days=days)),
    ).fetchone()
    return {"days": days, "by_day": by_day, "messages_in": messages_row["n"]}


def get_jobs_timeseries_24h() -> list[dict[str, Any]]:
    conn = _db.get_conn()
    rows = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:00', created_at) hour, status, COUNT(*) c "
        "FROM jobs WHERE created_at >= ? GROUP BY hour, status ORDER BY hour", (_since(hours=24),)
    ).fetchall()
    buckets: dict[str, dict[str, int]] = {}
    for r in rows:
        buckets.setdefault(r["hour"], {"completed": 0, "failed": 0})
        if r["status"] == "success":
            buckets[r["hour"]]["completed"] += r["c"]
        elif r["status"] == "failed":
            buckets[r["hour"]]["failed"] += r["c"]
    return [{"hour": h, **v} for h, v in sorted(buckets.items())]


def get_jobs_by_backend_today() -> dict[str, int]:
    conn = _db.get_conn()
    rows = conn.execute(
        "SELECT backend, COUNT(*) c FROM jobs WHERE created_at >= ? AND created_at < ? GROUP BY backend", _today()
    ).fetchall()
    return {r["backend"]: r["c"] for r in rows}


def get_latency_by_backend() -> dict[str, dict[str, float]]:
    conn = _db.get_conn()
    out: dict[str, dict[str, float]] = {}
    for backend in ("api", "cli", "ui"):
        rows = [
            r["value"]
            for r in conn.execute(
                "SELECT value FROM telemetry_events WHERE component=? AND metric='latency_ms' "
                "AND ts >= ? ORDER BY value",
                (backend, _since(hours=6)),
            ).fetchall()
        ]
        if rows:
            p50 = rows[len(rows) // 2]
            p95 = rows[min(len(rows) - 1, int(len(rows) * 0.95))]
            out[backend] = {"p50_ms": p50, "p95_ms": p95}
        else:
            out[backend] = {"p50_ms": 0, "p95_ms": 0}
    return out


def get_table_counts() -> dict[str, int]:
    conn = _db.get_conn()
    tables = ["jobs", "connections_log", "telemetry_events", "mcp_events", "audit_log", "messages", "bot_instances", "swarms", "swarm_runs"]
    return {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables
    }


def get_db_size_bytes() -> int:
    size = _db.DB_PATH.stat().st_size if _db.DB_PATH.exists() else 0
    for suffix in ("-wal", "-shm"):
        p = Path(str(_db.DB_PATH) + suffix)
        if p.exists():
            size += p.stat().st_size
    return size


def export_table(table: str) -> list[dict]:
    if table not in _db.EXPORTABLE_TABLES:
        raise ValueError(f"unknown or non-exportable table: {table}")
    conn = _db.get_conn()
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


def get_recent_connection_events(limit: int = 20) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM connections_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def vacuum(actor: str = "dashboard", detail: str = "manual VACUUM triggered") -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("VACUUM;")
        conn.commit()
    _db.log_audit(actor=actor, action="vacuum", detail=detail)


def days_since_last_vacuum() -> Optional[float]:
    """None if a VACUUM has never been logged (manual or automatic) —
    callers should treat that as "due", not "recent"."""
    conn = _db.get_conn()
    row = conn.execute(
        "SELECT ts FROM audit_log WHERE action='vacuum' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    last = datetime.fromisoformat(row["ts"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 86400
