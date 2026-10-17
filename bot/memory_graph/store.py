"""The memory graph database (data/agent/memory_graph.db, beside the trace store).

Its own file, not a table set in the context vault: the vault is off by default and moves items between storage tiers, while
the graph is small structured counts that must be cheap to read on every recall and safe to wipe or rebuild from the traces.
Every operation opens and closes its own connection (any thread may call).

episodes: one row per agent run (id = run_id). nodes/edges: the graph; an edge's `weight` is the smoothed win rate
(wins + 1) / (n + 2); recency decay is applied when read (`effective_weight`), so the stored number never goes stale.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

NODE_TYPES = ("task_type", "tool", "skill", "file", "symbol", "error", "fix", "model")
RELS = ("used_in", "helped", "caused", "resolved_by", "co_occurred")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, created REAL, project TEXT, task_type TEXT, goal_summary TEXT, tools TEXT, skills TEXT,
    files TEXT, errors TEXT, model TEXT, tokens INTEGER, usd REAL, iterations INTEGER, duration_ms INTEGER,
    outcome TEXT, run_id TEXT, task_id TEXT);
CREATE INDEX IF NOT EXISTS ep_created ON episodes(created);
CREATE INDEX IF NOT EXISTS ep_project ON episodes(project);
CREATE TABLE IF NOT EXISTS mem_nodes (
    id INTEGER PRIMARY KEY, type TEXT NOT NULL, key TEXT NOT NULL, UNIQUE(type, key));
CREATE TABLE IF NOT EXISTS mem_edges (
    src INTEGER NOT NULL, dst INTEGER NOT NULL, rel TEXT NOT NULL, n INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
    fails INTEGER DEFAULT 0, weight REAL DEFAULT 0.5, last_seen REAL DEFAULT 0, PRIMARY KEY (src, dst, rel));
CREATE INDEX IF NOT EXISTS me_dst ON mem_edges(dst, rel);
"""
_JSON_COLS = ("tools", "skills", "files", "errors", "outcome")
_DICT_COLS = ("tools", "outcome")


def db_path(trace_db: Optional[Path] = None) -> Path:
    explicit = os.environ.get("ABP_MEMORY_GRAPH_DB", "").strip()
    if explicit:
        return Path(explicit)
    if trace_db is not None:
        return Path(trace_db).with_name("memory_graph.db")
    from bot.agent_runtime.state import state_dir

    return state_dir() / "memory_graph.db"


def edge_weight(wins: int, n: int) -> float:
    return (wins + 1) / (n + 2)


def decay(last_seen: float, half_life_days: float, now: Optional[float] = None) -> float:
    age_days = max(0.0, ((now or time.time()) - (last_seen or 0.0)) / 86400.0)
    return math.exp(-math.log(2) * age_days / max(0.1, half_life_days))


def effective_weight(row: Any, half_life_days: float, now: Optional[float] = None) -> float:
    """The stored smoothed win rate pulled toward the 0.5 prior as the edge goes unseen."""
    return 0.5 + (float(row["weight"]) - 0.5) * decay(row["last_seen"], half_life_days, now)


