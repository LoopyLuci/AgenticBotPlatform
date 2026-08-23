"""SQLite storage layer — small, fast, and local.

WAL mode gives concurrent readers (the dashboard) a consistent view while
the bot keeps writing job/telemetry rows, without needing a separate
database server. One connection is shared process-wide behind a lock;
at this traffic scale (a single-user bot) that is simpler and just as
fast as a connection pool, and avoids a second moving part.
"""

from __future__ import annotations

import hashlib
import secrets
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
    duration_ms   INTEGER,
    instance_id   INTEGER,   -- bot_instances.id; NULL for pre-multi-instance rows ("legacy")
    swarm_run_id  TEXT       -- swarm_runs.swarm_run_id; NULL unless this job is one swarm member's call
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
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    platform     TEXT NOT NULL DEFAULT 'telegram',  -- 'telegram' | 'discord' | 'slack' | ...
    chat_id      TEXT NOT NULL,   -- platform-native id: Telegram/Discord numeric, Slack channel string
    user_id      TEXT,
    username     TEXT,
    direction    TEXT NOT NULL,   -- 'in' (from the platform) | 'out' (bot/dashboard -> platform)
    source       TEXT NOT NULL,   -- '<platform>' | 'bot' | 'dashboard'
    text         TEXT NOT NULL,
    instance_id  INTEGER,  -- bot_instances.id; NULL for pre-multi-instance rows ("legacy")
    attachment_path  TEXT,  -- relative filename under data/attachments/, NULL if no attachment
    attachment_name  TEXT,  -- original filename as supplied by the platform/user, display only
    attachment_mime  TEXT   -- best-effort mime type, NULL if unknown
);

-- One row per independently-configured bot ("claude-support-telegram",
-- "hermes-sales-discord", etc). Credentials/allowlist/action_overrides are
-- JSON blobs rather than fixed columns since their shape varies by
-- platform and grows without a schema migration every time — mirrors how
-- messages.text already stays schema-free per platform. Not FK-enforced
-- against jobs/messages (this project doesn't enforce FKs on any of its
-- append-only log tables either), so deleting an instance never risks a
-- cascade against its own history.
CREATE TABLE IF NOT EXISTS bot_instances (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    platform           TEXT NOT NULL,                -- telegram | discord | slack
    backend            TEXT NOT NULL DEFAULT 'cli',   -- this instance's own default backend
    enabled            INTEGER NOT NULL DEFAULT 1,
    credentials        TEXT NOT NULL,                 -- JSON, shape depends on platform
    allowed_user_ids   TEXT NOT NULL DEFAULT '[]',    -- JSON array
    action_overrides   TEXT NOT NULL DEFAULT '{}',    -- JSON, same shape as backends.yaml's action_overrides
    can_target         TEXT NOT NULL DEFAULT '[]',    -- JSON array of bot_instances.id this instance may command (agent_control)
    model              TEXT,                          -- optional per-instance model override, passed through to this instance's backend
    desktop_session_key TEXT,                         -- links to one specific chat/session in the ui/hermes_gateway backend, NULL if none created yet
    custom_instructions TEXT,                          -- optional persona/instructions prepended to every prompt this instance routes through router.ask()
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_started_at    TEXT,
    last_error         TEXT
);

-- A named group of bot instances plus a chosen collaboration strategy.
-- `config` is JSON, strategy-specific (member instance ids, roles, or for
-- strategy='custom' a full step graph) — see bot/swarm/strategies.py.
CREATE TABLE IF NOT EXISTS swarms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    strategy     TEXT NOT NULL,   -- fanout_synthesize | leader_vote | sequential_relay | decompose_delegate | custom
    config       TEXT NOT NULL,   -- JSON, strategy-specific
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- One row per triggered run. `steps` is a live-updated JSON array (each
-- member's status/result as it completes) so the dashboard can poll for
-- genuine in-progress state, not just a final result. Individual member
-- calls still show up as ordinary rows in `jobs`, tagged via
-- jobs.swarm_run_id = swarms_runs.swarm_run_id, so the Jobs tab needs no
-- separate swarm-aware code path.
CREATE TABLE IF NOT EXISTS swarm_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    swarm_id       INTEGER NOT NULL,
    swarm_run_id   TEXT NOT NULL UNIQUE,  -- uuid4 hex, correlates member jobs
    status         TEXT NOT NULL DEFAULT 'running',  -- running|success|failed|cancelled
    prompt         TEXT NOT NULL,
    result         TEXT,
    error          TEXT,
    steps          TEXT,               -- JSON, live/final per-step progress
    requested_by   TEXT,
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    duration_ms    INTEGER
);

