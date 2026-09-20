"""Append-only event store on SQLite (WAL), with a hash chain.

* Multi-process safe: the release script, the pipeline, the server and the CLI can
  all write and read at once (each call opens its own short-lived connection;
  appends take a write lock so the chain stays linear).
* Tamper-evident: every event's hash covers the previous event's hash, so an
  edited or deleted row is detected by `verify_chain()`.
* Telemetry must never break a build: `safe_append` swallows and reports (once)
  any failure to write.
* Open formats: `export_jsonl` writes the log as plain JSON lines.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import SCHEMA_VERSION, events

GENESIS = "0" * 64
_REPO_ROOT = Path(__file__).resolve().parent.parent
_warned = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL    NOT NULL,
    v      INTEGER NOT NULL,
    kind   TEXT    NOT NULL,
    run_id TEXT,
    step   TEXT,
    data   TEXT    NOT NULL,
    prev   TEXT    NOT NULL,
    hash   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run  ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, seq);
CREATE TABLE IF NOT EXISTS anchor (id INTEGER PRIMARY KEY CHECK (id = 1), seq INTEGER NOT NULL, hash TEXT NOT NULL);
"""


def default_db_path(root: Optional[Path] = None) -> Path:
    """ABP_CICD_DB, else <ABP_HOME or the checkout>/data/cicd/events.db."""
    explicit = os.environ.get("ABP_CICD_DB", "").strip()
    if explicit:
        return Path(explicit)
    base = Path(root) if root else Path(os.environ.get("ABP_HOME") or _REPO_ROOT)
    return base / "data" / "cicd" / "events.db"


