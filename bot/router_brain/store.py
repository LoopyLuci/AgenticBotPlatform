"""router.db: the router's decisions, statistics, cooldowns, learning log, training examples and policy versions.

A separate file from bot.db (like traces.db), so the router can be reset, copied or inspected on its own. Each call
opens a short-lived connection: the router writes a few rows per turn, and this keeps it correct across threads and
across tests that point the state directory elsewhere.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,                 -- auto | sticky | failover | advise
    instance_id INTEGER,
    task_excerpt TEXT,
    task_class TEXT,
    class_source TEXT,                  -- rules | keywords | classifier | given
    chosen TEXT,
    explored INTEGER NOT NULL DEFAULT 0,
    policy_version INTEGER,
    parent_id INTEGER,                  -- the decision this one replaced (failover, re-route)
    detail TEXT NOT NULL,               -- JSON: the classification and every candidate's reasoning
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | failed | advice
    error_kind TEXT,
    error TEXT,
    latency_ms INTEGER,
    tokens INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    rating INTEGER,
    feedback_note TEXT,
    corrected_class TEXT,
    preferred_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_decisions_chosen ON decisions(chosen);
CREATE TABLE IF NOT EXISTS model_stats (
    model TEXT NOT NULL,
    task_class TEXT NOT NULL,           -- a class, or '*' for every class together
    ok REAL NOT NULL DEFAULT 0,         -- successful calls, decayed over time
    fail REAL NOT NULL DEFAULT 0,
    up REAL NOT NULL DEFAULT 0,         -- good-choice feedback, decayed
    down REAL NOT NULL DEFAULT 0,
    latency_ms REAL,                    -- moving average
    calls INTEGER NOT NULL DEFAULT 0,   -- every call ever, not decayed
    last_ok REAL,
    last_fail REAL,
    last_error_kind TEXT,
    last_error TEXT,
    streak INTEGER NOT NULL DEFAULT 0,  -- consecutive failures (negative: consecutive successes)
    updated REAL NOT NULL,
    PRIMARY KEY (model, task_class)
);
CREATE TABLE IF NOT EXISTS cooldowns (
    key TEXT PRIMARY KEY,               -- "provider/model", or "provider/*" for a whole provider
    until REAL NOT NULL,
    kind TEXT,
    reason TEXT,
    strikes INTEGER NOT NULL DEFAULT 1,
    since REAL NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    model TEXT,
    decision_id INTEGER,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    task_class TEXT,
    preferred_model TEXT,
    source TEXT NOT NULL DEFAULT 'manual',   -- manual | feedback
    decision_id INTEGER
);
CREATE TABLE IF NOT EXISTS policy_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    note TEXT,
    policy TEXT NOT NULL
);
"""

MAX_DECISIONS = 20_000
MAX_EVENTS = 20_000

_lock = threading.RLock()
_ready: set[str] = set()
_writes = 0


def db_path() -> Path:
    explicit = os.environ.get("ABP_ROUTER_DB", "").strip()
    if explicit:
        return Path(explicit)
    from bot.agent_runtime.state import state_dir

    return state_dir() / "router.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        key = str(path)
        if key not in _ready or not path.exists():
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _ready.add(key)
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    found = rows(sql, params)
    return found[0] if found else None


def execute(sql: str, params: tuple = ()) -> int:
    """Runs one write; returns lastrowid."""
    global _writes
    with _lock, connect() as conn:
        cur = conn.execute(sql, params)
        _writes += 1
        if _writes % 200 == 0:
            _prune(conn)
        return int(cur.lastrowid or 0)


def _prune(conn: sqlite3.Connection) -> None:
    for table, cap in (("decisions", MAX_DECISIONS), ("events", MAX_EVENTS)):
        conn.execute(f"DELETE FROM {table} WHERE id <= (SELECT MAX(id) FROM {table}) - ?", (cap,))
    try:
        from bot.router_brain import policy

        days = float(policy.current()["retention_days"])
        conn.execute("DELETE FROM decisions WHERE ts < ?", (time.time() - days * 86400,))
        conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - days * 86400,))
    except Exception:  # noqa: BLE001 — pruning is housekeeping
        pass


def event(kind: str, message: str, *, model: Optional[str] = None, decision_id: Optional[int] = None, **data: Any) -> None:
    """One line in the learning log: what the router noticed or changed, and why."""
    try:
        execute("INSERT INTO events(ts, kind, model, decision_id, message, data) VALUES (?,?,?,?,?,?)",
                (time.time(), kind, model, decision_id, message, json.dumps(data, default=str) if data else None))
    except sqlite3.Error:
        pass


def reset(*, keep_policy: bool = True, keep_examples: bool = True) -> None:
    with _lock, connect() as conn:
        for table in ("decisions", "model_stats", "cooldowns", "events"):
            conn.execute(f"DELETE FROM {table}")
        if not keep_examples:
            conn.execute("DELETE FROM examples")
        if not keep_policy:
            conn.execute("DELETE FROM policy_versions")