-- A browsable grouping of jobs/messages for the same (instance_id, chat_id)
-- pair that happened close together in time — see _get_or_create_session().
-- Populated at write time, not computed on read, so listing/filtering stays
-- index-backed as history grows (mirrors how jobs/swarm_runs are handled).
CREATE TABLE IF NOT EXISTS sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id       INTEGER,
    chat_id           TEXT,
    title             TEXT NOT NULL DEFAULT '',
    started_at        TEXT NOT NULL,
    last_activity_at  TEXT NOT NULL,
    item_count        INTEGER NOT NULL DEFAULT 0
);

-- Mobile/device API keys — a growing multi-device credential set, unlike
-- the single legacy DASHBOARD_TOKEN env value. Only a hash is ever stored;
-- the plaintext is returned once, at creation, and never again.
CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT
);

-- One row per device's current FCM registration token, tied to the mobile
-- api_keys row that registered it — revoking the key (api_keys.revoked_at)
-- should stop paging that device too, so revoke_api_key() deletes the
-- matching rows here in the same call.
CREATE TABLE IF NOT EXISTS push_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id    INTEGER NOT NULL,
    fcm_token     TEXT NOT NULL UNIQUE,
    updated_at    TEXT NOT NULL
);

-- Live presence for a paired device — one row per api_keys id, upserted on
-- every authenticated request (see verify_api_key()) rather than via a
-- separate heartbeat endpoint, since every real client call already proves
-- the device is alive. "Online" is computed at read time (now - last_seen
-- < window), not stored, so nothing needs to age it out on disconnect.
CREATE TABLE IF NOT EXISTS device_presence (
    api_key_id    INTEGER PRIMARY KEY,
    platform      TEXT,
    app_version   TEXT,
    device_model  TEXT,   -- e.g. "Pixel 8 Pro" — real hardware model, not the user-typed pairing label
    os_version    TEXT,   -- e.g. "Android 14"
    last_seen     TEXT NOT NULL
);

-- User-added training phrases for the Support Bot's TF-IDF intent
-- classifier (bot/support_bot/model.py), on top of the hand-authored
-- baseline in bot/support_bot/training_data.py's EXAMPLES. Lets someone
-- improve recognition for a phrasing the classifier missed without
-- editing code — see the Training tab.
CREATE TABLE IF NOT EXISTS support_bot_phrases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase     TEXT NOT NULL,
    intent     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Self-monitoring log for the Support Bot's hybrid classifier
-- (bot/support_bot/hybrid.py) — every single classification, both
-- sub-models' independent verdicts, and which one the hybrid trusted.
-- This is what the Training tab's model-health panel is computed from —
-- real logged behavior, not a guess.
CREATE TABLE IF NOT EXISTS support_bot_classifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    text              TEXT NOT NULL,
    tfidf_intent      TEXT NOT NULL,
    tfidf_confidence  REAL NOT NULL,
    nn_intent         TEXT NOT NULL,
    nn_confidence     REAL NOT NULL,
    final_intent      TEXT NOT NULL,
    final_confidence  REAL NOT NULL,
    source            TEXT NOT NULL,   -- 'ensemble' | 'tfidf' | 'nn' | 'unknown'
    agreed            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_component ON telemetry_events(component, ts);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(platform, chat_id, id);
