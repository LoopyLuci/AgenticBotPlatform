"""LocalBoard: the work board protocol in ABP's own SQLite file, for when AgentSentinel is not around.

Same semantics as AgentSentinel's board (docs/coordination.md, the contract it implements): every mutation is one
`BEGIN IMMEDIATE` transaction, claims and lock acquisition are all-or-nothing, leases with a lazy sweeper (every call
first requeues what expired), fencing on every state change, dedupe by key, `next` picking the best runnable task, and
the path overlap rule. It has a database of its own (data/coordination/board.db, or ABP_COORDINATION_DB) so it needs
nothing from the rest of ABP and cannot be locked by it.

Threads each get their own connection; SQLite serialises the writers. `clock` (milliseconds) exists for tests.
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from bot.coordination.base import Board
from bot.coordination.model import (
    ACTIVE, HEARTBEAT_INTERVAL_S, MAX_DETAIL, MAX_RESULT, MAX_TITLE, MIN_LEASE_S, SETTABLE, STATES, TERMINAL, BoardError,
    clamp_lease, conflicts, mode_conflicts, norm_resources, now_ms,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS coord_tasks (
    id TEXT PRIMARY KEY, board TEXT NOT NULL, key TEXT, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0, needs TEXT NOT NULL DEFAULT '[]', deps TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '{}', max_attempts INTEGER NOT NULL DEFAULT 3, attempts INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open', owner TEXT, lease_until INTEGER, fence INTEGER NOT NULL DEFAULT 0,
    result TEXT, note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS coord_tasks_board ON coord_tasks(board, state);
CREATE INDEX IF NOT EXISTS coord_tasks_key ON coord_tasks(board, key);
CREATE TABLE IF NOT EXISTS coord_locks (
    resource TEXT NOT NULL, owner TEXT NOT NULL, mode TEXT NOT NULL, task TEXT, lease_until INTEGER NOT NULL,
    fence INTEGER NOT NULL, PRIMARY KEY (resource, owner)
);
CREATE INDEX IF NOT EXISTS coord_locks_task ON coord_locks(task);
CREATE TABLE IF NOT EXISTS coord_presence (
    agent TEXT PRIMARY KEY, status TEXT NOT NULL, task TEXT, capabilities TEXT NOT NULL DEFAULT '[]',
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coord_meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
INSERT OR IGNORE INTO coord_meta(k, v) VALUES ('fence', 0);
"""


def default_path() -> Path:
    explicit = os.environ.get("ABP_COORDINATION_DB", "").strip()
    if explicit:
        return Path(explicit)
    from bot.envfile import PROJECT_ROOT

    return PROJECT_ROOT / "data" / "coordination" / "board.db"


