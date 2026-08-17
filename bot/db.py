"""SQLite storage layer — small, fast, and local.

WAL mode gives concurrent readers (the dashboard) a consistent view while
the bot keeps writing job/telemetry rows, without needing a separate
database server. One connection is shared process-wide behind a lock;
at this traffic scale (a single-user bot) that is simpler and just as
fast as a connection pool, and avoids a second moving part.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

# Reuses envfile's canonical-root resolution rather than Path(__file__)
# directly — a release build's bundled bot/ lives under a different folder
# than the source tree, and without pinning to one fixed root, a release
# run and a `cargo tauri dev` run would each quietly keep their own
# separate database (this bit .env the same way before it was fixed).
DB_PATH = PROJECT_ROOT / "data" / "bot.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type   TEXT NOT NULL,
    backend       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',   -- queued|running|retrying|success|failed
    user_id       INTEGER,
    prompt        TEXT,
    result        TEXT,
    error         TEXT,
    tokens        INTEGER,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    duration_ms   INTEGER
);

CREATE TABLE IF NOT EXISTS connections_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    component  TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    component  TEXT NOT NULL,
    metric     TEXT NOT NULL,
    value      REAL
);

CREATE TABLE IF NOT EXISTS mcp_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    server    TEXT NOT NULL,
    event     TEXT NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS config_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    version   INTEGER NOT NULL,
    actor     TEXT NOT NULL,
    summary   TEXT
);

CREATE TABLE IF NOT EXISTS allowed_users (
    telegram_id  INTEGER PRIMARY KEY,
    name         TEXT,
    added_at     TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    platform   TEXT NOT NULL DEFAULT 'telegram',  -- 'telegram' | 'discord' | 'slack' | ...
    chat_id    TEXT NOT NULL,   -- platform-native id: Telegram/Discord numeric, Slack channel string
    user_id    TEXT,
    username   TEXT,
    direction  TEXT NOT NULL,   -- 'in' (from the platform) | 'out' (bot/dashboard -> platform)
    source     TEXT NOT NULL,   -- '<platform>' | 'bot' | 'dashboard'
    text       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_component ON telemetry_events(component, ts);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(platform, chat_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema patches for databases created before a
    column existed — CREATE TABLE IF NOT EXISTS in SCHEMA only helps fresh
    installs; an existing messages table from before multi-platform support
    needs its new column added explicitly."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "platform" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'")


def init_db() -> None:
    conn = get_conn()
    with _lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


# ---------------------------------------------------------------- jobs ----

def create_job(action_type: str, backend: str, user_id: int, prompt: str) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO jobs (action_type, backend, status, user_id, prompt, created_at) "
            "VALUES (?, ?, 'queued', ?, ?, ?)",
            (action_type, backend, user_id, prompt, _now()),
        )
        conn.commit()
        return cur.lastrowid


def mark_job_running(job_id: int, backend: Optional[str] = None) -> None:
    conn = get_conn()
    with _lock:
        if backend:
            conn.execute(
                "UPDATE jobs SET status='running', backend=?, started_at=? WHERE id=?",
                (backend, _now(), job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?", (_now(), job_id)
            )
        conn.commit()


def mark_job_retrying(job_id: int, backend: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE jobs SET status='retrying', backend=? WHERE id=?", (backend, job_id)
        )
        conn.commit()


def mark_job_done(
    job_id: int,
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
    tokens: Optional[int] = None,
) -> None:
    conn = get_conn()
    with _lock:
        row = conn.execute("SELECT started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        finished = _now()
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


def get_job(job_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(limit: int = 50, status: Optional[str] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if status and status != "all":
        return conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ------------------------------------------------------------- logging ----

def log_connection_event(component: str, event: str, detail: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO connections_log (ts, component, event, detail) VALUES (?, ?, ?, ?)",
            (_now(), component, event, detail),
        )
        conn.commit()


def log_telemetry(component: str, metric: str, value: float) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO telemetry_events (ts, component, metric, value) VALUES (?, ?, ?, ?)",
            (_now(), component, metric, value),
        )
        conn.commit()


def log_mcp_event(server: str, event: str, detail: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO mcp_events (ts, server, event, detail) VALUES (?, ?, ?, ?)",
            (_now(), server, event, detail),
        )
        conn.commit()


def log_audit(actor: str, action: str, detail: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
            (_now(), actor, action, detail),
        )
        conn.commit()


def record_config_version(version: int, actor: str, summary: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO config_history (ts, version, actor, summary) VALUES (?, ?, ?, ?)",
            (_now(), version, actor, summary),
        )
        conn.commit()


def list_config_history(limit: int = 20) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM config_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# ------------------------------------------------------------ users -------

def add_allowed_user(telegram_id: int, name: str = "") -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO allowed_users (telegram_id, name, added_at) VALUES (?, ?, ?)",
            (telegram_id, name, _now()),
        )
        conn.commit()


def remove_allowed_user(telegram_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM allowed_users WHERE telegram_id=?", (telegram_id,))
        conn.commit()


def list_allowed_users() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM allowed_users ORDER BY added_at").fetchall()


# -------------------------------------------------------------- chat ------

def log_message(
    chat_id: Any,
    direction: str,
    source: str,
    text: str,
    platform: str = "telegram",
    user_id: Optional[Any] = None,
    username: str = "",
) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO messages (ts, platform, chat_id, user_id, username, direction, source, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), platform, str(chat_id), str(user_id) if user_id is not None else None, username, direction, source, text),
        )
        conn.commit()
        return cur.lastrowid


def list_messages(
    limit: int = 100,
    platform: Optional[str] = None,
    chat_id: Optional[Any] = None,
    after_id: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Oldest-first, capped to the most recent `limit` — the natural order
    for a chat view (append at the bottom). `after_id` supports incremental
    polling: only rows newer than the last one the caller already has."""
    conn = get_conn()
    clauses = []
    params: list[Any] = []
    if platform is not None:
        clauses.append("platform=?")
        params.append(platform)
    if chat_id is not None:
        clauses.append("chat_id=?")
        params.append(str(chat_id))
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