def _digest(prev: str, v: int, ts: float, kind: str, run_id: Optional[str], step: Optional[str], data: str) -> str:
    material = "\x1f".join([prev, str(v), repr(ts), kind, run_id or "", step or "", data])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class EventStore:
    def __init__(self, path: Path | str, kinds: Optional[dict] = None):
        self.path = Path(path)
        self.kinds = kinds          # None = the CI/CD schema; another store passes its own allow-list
        self._ready = False

    # ---- connection -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        if not self._ready:
            self._initialise(conn)
            self._ready = True
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _initialise(conn: sqlite3.Connection) -> None:
        """First use of a store. Several processes can arrive here at once (the
        release, the pipeline it launches, the server) and switching a fresh
        database to WAL needs an exclusive lock that busy_timeout does not always
        wait for, so retry instead of failing the caller."""
        deadline = time.monotonic() + 30
        while True:
            try:
                if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    # ---- writing ----------------------------------------------------------
    def append(self, kind: str, data: Optional[dict] = None, *, run_id: Optional[str] = None,
               step: Optional[str] = None, ts: Optional[float] = None) -> dict:
        clean = events.sanitize(kind, data, self.kinds)
        payload = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stamp = float(ts if ts is not None else time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                if row is None:
                    anchor = conn.execute("SELECT hash FROM anchor WHERE id = 1").fetchone()
                    prev = anchor["hash"] if anchor else GENESIS
                else:
                    prev = row["hash"]
                h = _digest(prev, SCHEMA_VERSION, stamp, kind, run_id, step, payload)
                cur = conn.execute(
                    "INSERT INTO events (ts, v, kind, run_id, step, data, prev, hash) VALUES (?,?,?,?,?,?,?,?)",
                    (stamp, SCHEMA_VERSION, kind, run_id, step, payload, prev, h))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return {"seq": cur.lastrowid, "ts": stamp, "v": SCHEMA_VERSION, "kind": kind, "run_id": run_id,
                "step": step, "data": clean, "hash": h}

    def safe_append(self, *args: Any, **kwargs: Any) -> Optional[dict]:
        """Never raises: a failure to record must not fail the build."""
        global _warned
        try:
            return self.append(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not _warned:
                _warned = True
                print(f"[abp_cicd] telemetry disabled for this process: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None

    # ---- reading ----------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        return {"seq": r["seq"], "ts": r["ts"], "v": r["v"], "kind": r["kind"], "run_id": r["run_id"],
                "step": r["step"], "data": json.loads(r["data"]), "hash": r["hash"]}

    def events(self, *, since_seq: int = 0, kind: Optional[str] = None, run_id: Optional[str] = None,
               limit: int = 500, descending: bool = False) -> list[dict]:
        where, params = ["seq > ?"], [since_seq]
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        order = "DESC" if descending else "ASC"
        sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY seq {order} LIMIT ?"
        conn = self._connect()
        try:
            return [self._row(r) for r in conn.execute(sql, (*params, max(1, min(limit, 5000)))).fetchall()]
        finally:
            conn.close()

    def select(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Read-only helper for query modules."""
        conn = self._connect()
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    def last_seq(self) -> int:
        rows = self.select("SELECT COALESCE(MAX(seq), 0) AS m FROM events")
        return int(rows[0]["m"])

    def count(self) -> int:
        return int(self.select("SELECT COUNT(*) AS n FROM events")[0]["n"])

    # ---- integrity, retention, export -------------------------------------
    def verify_chain(self) -> dict:
        """Recompute every hash. Returns {ok, count, first_bad_seq, reason}."""
        conn = self._connect()
        try:
            anchor = conn.execute("SELECT seq, hash FROM anchor WHERE id = 1").fetchone()
            prev = anchor["hash"] if anchor else GENESIS
            expect_after = anchor["seq"] if anchor else 0
            count = 0
            for r in conn.execute("SELECT * FROM events ORDER BY seq ASC"):
                count += 1
                if r["prev"] != prev:
                    return {"ok": False, "count": count, "first_bad_seq": r["seq"],
                            "reason": "chain broken (a row was removed, reordered or inserted)"}
                want = _digest(r["prev"], r["v"], r["ts"], r["kind"], r["run_id"], r["step"], r["data"])
                if want != r["hash"]:
                    return {"ok": False, "count": count, "first_bad_seq": r["seq"],
                            "reason": "row contents do not match their hash"}
                if r["seq"] <= expect_after:
                    return {"ok": False, "count": count, "first_bad_seq": r["seq"], "reason": "sequence regressed"}
                prev = r["hash"]
            return {"ok": True, "count": count, "first_bad_seq": None, "reason": ""}
        finally:
            conn.close()

    def prune(self, older_than_days: float) -> int:
        """Delete events older than the cutoff (never the newest one) and record
        the last deleted hash as the new chain anchor so the chain still verifies."""
        cutoff = time.time() - older_than_days * 86400
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                newest = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()["m"]
                last = conn.execute("SELECT seq, hash FROM events WHERE ts < ? AND seq < ? ORDER BY seq DESC LIMIT 1",
                                    (cutoff, newest)).fetchone()
                if last is None:
                    conn.execute("COMMIT")
                    return 0
                # Only a contiguous prefix may be removed, or the chain would break.
                n = conn.execute("DELETE FROM events WHERE seq <= ?", (last["seq"],)).rowcount
                conn.execute("INSERT INTO anchor (id, seq, hash) VALUES (1, ?, ?) "
                             "ON CONFLICT(id) DO UPDATE SET seq = excluded.seq, hash = excluded.hash",
                             (last["seq"], last["hash"]))
                conn.execute("COMMIT")
                return n
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def export_jsonl(self, out: Path | str) -> int:
        n = 0
        with open(out, "w", encoding="utf-8") as fh:
            for ev in self.iter_events():
                fh.write(json.dumps(ev, sort_keys=True, ensure_ascii=False) + "\n")
                n += 1
        return n

    def iter_events(self, batch: int = 1000) -> Iterator[dict]:
        cursor = 0
        while True:
            rows = self.events(since_seq=cursor, limit=batch)
            if not rows:
                return
            yield from rows
            cursor = rows[-1]["seq"]


_stores: dict[str, EventStore] = {}


def get_store(path: Path | str | None = None) -> EventStore:
    """A cached store for `path` (default: default_db_path())."""
    key = str(Path(path) if path else default_db_path())
    if key not in _stores:
        _stores[key] = EventStore(key)
    return _stores[key]
