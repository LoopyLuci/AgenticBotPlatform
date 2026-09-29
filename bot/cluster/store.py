"""The cluster's records, in their own SQLite file (data/cluster/cluster.sqlite3).

    runs         jobs this machine runs (for itself or for a peer): spec, what was reserved, state, exit code, result
    placements   jobs this machine submitted to the cluster: which node took each one, and the retries
    groups       gangs and arrays this machine submitted: their member jobs

Records are JSON documents keyed by id, with state and timestamps as columns for listing. A run that was running
when ABP stopped is marked "lost" at the next start: its processes died with ABP (the job object kills them).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_path_override: Optional[Path] = None
TABLES = ("runs", "placements", "groups")
ACTIVE = ("accepted", "held", "starting", "running")


def db_path() -> Path:
    if _path_override:
        return _path_override
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "data" / "cluster" / "cluster.sqlite3"


def use_path(path: Optional[Path]) -> None:
    """Point the store somewhere else (tests)."""
    global _conn, _path_override
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _path_override = None, path


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            p = db_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
            _conn.execute("PRAGMA journal_mode=WAL")
            for t in TABLES:
                _conn.execute(f"CREATE TABLE IF NOT EXISTS {t} (id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                              "created REAL NOT NULL, updated REAL NOT NULL, doc TEXT NOT NULL)")
                _conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_created ON {t}(created)")
            now = time.time()
            for row in _conn.execute("SELECT id, doc FROM runs WHERE state IN (?,?,?,?)", ACTIVE).fetchall():
                doc = json.loads(row[1])
                doc.update(state="lost", error="ABP stopped while this job ran", finished=now)
                _conn.execute("UPDATE runs SET state=?, updated=?, doc=? WHERE id=?", ("lost", now, json.dumps(doc), row[0]))
        return _conn


def put(table: str, doc: dict) -> dict:
    assert table in TABLES
    now = time.time()
    doc.setdefault("created", now)
    doc["updated"] = now
    with _lock:
        conn().execute(f"INSERT INTO {table}(id, state, created, updated, doc) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated=excluded.updated, doc=excluded.doc",
                       (doc["id"], doc.get("state", ""), doc["created"], now, json.dumps(doc, default=str)))
    return doc


def update(table: str, rid: str, **changes: Any) -> Optional[dict]:
    with _lock:
        doc = get(table, rid)
        if doc is None:
            return None
        doc.update(changes)
        return put(table, doc)


def get(table: str, rid: str) -> Optional[dict]:
    assert table in TABLES
    row = conn().execute(f"SELECT doc FROM {table} WHERE id=?", (rid,)).fetchone()
    return json.loads(row[0]) if row else None


def list_(table: str, *, states: Optional[tuple] = None, limit: int = 100) -> list[dict]:
    assert table in TABLES
    q, args = f"SELECT doc FROM {table}", []
    if states:
        q += f" WHERE state IN ({','.join('?' * len(states))})"
        args += list(states)
    q += " ORDER BY created DESC LIMIT ?"
    args.append(int(limit))
    return [json.loads(r[0]) for r in conn().execute(q, args).fetchall()]


def prune(table: str, older_than_s: float) -> int:
    cutoff = time.time() - older_than_s
    with _lock:
        cur = conn().execute(f"DELETE FROM {table} WHERE updated < ? AND state NOT IN ({','.join('?' * len(ACTIVE))})",
                             (cutoff, *ACTIVE))
        return cur.rowcount