# --------------------------------------------------------- dashboard KPIs -

def get_overview() -> dict[str, Any]:
    conn = get_conn()
    running = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='running'").fetchone()["c"]
    queued = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='queued'").fetchone()["c"]
    completed_today = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='success' AND date(created_at)=date('now')"
    ).fetchone()["c"]
    failed_today = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='failed' AND date(created_at)=date('now')"
    ).fetchone()["c"]
    week = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs WHERE created_at >= datetime('now','-7 days') "
        "AND status IN ('success','failed') GROUP BY status"
    ).fetchall()
    succ = sum(r["c"] for r in week if r["status"] == "success")
    fail = sum(r["c"] for r in week if r["status"] == "failed")
    success_rate = round(100 * succ / (succ + fail), 1) if (succ + fail) else 100.0
    avg_dur = conn.execute(
        "SELECT AVG(duration_ms) a FROM jobs WHERE status='success' "
        "AND created_at >= datetime('now','-1 day')"
    ).fetchone()["a"]
    tokens_today = conn.execute(
        "SELECT COALESCE(SUM(tokens),0) t FROM jobs WHERE date(created_at)=date('now')"
    ).fetchone()["t"]
    return {
        "jobs_running": running,
        "jobs_queued": queued,
        "completed_today": completed_today,
        "failed_today": failed_today,
        "success_rate_7d": success_rate,
        "avg_duration_ms": round(avg_dur) if avg_dur else 0,
        "tokens_today": tokens_today,
    }


def get_jobs_timeseries_24h() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:00', created_at) hour, status, COUNT(*) c "
        "FROM jobs WHERE created_at >= datetime('now','-24 hours') "
        "GROUP BY hour, status ORDER BY hour"
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
    conn = get_conn()
    rows = conn.execute(
        "SELECT backend, COUNT(*) c FROM jobs WHERE date(created_at)=date('now') "
        "GROUP BY backend"
    ).fetchall()
    return {r["backend"]: r["c"] for r in rows}


def get_latency_by_backend() -> dict[str, dict[str, float]]:
    conn = get_conn()
    out: dict[str, dict[str, float]] = {}
    for backend in ("api", "cli", "ui"):
        rows = [
            r["value"]
            for r in conn.execute(
                "SELECT value FROM telemetry_events WHERE component=? AND metric='latency_ms' "
                "AND ts >= datetime('now','-6 hours') ORDER BY value",
                (backend,),
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
    conn = get_conn()
    tables = ["jobs", "connections_log", "telemetry_events", "mcp_events", "audit_log", "messages"]
    return {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables
    }


def get_db_size_bytes() -> int:
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            size += p.stat().st_size
    return size


def get_recent_connection_events(limit: int = 20) -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM connections_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def vacuum() -> None:
    conn = get_conn()
    with _lock:
        conn.execute("VACUUM;")
        conn.commit()
    log_audit(actor="dashboard", action="vacuum", detail="manual VACUUM triggered")