CREATE INDEX IF NOT EXISTS idx_bot_instances_platform ON bot_instances(platform);
CREATE INDEX IF NOT EXISTS idx_swarm_runs_swarm ON swarm_runs(swarm_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_instance ON sessions(instance_id, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(instance_id, chat_id, last_activity_at);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_push_tokens_key ON push_tokens(api_key_id);
CREATE INDEX IF NOT EXISTS idx_support_bot_classifications_ts ON support_bot_classifications(ts);
"""
# idx_jobs_instance / idx_jobs_swarm_run / idx_messages_instance are created
# in _migrate(), not here — on a pre-existing DB, jobs/messages get their
# instance_id/swarm_run_id columns via ALTER TABLE in _migrate(), which
# runs *after* this script; indexing them here would fail with "no such
# column" the first time this runs against an existing database.


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
        # Multiple paired devices can poll/write concurrently — wait out a
        # brief lock instead of raising "database is locked" immediately.
        _conn.execute("PRAGMA busy_timeout=5000;")
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema patches for databases created before a
    column existed — CREATE TABLE IF NOT EXISTS in SCHEMA only helps fresh
    installs; an existing messages table from before multi-platform support
    needs its new column added explicitly."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "platform" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN platform TEXT NOT NULL DEFAULT 'telegram'")
    if "instance_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN instance_id INTEGER")
    if "attachment_path" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_path TEXT")
    if "attachment_name" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_name TEXT")
    if "attachment_mime" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_mime TEXT")
    if "attachment_size" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_size INTEGER")
    if "thumbnail_path" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN thumbnail_path TEXT")

    if "session_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER")

    job_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "instance_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN instance_id INTEGER")
    if "swarm_run_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN swarm_run_id TEXT")
    if "session_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN session_id INTEGER")
    if "chat_id" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN chat_id TEXT")

    instance_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bot_instances)").fetchall()}
    if "can_target" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN can_target TEXT NOT NULL DEFAULT '[]'")
    if "model" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN model TEXT")
    if "desktop_session_key" not in instance_cols:
        # Links this instance to one specific already-created chat/session in
        # a real desktop agent app — see bot/backends/ui_backend.py and
        # hermes_gateway_backend.py. NULL means "no session created yet" —
        # those two backends must never fall back to "whatever's open" when
        # this is NULL; see Router.create_session().
        conn.execute("ALTER TABLE bot_instances ADD COLUMN desktop_session_key TEXT")
    if "custom_instructions" not in instance_cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN custom_instructions TEXT")

    presence_cols = {row["name"] for row in conn.execute("PRAGMA table_info(device_presence)").fetchall()}
    if "device_model" not in presence_cols:
        conn.execute("ALTER TABLE device_presence ADD COLUMN device_model TEXT")
    if "os_version" not in presence_cols:
        conn.execute("ALTER TABLE device_presence ADD COLUMN os_version TEXT")

    # Safe to create now — the columns above are guaranteed to exist by
    # this point, whether this is a fresh install (created in SCHEMA) or an
    # upgrade (just ALTERed in).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_instance ON jobs(instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_swarm_run ON jobs(swarm_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_instance ON messages(instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")


def init_db() -> None:
    conn = get_conn()
    with _lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


# ------------------------------------------------------------ sessions ----

SESSION_GAP_MINUTES = 30


def _get_or_create_session(
    conn: sqlite3.Connection,
    instance_id: Optional[int],
    chat_id: Optional[Any],
    first_text: str = "",
) -> Optional[int]:
    """Buckets activity for (instance_id, chat_id) into a session row, reusing
    the most recent one if it's still "current" (last activity within
    SESSION_GAP_MINUTES). Must be called with `_lock` already held by the
    caller — this issues its own execute() calls on the shared connection,
    not a separate one, to stay inside the caller's transaction.

    Returns None when instance_id is unset (pre-multi-instance/"legacy"
    activity) — nothing meaningful to bucket a session under."""
    if instance_id is None:
        return None
    chat_key = str(chat_id) if chat_id is not None else None
    now = _now()
    row = conn.execute(
        "SELECT id, last_activity_at FROM sessions WHERE instance_id=? AND "
        "(chat_id=? OR (chat_id IS NULL AND ? IS NULL)) ORDER BY last_activity_at DESC LIMIT 1",
        (instance_id, chat_key, chat_key),
    ).fetchone()
    if row:
        last = datetime.fromisoformat(row["last_activity_at"])
        if datetime.now(timezone.utc) - last <= timedelta(minutes=SESSION_GAP_MINUTES):
            conn.execute(
                "UPDATE sessions SET last_activity_at=?, item_count=item_count+1 WHERE id=?",
                (now, row["id"]),
            )
            return row["id"]
    title = (first_text or "").strip().replace("\n", " ")[:60] or "New session"
    cur = conn.execute(
        "INSERT INTO sessions (instance_id, chat_id, title, started_at, last_activity_at, item_count) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (instance_id, chat_key, title, now, now),
    )
    return cur.lastrowid


def get_session(session_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def list_sessions(
    instance_id: Optional[int] = None,
    q: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    conn = get_conn()
    clauses = []
    params: list[Any] = []
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
    if q:
        clauses.append("title LIKE ?")
        params.append(f"%{q}%")
    if since:
        clauses.append("last_activity_at>=?")
        params.append(since)
    if until:
        clauses.append("last_activity_at<=?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM sessions {where} ORDER BY last_activity_at DESC LIMIT ?", params
    ).fetchall()


def count_legacy_items(instance_id: int) -> int:
    """Rows predating the sessions feature (session_id IS NULL) for one
    instance — surfaced as a synthetic "Before sessions" bucket rather than
    backfilled, since bucketing thousands of historical rows on upgrade is a
    materially riskier migration than this file's usual additive ALTERs."""
    conn = get_conn()
    msgs = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE instance_id=? AND session_id IS NULL", (instance_id,)
    ).fetchone()["c"]
    jobs = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE instance_id=? AND session_id IS NULL", (instance_id,)
    ).fetchone()["c"]
    return msgs + jobs


