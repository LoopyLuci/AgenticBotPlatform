"""Durable per-instance state: a `runtime_state` table keyed by `unit@instance` plus key, JSON values.

Both versions of a unit share one namespace, so state survives a swap. A shadow branch gets a copy-on-write
namespace: reads fall through to the base namespace, writes (and deletes, as tombstones) go to the branch's own rows.
The database path is injectable; `default_store()` opens the application database (bot/db.py's DB_PATH).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_state (
    owner      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    deleted    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, key)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold every other user of the store off and release the database file while the caller replaces it (a
        snapshot restore); the connection is reopened on the way out."""
        with self._lock:
            self._conn.close()
            try:
                yield
            finally:
                self._conn = self._open()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def namespace(self, owner: str, base: Optional["StateNamespace"] = None) -> "StateNamespace":
        return StateNamespace(self, owner, base)

    def drop_owner(self, owner: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM runtime_state WHERE owner = ?", (owner,))

    # -- row access (used by StateNamespace) --
    def _row(self, owner: str, key: str) -> Optional[tuple[Optional[str], int]]:
        with self._lock:
            row = self._conn.execute("SELECT value, deleted FROM runtime_state WHERE owner = ? AND key = ?",
                                     (owner, key)).fetchone()
        return (row[0], row[1]) if row else None

    def _put(self, owner: str, key: str, value: Optional[str], deleted: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO runtime_state (owner, key, value, deleted, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner, key) DO UPDATE SET value = excluded.value, deleted = excluded.deleted, "
                "updated_at = excluded.updated_at", (owner, key, value, 1 if deleted else 0, _now()))

    def _remove(self, owner: str, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM runtime_state WHERE owner = ? AND key = ?", (owner, key))

    def _rows(self, owner: str) -> list[tuple[str, int]]:
        with self._lock:
            return [(r[0], r[1]) for r in
                    self._conn.execute("SELECT key, deleted FROM runtime_state WHERE owner = ?", (owner,))]


class StateNamespace:
    """get / set / delete over one owner's keys; with a `base`, copy-on-write over it."""

    def __init__(self, store: StateStore, owner: str, base: Optional["StateNamespace"] = None) -> None:
        self._store = store
        self.owner = owner
        self._base = base

    def get(self, key: str, default: Any = None) -> Any:
        row = self._store._row(self.owner, key)
        if row is not None:
            return default if row[1] else json.loads(row[0])
        if self._base is not None:
            return self._base.get(key, default)
        return default

    def set(self, key: str, value: Any) -> None:
        self._store._put(self.owner, key, json.dumps(value), False)

    def delete(self, key: str) -> None:
        if self._base is not None:
            self._store._put(self.owner, key, None, True)       # a tombstone hides the base's value
        else:
            self._store._remove(self.owner, key)

    def keys(self) -> list[str]:
        own = {k: bool(deleted) for k, deleted in self._store._rows(self.owner)}
        keys = {k for k, deleted in own.items() if not deleted}
        if self._base is not None:
            keys |= {k for k in self._base.keys() if k not in own}
        return sorted(keys)

    def items(self) -> Iterator[tuple[str, Any]]:
        for key in self.keys():
            yield key, self.get(key)

    def clear(self) -> None:
        for key in self.keys():
            self.delete(key)


def default_store() -> StateStore:
    """The application database."""
    from bot import db

    return StateStore(db.DB_PATH)