def _loads(text: Optional[str], default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _locked_message(holders: list[dict]) -> str:
    h = holders[0]
    what = h.get("title") or h.get("task") or "something"
    return f"{h['resource']} is held by {h['agent']} ({what})"


class LocalBoard(Board):
    name = "local"
    blocking = False

    def __init__(self, path: Optional[Path | str] = None, *, clock: Optional[Callable[[], int]] = None,
                 min_lease_s: float = MIN_LEASE_S):
        self.path = Path(path) if path else default_path()
        self.clock = clock or now_ms
        self.min_lease_s = min_lease_s
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=500)
        self._seq = 0
        self._ev_lock = threading.Lock()
        self.listeners: list[Callable[[dict], None]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        for attempt in range(40):          # concurrent first opens of a new file can race on the schema
            try:
                conn.executescript(SCHEMA)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 39:
                    raise
                time.sleep(0.02 * (attempt + 1))

    # ------------------------------------------------------------ plumbing
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000;")
            for attempt in range(40):
                try:
                    if str(conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower() != "wal":
                        conn.execute("PRAGMA journal_mode=WAL;")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    time.sleep(0.01 * (attempt + 1))
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
        self._local = threading.local()

    @contextmanager
    def _tx(self):
        """BEGIN IMMEDIATE ... COMMIT around one mutation; yields (conn, now_ms, events). Expired leases are
        swept first so every operation sees a board where nothing stale is still holding anything."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        events: list[tuple] = []
        try:
            now = int(self.clock())
            self._sweep(conn, now, events)
            yield conn, now, events
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        for ev in events:
            self._emit(*ev)

    def _stale(self, now: int) -> bool:
        return bool(self._conn().execute(
            "SELECT 1 FROM coord_tasks WHERE state IN ('claimed','running','review') AND lease_until IS NOT NULL "
            "AND lease_until < ? UNION ALL SELECT 1 FROM coord_locks WHERE lease_until < ? LIMIT 1",
            (now, now)).fetchone())

    def _fresh(self) -> None:
        """Lazy sweep for reads: only opens a write transaction when something has actually expired."""
        if self._stale(int(self.clock())):
            with self._tx():
                pass

    def _emit(self, kind: str, task: Optional[dict] = None, extra: Optional[dict] = None) -> None:
        with self._ev_lock:
            self._seq += 1
            ev: dict[str, Any] = {"seq": self._seq, "ts": int(self.clock()), "kind": kind}
            if task:
                ev.update(board=task["board"], task=task["id"], title=task["title"], owner=task["owner"],
                          fence=task["fence"])
            ev.update(extra or {})
            self._events.append(ev)
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - a listener must never break the board
                pass

    def events(self, since: int = 0, limit: int = 100) -> list[dict]:
        with self._ev_lock:
            return [e for e in self._events if e["seq"] > since][-limit:]

    @property
    def last_hint(self) -> dict:
        """Why the calling thread's last `next` found nothing: counts blocked by deps, by locks, by capabilities."""
        return getattr(self._local, "hint", {})

    def _next_fence(self, conn: sqlite3.Connection) -> int:
        conn.execute("UPDATE coord_meta SET v = v + 1 WHERE k = 'fence'")
        return int(conn.execute("SELECT v FROM coord_meta WHERE k = 'fence'").fetchone()[0])

    # --------------------------------------------------------------- rows
    @staticmethod
    def _task(row: sqlite3.Row, *, full: bool = True) -> dict:
        t = {
            "id": row["id"], "board": row["board"], "key": row["key"], "title": row["title"],
            "priority": row["priority"], "needs": _loads(row["needs"], []), "deps": _loads(row["deps"], []),
            "scope": _loads(row["scope"], {}), "max_attempts": row["max_attempts"], "attempts": row["attempts"],
            "state": row["state"], "owner": row["owner"], "lease_until": row["lease_until"], "fence": row["fence"],
            "note": row["note"], "created_by": row["created_by"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "version": row["version"],
        }
        if full:
            t["detail"] = row["detail"]
            t["result"] = _loads(row["result"], None)
        return t

    @staticmethod
    def _lock(row: sqlite3.Row) -> dict:
        return {"resource": row["resource"], "mode": row["mode"], "owner": row["owner"], "task": row["task"],
                "lease_until": row["lease_until"], "fence": row["fence"]}

    def _get(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM coord_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise BoardError("NOT_FOUND", f"no task {task_id}", status=404)
        return row

    def _touch(self, conn: sqlite3.Connection, task_id: str, **cols: Any) -> None:
        cols["updated_at"] = int(self.clock())
        names = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE coord_tasks SET {names}, version = version + 1 WHERE id = ?", (*cols.values(), task_id))

    # ------------------------------------------------------------- sweeper
    def _sweep(self, conn: sqlite3.Connection, now: int, events: list) -> None:
        rows = conn.execute(
            "SELECT * FROM coord_tasks WHERE state IN ('claimed','running','review') AND lease_until IS NOT NULL "
            "AND lease_until < ?", (now,)).fetchall()
        for row in rows:
            attempts = row["attempts"] + 1
            conn.execute("DELETE FROM coord_locks WHERE task = ?", (row["id"],))
            if attempts >= row["max_attempts"]:
                self._touch(conn, row["id"], state="failed", attempts=attempts, lease_until=None,
                            note=f"lease expired {attempts} times")
            else:
                self._touch(conn, row["id"], state="open", attempts=attempts, owner=None, lease_until=None,
                            note=f"lease expired (attempt {attempts} of {row['max_attempts']})")
            fresh = self._task(self._get(conn, row["id"]), full=False)
            events.append(("board.task.expired", {**fresh, "owner": row["owner"]}))
            if fresh["state"] == "failed":
                events.append(("board.task.failed", fresh))
        for lk in conn.execute("SELECT * FROM coord_locks WHERE lease_until < ?", (now,)).fetchall():
            conn.execute("DELETE FROM coord_locks WHERE resource = ? AND owner = ?", (lk["resource"], lk["owner"]))
            events.append(("board.lock.expired", None, {"resource": lk["resource"], "owner": lk["owner"]}))

    # --------------------------------------------------------------- locks
    def _holders(self, conn: sqlite3.Connection, wanted: list[tuple[str, str]], agent: str) -> list[dict]:
        out: list[dict] = []
        for lk in conn.execute("SELECT * FROM coord_locks WHERE owner != ?", (agent,)).fetchall():
            for res, mode in wanted:
                if conflicts(res, lk["resource"]) and mode_conflicts(mode, lk["mode"]):
                    title = None
                    if lk["task"]:
                        t = conn.execute("SELECT title FROM coord_tasks WHERE id = ?", (lk["task"],)).fetchone()
                        title = t["title"] if t else None
                    out.append({"resource": lk["resource"], "requested": res, "mode": lk["mode"], "agent": lk["owner"],
                                "task": lk["task"], "title": title, "lease_until": lk["lease_until"]})
                    break
        return out

    def _take(self, conn: sqlite3.Connection, wanted: list[tuple[str, str]], agent: str, task: Optional[str],
              lease_until: int, fence: int) -> list[dict]:
        out = []
        for res, mode in wanted:
            conn.execute("INSERT OR REPLACE INTO coord_locks(resource, owner, mode, task, lease_until, fence) "
                         "VALUES (?,?,?,?,?,?)", (res, agent, mode, task, lease_until, fence))
            out.append({"resource": res, "mode": mode, "owner": agent, "task": task, "lease_until": lease_until,
                        "fence": fence})
        return out

    def acquire(self, agent, resources, *, task=None, lease_s=None):
        wanted = norm_resources(resources)
        if not wanted:
            raise BoardError("BAD_REQUEST", "no resources", status=400)
        lease = clamp_lease(lease_s, self.min_lease_s)
        with self._tx() as (conn, now, events):
            holders = self._holders(conn, wanted, agent)
            if holders:
                raise BoardError("LOCKED", _locked_message(holders), holders=holders)
            fence = self._next_fence(conn)
            locks = self._take(conn, wanted, agent, task, now + int(lease * 1000), fence)
            for lk in locks:
                events.append(("board.lock.acquired", None, {"resource": lk["resource"], "owner": agent,
                                                              "task": task, "fence": fence}))
            self._seen(conn, agent, now)
        return {"locks": locks, "fence": fence}

    def lock_heartbeat(self, agent, fence, *, lease_s=None):
        lease = clamp_lease(lease_s, self.min_lease_s)
        with self._tx() as (conn, now, _events):
            until = now + int(lease * 1000)
            n = conn.execute("UPDATE coord_locks SET lease_until = ? WHERE owner = ? AND fence = ?",
                             (until, agent, fence)).rowcount
            if not n:
                raise BoardError("STALE_FENCE", "no locks held with that fence")
            self._seen(conn, agent, now)
        return {"lease_until": until}

    def release_locks(self, agent, fence, resources=None):
        with self._tx() as (conn, _now, events):
            if resources:
                n = 0
                for res in resources:
                    n += conn.execute("DELETE FROM coord_locks WHERE owner = ? AND fence = ? AND resource = ?",
                                      (agent, fence, res)).rowcount
            else:
                n = conn.execute("DELETE FROM coord_locks WHERE owner = ? AND fence = ?", (agent, fence)).rowcount
            if n:
                events.append(("board.lock.released", None, {"owner": agent, "fence": fence, "count": n}))
        return {"released": n}

    def list_locks(self, *, resource_prefix="", owner=""):
        self._fresh()
        conn = self._conn()
        out = []
        for lk in conn.execute("SELECT * FROM coord_locks ORDER BY resource").fetchall():
            if resource_prefix and not lk["resource"].startswith(resource_prefix):
                continue
            if owner and lk["owner"] != owner:
                continue
            d = self._lock(lk)
            if lk["task"]:
                t = conn.execute("SELECT title, board FROM coord_tasks WHERE id = ?", (lk["task"],)).fetchone()
                if t:
                    d["title"], d["board"] = t["title"], t["board"]
            out.append(d)
        return out

    # --------------------------------------------------------------- tasks
    def create_task(self, board, title, *, key=None, detail="", priority=0, needs=None, deps=None, scope=None,
                    max_attempts=3, dedupe_done=False, created_by=""):
        title = (title or "").strip()
        if not board or not title:
            raise BoardError("BAD_REQUEST", "board and title are required", status=400)
        if len(title) > MAX_TITLE:
            raise BoardError("BAD_REQUEST", f"title is longer than {MAX_TITLE} characters", status=400)
        if len(detail or "") > MAX_DETAIL:
            raise BoardError("BAD_REQUEST", f"detail is longer than {MAX_DETAIL} characters", status=400)
        scope = dict(scope or {})
        norm_resources(scope.get("resources"))             # validates
        with self._tx() as (conn, now, events):
            if key:
                states = ("open", "claimed", "running", "review") + (("done",) if dedupe_done else ())
                marks = ",".join("?" * len(states))
                row = conn.execute(f"SELECT * FROM coord_tasks WHERE board = ? AND key = ? AND state IN ({marks}) "
                                   "ORDER BY created_at, rowid LIMIT 1", (board, key, *states)).fetchone()
                if row is not None:
                    return self._task(row), True
            task_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO coord_tasks(id, board, key, title, detail, priority, needs, deps, scope, max_attempts, "
                "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, board, key, title, detail or "", int(priority), json.dumps(list(needs or [])),
                 json.dumps(list(deps or [])), json.dumps(scope), max(1, int(max_attempts)), created_by, now, now))
            task = self._task(self._get(conn, task_id))
            events.append(("board.task.created", task))
        return task, False

    def get_task(self, task_id):
        self._fresh()
        return self._task(self._get(self._conn(), task_id))

    def list_tasks(self, board=None, *, state=None, owner=None, limit=200):
        self._fresh()
        sql, args = "SELECT * FROM coord_tasks WHERE 1=1", []
        for col, val in (("board", board), ("state", state), ("owner", owner)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        sql += " ORDER BY created_at, rowid LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        return [self._task(r, full=False) for r in self._conn().execute(sql, args).fetchall()]

    def _blocked_deps(self, conn: sqlite3.Connection, deps: list[str]) -> list[str]:
        out = []
        for dep in deps:
            row = conn.execute("SELECT state FROM coord_tasks WHERE id = ?", (dep,)).fetchone()
            if row is None or row["state"] != "done":
                out.append(dep)
        return out

    def _do_claim(self, conn, now, events, row, agent, lease) -> dict:
        pending = self._blocked_deps(conn, _loads(row["deps"], []))
        if pending:
            raise BoardError("DEPS", f"waiting for {len(pending)} task(s) to be done: {', '.join(pending[:5])}")
        wanted = norm_resources(_loads(row["scope"], {}).get("resources"))
        if wanted:
            holders = self._holders(conn, wanted, agent)
            if holders:
                raise BoardError("LOCKED", _locked_message(holders), holders=holders)
        fence = self._next_fence(conn)
        until = now + int(lease * 1000)
        locks = self._take(conn, wanted, agent, row["id"], until, fence) if wanted else []
        self._touch(conn, row["id"], state="claimed", owner=agent, lease_until=until, fence=fence)
        task = self._task(self._get(conn, row["id"]))
        events.append(("board.task.claimed", task))
        for lk in locks:
            events.append(("board.lock.acquired", None, {"resource": lk["resource"], "owner": agent,
                                                          "task": row["id"], "fence": fence}))
        self._seen(conn, agent, now, status="busy", task=row["id"])
        return {"task": task, "fence": fence, "locks": locks}

    def claim(self, task_id, agent, *, lease_s=None, expect_version=None):
        lease = clamp_lease(lease_s, self.min_lease_s)
        with self._tx() as (conn, now, events):
            row = self._get(conn, task_id)
            if row["state"] != "open":
                raise BoardError("TAKEN", f"task is {row['state']}" + (f" (owner {row['owner']})" if row["owner"] else ""))
            if expect_version is not None and int(expect_version) != row["version"]:
                raise BoardError("VERSION", f"task is at version {row['version']}, not {expect_version}")
            return self._do_claim(conn, now, events, row, agent, lease)

    def next(self, board, agent, *, capabilities=None, lease_s=None):
        lease = clamp_lease(lease_s, self.min_lease_s)
        caps = set(capabilities or [])
        hint = {"blocked_by_deps": 0, "blocked_by_locks": 0, "needs_capabilities": 0}
        self._local.hint = hint
        with self._tx() as (conn, now, events):
            self._seen(conn, agent, now, status="idle", capabilities=sorted(caps) if capabilities is not None else None)
            rows = conn.execute("SELECT * FROM coord_tasks WHERE board = ? AND state = 'open' "
                                "ORDER BY priority DESC, created_at ASC, rowid ASC LIMIT 1000", (board,)).fetchall()
            for row in rows:
                if not set(_loads(row["needs"], [])) <= caps:
                    hint["needs_capabilities"] += 1
                    continue
                if self._blocked_deps(conn, _loads(row["deps"], [])):
                    hint["blocked_by_deps"] += 1
                    continue
                wanted = norm_resources(_loads(row["scope"], {}).get("resources"))
                if wanted and self._holders(conn, wanted, agent):
                    hint["blocked_by_locks"] += 1
                    continue
                return self._do_claim(conn, now, events, row, agent, lease)
        return None

    def heartbeat(self, task_id, agent, fence, *, lease_s=None, progress=None):
        lease = clamp_lease(lease_s, self.min_lease_s)
        with self._tx() as (conn, now, _events):
            row = self._get(conn, task_id)
            self._check_owner(row, agent, fence)
            until = now + int(lease * 1000)
            self._touch(conn, task_id, lease_until=until)
            conn.execute("UPDATE coord_locks SET lease_until = ? WHERE task = ? AND owner = ?", (until, task_id, agent))
            self._seen(conn, agent, now)
        return {"lease_until": until}

    @staticmethod
    def _check_owner(row: sqlite3.Row, agent: str, fence: int) -> None:
        if int(fence) != row["fence"]:
            raise BoardError("STALE_FENCE", f"fence {fence} is stale (task is at {row['fence']}, owner {row['owner']})")
        if row["owner"] != agent or row["state"] not in ACTIVE:
            raise BoardError("NOT_OWNER", f"{agent} does not own this task (state {row['state']})")

    def set_state(self, task_id, agent, fence, state, *, result=None, note=""):
        if state not in SETTABLE:
            raise BoardError("BAD_REQUEST", f"state must be one of {sorted(SETTABLE)}", status=400)
        blob = None if result is None else json.dumps(result, default=str)
        if blob is not None and len(blob) > MAX_RESULT:
            raise BoardError("BAD_REQUEST", f"result is larger than {MAX_RESULT} bytes", status=413)
        with self._tx() as (conn, now, events):
            row = self._get(conn, task_id)
            self._check_owner(row, agent, fence)
            cols: dict[str, Any] = {}
            if note:
                cols["note"] = note[:2000]
            if blob is not None:
                cols["result"] = blob
            finished = state in ("done", "failed", "released")
            if state in ("done", "failed"):
                cols.update(state=state, lease_until=None)
            elif state == "released":
                cols.update(state="open", owner=None, lease_until=None)
            else:
                cols.update(state=state)
            if finished:
                conn.execute("DELETE FROM coord_locks WHERE task = ?", (task_id,))
            self._touch(conn, task_id, **cols)
            task = self._task(self._get(conn, task_id))
            events.append((f"board.task.{state}", {**task, "owner": agent}))
            self._seen(conn, agent, now, status="idle" if finished else "busy", task=None if finished else task_id)
        return task

    def cancel(self, task_id, *, agent="", note=""):
        with self._tx() as (conn, _now, events):
            row = self._get(conn, task_id)
            if row["state"] in TERMINAL:
                return self._task(row)
            conn.execute("DELETE FROM coord_locks WHERE task = ?", (task_id,))
            self._touch(conn, task_id, state="cancelled", lease_until=None, note=(note or f"cancelled by {agent}")[:2000])
            task = self._task(self._get(conn, task_id))
            events.append(("board.task.cancelled", task))
        return task

    # ------------------------------------------------------------ presence
    def _seen(self, conn, agent: str, now: int, *, status: Optional[str] = None, task: Optional[str] = None,
              capabilities: Optional[list] = None) -> None:
        row = conn.execute("SELECT * FROM coord_presence WHERE agent = ?", (agent,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO coord_presence(agent, status, task, capabilities, last_seen) VALUES (?,?,?,?,?)",
                         (agent, status or "idle", task, json.dumps(capabilities or []), now))
            return
        conn.execute("UPDATE coord_presence SET status = ?, task = ?, capabilities = ?, last_seen = ? WHERE agent = ?",
                     (status or row["status"], task if status is not None else row["task"],
                      json.dumps(capabilities) if capabilities is not None else row["capabilities"], now, agent))

    def presence(self, agent, status, *, task=None, capabilities=None):
        with self._tx() as (conn, now, _events):
            self._seen(conn, agent, now, status=status, task=task, capabilities=capabilities)
        return {"agent": agent, "status": status, "task": task}

    def list_presence(self):
        self._fresh()
        now = int(self.clock())
        out = []
        for r in self._conn().execute("SELECT * FROM coord_presence ORDER BY agent").fetchall():
            out.append({"agent": r["agent"], "status": r["status"], "task": r["task"],
                        "capabilities": _loads(r["capabilities"], []), "last_seen": r["last_seen"],
                        "gone": now - r["last_seen"] > 3 * HEARTBEAT_INTERVAL_S * 1000})
        return out

    # ------------------------------------------------------------- summary
    def summary(self, board=None):
        """Per board: counts by state, who holds what, expiring leases, idle agents, blocked tasks and why (the same
        shape as AgentSentinel's). `agents` lists the idle and the gone."""
        self._fresh()
        conn = self._conn()
        now = int(self.clock())
        agents = self.list_presence()
        live_caps: set[str] = set()
        for a in agents:
            if not a["gone"]:
                live_caps |= set(a["capabilities"])
        idle = [a["agent"] for a in agents if a["status"] == "idle" and not a["task"] and not a["gone"]]
        gone = [a["agent"] for a in agents if a["gone"]]
        states = {r["id"]: r["state"] for r in conn.execute("SELECT id, state FROM coord_tasks").fetchall()}
        all_locks = self.list_locks()
        names = [board] if board else [r[0] for r in conn.execute("SELECT DISTINCT board FROM coord_tasks ORDER BY board")]
        boards: dict[str, dict] = {}
        for name in names:
            counts = {st: 0 for st in STATES}
            for r in conn.execute("SELECT state, COUNT(*) AS n FROM coord_tasks WHERE board = ? GROUP BY state", (name,)):
                counts[r["state"]] = r["n"]
            holders = [lk for lk in all_locks if lk.get("board") == name]
            expiring = [{"task": r["id"], "title": r["title"], "owner": r["owner"], "fence": r["fence"],
                         "lease_until": r["lease_until"], "in_ms": r["lease_until"] - now}
                        for r in conn.execute("SELECT * FROM coord_tasks WHERE board = ? AND state IN "
                                              "('claimed','running','review') AND lease_until IS NOT NULL ORDER BY lease_until", (name,))
                        if r["lease_until"] - now < HEARTBEAT_INTERVAL_S * 1000]
            blocked: list[dict] = []
            for r in conn.execute("SELECT * FROM coord_tasks WHERE board = ? AND state = 'open' "
                                  "ORDER BY priority DESC, created_at, rowid", (name,)):
                waiting = [d for d in _loads(r["deps"], []) if states.get(d) != "done"]
                missing = [n for n in _loads(r["needs"], []) if n not in live_caps]
                wanted = norm_resources(_loads(r["scope"], {}).get("resources"))
                held = self._holders(conn, wanted, "") if wanted else []
                if waiting:
                    blocked.append({"task": r["id"], "title": r["title"], "reason": "deps", "waiting_for": waiting})
                elif held:
                    blocked.append({"task": r["id"], "title": r["title"], "reason": "locks", "holders": held})
                elif missing:
                    blocked.append({"task": r["id"], "title": r["title"], "reason": "needs", "needs": missing})
            boards[name] = {"counts": counts, "holders": holders, "expiring": expiring, "idle_agents": idle,
                            "blocked": blocked}
        return {"boards": boards, "agents": {"idle": idle, "gone": gone}}