def get_legacy_items(instance_id: int) -> dict[str, list[sqlite3.Row]]:
    conn = get_conn()
    return {
        "messages": conn.execute(
            "SELECT * FROM messages WHERE instance_id=? AND session_id IS NULL ORDER BY id ASC",
            (instance_id,),
        ).fetchall(),
        "jobs": conn.execute(
            "SELECT * FROM jobs WHERE instance_id=? AND session_id IS NULL ORDER BY id ASC",
            (instance_id,),
        ).fetchall(),
    }


def get_session_items(session_id: int) -> dict[str, list[sqlite3.Row]]:
    conn = get_conn()
    return {
        "messages": conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall(),
        "jobs": conn.execute(
            "SELECT * FROM jobs WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall(),
    }


# ---------------------------------------------------------------- jobs ----

def create_job(
    action_type: str,
    backend: str,
    user_id: int,
    prompt: str,
    instance_id: Optional[int] = None,
    swarm_run_id: Optional[str] = None,
    chat_id: Optional[Any] = None,
) -> int:
    conn = get_conn()
    with _lock:
        chat_key = str(chat_id) if chat_id is not None else None
        session_id = _get_or_create_session(conn, instance_id, chat_key, prompt)
        cur = conn.execute(
            "INSERT INTO jobs (action_type, backend, status, user_id, prompt, created_at, instance_id, swarm_run_id, chat_id, session_id) "
            "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (action_type, backend, user_id, prompt, _now(), instance_id, swarm_run_id, chat_key, session_id),
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


def list_jobs(
    limit: int = 50,
    status: Optional[str] = None,
    instance_id: Optional[int] = None,
    swarm_run_id: Optional[str] = None,
) -> list[sqlite3.Row]:
    conn = get_conn()
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


# ------------------------------------------------- support bot training ---
# User-added phrases layered on top of training_data.py's hand-authored
# EXAMPLES — see bot/support_bot/model.py's load_examples()/reload().
def list_support_bot_phrases() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM support_bot_phrases ORDER BY intent, id").fetchall()


def add_support_bot_phrase(phrase: str, intent: str) -> int:
    phrase = (phrase or "").strip()
    intent = (intent or "").strip()
    if not phrase or not intent:
        raise ValueError("both phrase and intent are required")
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO support_bot_phrases (phrase, intent, created_at) VALUES (?, ?, ?)",
            (phrase, intent, _now()),
        )
        conn.commit()
        return cur.lastrowid


def delete_support_bot_phrase(phrase_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM support_bot_phrases WHERE id=?", (phrase_id,))
        conn.commit()


def log_support_bot_classification(
    text: str,
    tfidf_intent: str,
    tfidf_confidence: float,
    nn_intent: str,
    nn_confidence: float,
    final_intent: str,
    final_confidence: float,
    source: str,
    agreed: bool,
) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO support_bot_classifications "
            "(ts, text, tfidf_intent, tfidf_confidence, nn_intent, nn_confidence, final_intent, final_confidence, source, agreed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), text, tfidf_intent, tfidf_confidence, nn_intent, nn_confidence, final_intent, final_confidence, source, 1 if agreed else 0),
        )
        conn.commit()


def get_support_bot_classification_stats(limit: int = 500) -> dict[str, Any]:
    """Self-monitoring summary for the Training tab — computed from the
    last `limit` real classifications, not a static guess. Empty/zeroed
    fields if nothing's been classified yet."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM support_bot_classifications ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    total = len(rows)
    if total == 0:
        return {
            "total": 0, "agreement_rate": 0.0, "unknown_rate": 0.0,
            "avg_tfidf_confidence": 0.0, "avg_nn_confidence": 0.0, "avg_final_confidence": 0.0,
            "source_counts": {}, "recent": [],
        }
    agreed = sum(1 for r in rows if r["agreed"])
    unknown = sum(1 for r in rows if r["final_intent"] == "unknown")
    source_counts: dict[str, int] = {}
    for r in rows:
        source_counts[r["source"]] = source_counts.get(r["source"], 0) + 1
    return {
        "total": total,
        "agreement_rate": round(agreed / total, 3),
        "unknown_rate": round(unknown / total, 3),
        "avg_tfidf_confidence": round(sum(r["tfidf_confidence"] for r in rows) / total, 3),
        "avg_nn_confidence": round(sum(r["nn_confidence"] for r in rows) / total, 3),
        "avg_final_confidence": round(sum(r["final_confidence"] for r in rows) / total, 3),
        "source_counts": source_counts,
        "recent": [dict(r) for r in rows[:15]],
    }


# ---------------------------------------------------------- api keys ------
# Mobile/device credentials — see SCHEMA's api_keys comment. Plaintext only
# ever exists in create_api_key()'s return value; every other accessor sees
# hashes/metadata only.

def create_api_key(label: str) -> tuple[int, str]:
    plaintext = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO api_keys (label, key_hash, created_at) VALUES (?, ?, ?)",
            (label, key_hash, _now()),
        )
        conn.commit()
        return cur.lastrowid, plaintext


def list_api_keys() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT id, label, created_at, last_used_at, revoked_at FROM api_keys ORDER BY created_at DESC").fetchall()


def update_api_key_label(key_id: int, label: str) -> None:
    """Renames a paired device's label — the dashboard's Paired Devices
    "Save Devices List Data" action, so a device auto-paired under a generic
    name (or a stale manually-typed one) can be corrected in place instead
    of revoking and re-pairing just to fix a name."""
    label = (label or "").strip()
    if not label:
        raise ValueError("label can't be empty")
    conn = get_conn()
    with _lock:
        cur = conn.execute("UPDATE api_keys SET label=? WHERE id=?", (label, key_id))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"api key {key_id} not found")


def revoke_api_key(key_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (_now(), key_id))
        conn.execute("DELETE FROM push_tokens WHERE api_key_id=?", (key_id,))
        conn.execute("DELETE FROM device_presence WHERE api_key_id=?", (key_id,))
        conn.commit()


def purge_revoked_keys() -> int:
    """Permanently deletes every already-revoked api_keys row — the
    dashboard's "Clear Revoked Devices" action. Revoking already tears down
    a device's presence/push rows immediately (see revoke_api_key above);
    this just removes the now-inert row itself so the Paired Devices list
    doesn't accumulate dead entries forever. Returns the number removed."""
    conn = get_conn()
    with _lock:
        cur = conn.execute("DELETE FROM api_keys WHERE revoked_at IS NOT NULL")
        conn.commit()
        return cur.rowcount


def list_devices() -> list[sqlite3.Row]:
    """Paired, unrevoked devices with their live presence, if any — used by
    /api/devices and the WebSocket broadcaster. "Online" is left for the
    caller to compute from last_seen, so both can share the exact same
    freshness window without this layer hardcoding one."""
    conn = get_conn()
    return conn.execute(
        "SELECT ak.id, ak.label, ak.created_at, ak.last_used_at, "
        "dp.platform, dp.app_version, dp.device_model, dp.os_version, dp.last_seen "
        "FROM api_keys ak LEFT JOIN device_presence dp ON dp.api_key_id = ak.id "
        "WHERE ak.revoked_at IS NULL ORDER BY ak.created_at DESC"
    ).fetchall()


def upsert_push_token(api_key_id: int, fcm_token: str) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO push_tokens (api_key_id, fcm_token, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(fcm_token) DO UPDATE SET api_key_id=excluded.api_key_id, updated_at=excluded.updated_at",
            (api_key_id, fcm_token, _now()),
        )
        conn.commit()


def list_push_tokens() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute(
        "SELECT pt.* FROM push_tokens pt JOIN api_keys ak ON ak.id = pt.api_key_id WHERE ak.revoked_at IS NULL"
    ).fetchall()


def verify_api_key(
    plaintext: str,
    platform: Optional[str] = None,
    app_version: Optional[str] = None,
    device_model: Optional[str] = None,
    os_version: Optional[str] = None,
) -> Optional[int]:
    """Also upserts device_presence on every successful call — piggybacking
    presence tracking on the auth check every mobile request already makes,
    rather than a separate heartbeat endpoint. All device fields are
    optional (COALESCE keeps whatever was last known if this particular
    caller didn't send them) since not every route bothers threading device
    headers through. `device_model`/`os_version` are the real hardware model
    and OS release (e.g. "Pixel 8 Pro" / "Android 14") — distinct from the
    user-typed pairing label, so devices are identifiable even when someone
    left the label as something generic."""
    if not plaintext:
        return None
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (key_hash,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (_now(), row["id"]))
        conn.execute(
            "INSERT INTO device_presence (api_key_id, platform, app_version, device_model, os_version, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(api_key_id) DO UPDATE SET "
            "platform=COALESCE(excluded.platform, device_presence.platform), "
            "app_version=COALESCE(excluded.app_version, device_presence.app_version), "
            "device_model=COALESCE(excluded.device_model, device_presence.device_model), "
            "os_version=COALESCE(excluded.os_version, device_presence.os_version), "
            "last_seen=excluded.last_seen",
            (row["id"], platform, app_version, device_model, os_version, _now()),
        )
        conn.commit()
        return row["id"]


# -------------------------------------------------------------- chat ------

def log_message(
    chat_id: Any,
    direction: str,
    source: str,
    text: str,
    platform: str = "telegram",
    user_id: Optional[Any] = None,
    username: str = "",
    instance_id: Optional[int] = None,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
    attachment_mime: Optional[str] = None,
    attachment_size: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
) -> int:
    conn = get_conn()
    with _lock:
        session_id = _get_or_create_session(conn, instance_id, chat_id, text)
        cur = conn.execute(
            "INSERT INTO messages (ts, platform, chat_id, user_id, username, direction, source, text, instance_id, "
            "attachment_path, attachment_name, attachment_mime, attachment_size, thumbnail_path, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), platform, str(chat_id), str(user_id) if user_id is not None else None, username, direction, source, text, instance_id,
             attachment_path, attachment_name, attachment_mime, attachment_size, thumbnail_path, session_id),
        )
        conn.commit()
        return cur.lastrowid