class Graph:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else db_path()
        self._ready = False

    def _conn(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        if not self._ready:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            conn.executescript(_SCHEMA)
            self._ready = True
        return conn

    # ---- episodes --------------------------------------------------------------------------------------
    @staticmethod
    def _ep(row: sqlite3.Row) -> dict:
        d = dict(row)
        for c in _JSON_COLS:
            empty: Any = {} if c in _DICT_COLS else []
            try:
                d[c] = json.loads(d[c]) if d.get(c) else empty
            except ValueError:
                d[c] = empty
        return d

    def get_episode(self, ep_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()
            return self._ep(row) if row else None
        finally:
            conn.close()

    def recent_episodes(self, limit: int, project: str = "") -> list[dict]:
        conn = self._conn()
        try:
            if project:
                rows = conn.execute("SELECT * FROM episodes WHERE project=? ORDER BY created DESC LIMIT ?",
                                    (project, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM episodes ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
            return [self._ep(r) for r in rows]
        finally:
            conn.close()

    def add_episode(self, ep: dict, edges: Iterable[tuple], *, max_episodes: int = 5000) -> bool:
        """Write the episode and bump its edges in one transaction. `edges` = (src_type, src_key, rel, dst_type, dst_key,
        won); won=None counts a sighting with no verdict. False if the episode already exists."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM episodes WHERE id=?", (ep["id"],)).fetchone():
                    conn.execute("ROLLBACK")
                    return False
                cols = ("id", "created", "project", "task_type", "goal_summary", "tools", "skills", "files", "errors", "model",
                        "tokens", "usd", "iterations", "duration_ms", "outcome", "run_id", "task_id")
                vals = [json.dumps(ep.get(c), sort_keys=True) if c in _JSON_COLS else ep.get(c) for c in cols]
                conn.execute(f"INSERT INTO episodes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
                now = float(ep.get("created") or time.time())
                for st, sk, rel, dt, dk, won in edges:
                    self._bump(conn, self._node(conn, st, sk), self._node(conn, dt, dk), rel, won, now)
                extra = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] - max_episodes
                if extra > 0:
                    conn.execute("DELETE FROM episodes WHERE id IN (SELECT id FROM episodes ORDER BY created LIMIT ?)", (extra,))
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    # ---- graph -----------------------------------------------------------------------------------------
    @staticmethod
    def _node(conn: sqlite3.Connection, type_: str, key: str) -> int:
        if type_ not in NODE_TYPES:
            raise ValueError(f"unknown node type {type_!r}")
        key = str(key)[:300]
        conn.execute("INSERT OR IGNORE INTO mem_nodes (type, key) VALUES (?, ?)", (type_, key))
        return int(conn.execute("SELECT id FROM mem_nodes WHERE type=? AND key=?", (type_, key)).fetchone()[0])

    @staticmethod
    def _bump(conn: sqlite3.Connection, src: int, dst: int, rel: str, won: Optional[bool], now: float) -> None:
        if rel not in RELS:
            raise ValueError(f"unknown relation {rel!r}")
        conn.execute("INSERT OR IGNORE INTO mem_edges (src, dst, rel) VALUES (?, ?, ?)", (src, dst, rel))
        conn.execute("UPDATE mem_edges SET n = n + 1, wins = wins + ?, fails = fails + ?, last_seen = MAX(last_seen, ?) "
                     "WHERE src=? AND dst=? AND rel=?",
                     (1 if won is True else 0, 1 if won is False else 0, now, src, dst, rel))
        conn.execute("UPDATE mem_edges SET weight = (wins + 1.0) / (n + 2.0) WHERE src=? AND dst=? AND rel=?", (src, dst, rel))

    def edges_to(self, dst_type: str, dst_key: str, rel: str, src_type: Optional[str] = None) -> list[dict]:
        """Edges of `rel` into a node: [{src_type, src_key, n, wins, fails, weight, last_seen}]."""
        conn = self._conn()
        try:
            sql = ("SELECT s.type AS src_type, s.key AS src_key, e.n, e.wins, e.fails, e.weight, e.last_seen FROM mem_edges e "
                   "JOIN mem_nodes s ON s.id = e.src JOIN mem_nodes d ON d.id = e.dst WHERE d.type=? AND d.key=? AND e.rel=?")
            args: list = [dst_type, dst_key, rel]
            if src_type:
                sql += " AND s.type=?"
                args.append(src_type)
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def edges_from(self, src_type: str, src_key: str, rel: str) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT d.type AS dst_type, d.key AS dst_key, e.n, e.wins, e.fails, e.weight, e.last_seen FROM mem_edges e "
                "JOIN mem_nodes s ON s.id = e.src JOIN mem_nodes d ON d.id = e.dst WHERE s.type=? AND s.key=? AND e.rel=?",
                (src_type, src_key, rel)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._conn()
        try:
            return {"episodes": conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
                    "nodes": conn.execute("SELECT COUNT(*) FROM mem_nodes").fetchone()[0],
                    "edges": conn.execute("SELECT COUNT(*) FROM mem_edges").fetchone()[0]}
        finally:
            conn.close()
