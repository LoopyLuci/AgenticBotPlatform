"""Where Video Studio keeps its state: data/video ($ABP_VIDEO_DIR overrides), with video.db (SQLite: jobs, assets),
assets/ (the library's files), work/ (one folder per job while it renders) and uploads/.

One connection per call (SQLite serialises writers; WAL lets the API read while the worker writes)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_init_lock = threading.Lock()
_ready: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    workflow TEXT NOT NULL,
    backend TEXT NOT NULL,
    params TEXT NOT NULL,
    parent_id TEXT,
    actor TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    asset_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    timeout_s INTEGER NOT NULL DEFAULT 3600,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    cancel INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    file TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    job_id TEXT,
    parent_id TEXT,
    workflow TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    seed INTEGER,
    meta TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_created ON assets(created);
"""


def root() -> Path:
    over = os.environ.get("ABP_VIDEO_DIR")
    if over:
        d = Path(over)
    else:
        from bot import envfile
        d = Path(envfile.PROJECT_ROOT) / "data" / "video"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sub(name: str) -> Path:
    d = root() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return root() / "video.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        key = str(path)
        if key not in _ready:
            with _init_lock:
                if key not in _ready:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.executescript(SCHEMA)
                    _ready.add(key)
        yield con
        con.commit()
    finally:
        con.close()


def row(r: sqlite3.Row | None, json_keys: tuple = ()) -> dict | None:
    if r is None:
        return None
    out: dict[str, Any] = dict(r)
    for k in json_keys:
        try:
            out[k] = json.loads(out.get(k) or "{}")
        except ValueError:
            out[k] = {}
    return out


def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def now() -> float:
    return time.time()