def get_message(message_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()


def list_messages(
    limit: int = 100,
    platform: Optional[str] = None,
    chat_id: Optional[Any] = None,
    after_id: Optional[int] = None,
    instance_id: Optional[int] = None,
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
    if instance_id is not None:
        clauses.append("instance_id=?")
        params.append(instance_id)
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
    tables = ["jobs", "connections_log", "telemetry_events", "mcp_events", "audit_log", "messages", "bot_instances", "swarms", "swarm_runs"]
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


# -------------------------------------------------------------- swarms ----
# Thin SQL wrappers only — `config`/`steps` are passed through as raw JSON
# text, same as jobs.prompt/messages.text; encoding/decoding is the
# caller's job (bot/swarm/engine.py, dashboard/server.py), matching how
# this module stays a plain SQL layer everywhere else.

def create_swarm(name: str, strategy: str, config_json: str, enabled: bool = True) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO swarms (name, strategy, config, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, strategy, config_json, 1 if enabled else 0, _now(), _now()),
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
    params.append(_now())
    params.append(swarm_id)
    conn = get_conn()
    with _lock:
        conn.execute(f"UPDATE swarms SET {', '.join(columns)} WHERE id=?", params)
        conn.commit()


def delete_swarm(swarm_id: int) -> None:
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM swarms WHERE id=?", (swarm_id,))
        conn.commit()


def get_swarm(swarm_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM swarms WHERE id=?", (swarm_id,)).fetchone()


def list_swarms() -> list[sqlite3.Row]:
    conn = get_conn()
    return conn.execute("SELECT * FROM swarms ORDER BY id").fetchall()


def create_swarm_run(swarm_id: int, swarm_run_id: str, prompt: str, requested_by: str = "") -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "INSERT INTO swarm_runs (swarm_id, swarm_run_id, status, prompt, requested_by, created_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (swarm_id, swarm_run_id, prompt, requested_by, _now()),
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
    conn = get_conn()
    with _lock:
        if finished:
            row = conn.execute("SELECT created_at FROM swarm_runs WHERE swarm_run_id=?", (swarm_run_id,)).fetchone()
            finished_at = _now()
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
    conn = get_conn()
    return conn.execute("SELECT * FROM swarm_runs WHERE swarm_run_id=?", (swarm_run_id,)).fetchone()


def list_swarm_runs(swarm_id: Optional[int] = None, limit: int = 50) -> list[sqlite3.Row]:
    conn = get_conn()
    if swarm_id is not None:
        return conn.execute(
            "SELECT * FROM swarm_runs WHERE swarm_id=? ORDER BY id DESC LIMIT ?", (swarm_id, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM swarm_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
