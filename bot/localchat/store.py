"""Local Chat sessions and their messages.

One small SQLite file in the agent state folder (data/agent/localchat/localchat.db), a connection per call (nothing cached, a reload
loses nothing). A session keeps its own choices - model, mode, autonomy, workspace, cloud assist - so reopening it after a restart
(or a crash) puts everything back as it was. A turn's reply is stored when it finishes; a turn that never finished is marked as
such on the next start instead of looking complete.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from bot.localchat import CLOUD_ASSIST, MODES

ROLES = ("user", "assistant", "system", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
    model TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'build', autonomy INTEGER NOT NULL DEFAULT 2,
    workspace TEXT NOT NULL DEFAULT '', cloud_assist TEXT NOT NULL DEFAULT 'ask', pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    text TEXT NOT NULL, created REAL NOT NULL, turn_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL DEFAULT 0, tokens_per_s REAL NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, started REAL NOT NULL, finished REAL,
    state TEXT NOT NULL DEFAULT 'running');
"""

EDITABLE = ("title", "model", "mode", "autonomy", "workspace", "cloud_assist", "pinned", "archived")


def db_path() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("localchat") / "localchat.db"


@contextlib.contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _session(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["pinned"], d["archived"] = bool(d["pinned"]), bool(d["archived"])
    return d


def _check(field: str, value: Any) -> Any:
    if field == "mode" and value not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if field == "cloud_assist" and value not in CLOUD_ASSIST:
        raise ValueError(f"cloud_assist must be one of {', '.join(CLOUD_ASSIST)}")
    if field == "autonomy":
        value = int(value)
        if not 0 <= value <= 5:
            raise ValueError("autonomy is 0 (Locked) .. 5 (Sovereign)")
    if field in ("pinned", "archived"):
        return 1 if value else 0
    if field in ("title", "model", "workspace"):
        return str(value or "")[:500]
    return value


def create(*, title: str = "", model: str = "", mode: str = "build", autonomy: int = 2, workspace: str = "",
           cloud_assist: str = "ask") -> dict[str, Any]:
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    vals = {f: _check(f, v) for f, v in (("title", title), ("model", model), ("mode", mode), ("autonomy", autonomy),
                                         ("workspace", workspace), ("cloud_assist", cloud_assist))}
    with _db() as c:
        c.execute("INSERT INTO sessions (id, title, created, updated, model, mode, autonomy, workspace, cloud_assist) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (sid, vals["title"], now, now, vals["model"], vals["mode"], vals["autonomy"],
                                                 vals["workspace"], vals["cloud_assist"]))
    return get(sid) or {}


def get(sid: str) -> Optional[dict[str, Any]]:
    with _db() as c:
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return _session(row) if row else None


def list_sessions(*, query: str = "", archived: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sessions WHERE archived=?"
    args: list[Any] = [1 if archived else 0]
    if query:
        sql += (" AND (title LIKE ? OR id IN (SELECT session_id FROM messages WHERE text LIKE ?))")
        args += [f"%{query}%", f"%{query}%"]
    sql += " ORDER BY pinned DESC, updated DESC LIMIT ?"
    args.append(int(limit))
    with _db() as c:
        return [_session(r) for r in c.execute(sql, args).fetchall()]


def update(sid: str, **fields: Any) -> dict[str, Any]:
    bad = [f for f in fields if f not in EDITABLE]
    if bad:
        raise ValueError(f"cannot change {', '.join(bad)}")
    vals = {f: _check(f, v) for f, v in fields.items()}
    if not vals:
        return get(sid) or {}
    with _db() as c:
        n = c.execute(f"UPDATE sessions SET {', '.join(f'{f}=?' for f in vals)}, updated=? WHERE id=?",
                      [*vals.values(), time.time(), sid]).rowcount
    if not n:
        raise KeyError(sid)
    return get(sid) or {}


def delete(sid: str) -> bool:
    with _db() as c:
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM turns WHERE session_id=?", (sid,))
        return c.execute("DELETE FROM sessions WHERE id=?", (sid,)).rowcount > 0


def add_message(sid: str, role: str, text: str, *, turn_id: str = "", model: str = "", tokens: int = 0,
                tokens_per_s: float = 0.0, meta: Optional[dict] = None) -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    now = time.time()
    with _db() as c:
        mid = c.execute("INSERT INTO messages (session_id, role, text, created, turn_id, model, tokens, tokens_per_s, meta) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (sid, role, text, now, turn_id, model, int(tokens or 0),
                                                       float(tokens_per_s or 0), json.dumps(meta or {}))).lastrowid
        c.execute("UPDATE sessions SET updated=? WHERE id=?", (now, sid))
        if role == "user":                   # the first message names an untitled session
            c.execute("UPDATE sessions SET title=? WHERE id=? AND title=''", (" ".join(text.split())[:60], sid))
    return int(mid)


def messages(sid: str, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    with _db() as c:
        rows = c.execute("SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?", (sid, int(after), int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except ValueError:
            d["meta"] = {}
        out.append(d)
    return out


def start_turn(sid: str) -> str:
    tid = uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute("INSERT INTO turns (id, session_id, started) VALUES (?,?,?)", (tid, sid, time.time()))
    return tid


def finish_turn(tid: str, state: str) -> None:
    with _db() as c:
        c.execute("UPDATE turns SET finished=?, state=? WHERE id=?", (time.time(), state, tid))


def running_turns(sid: Optional[str] = None) -> list[dict[str, Any]]:
    with _db() as c:
        if sid:
            rows = c.execute("SELECT * FROM turns WHERE state='running' AND session_id=?", (sid,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM turns WHERE state='running'").fetchall()
    return [dict(r) for r in rows]


def recover() -> int:
    """At start: a turn still 'running' belongs to a process that is gone. Mark it interrupted and say so in its session, so the
    conversation never looks complete when it is not. Returns how many were recovered."""
    stale = running_turns()
    for t in stale:
        finish_turn(t["id"], "interrupted")
        add_message(t["session_id"], "system", "The previous reply was interrupted (ABP restarted) and did not finish.",
                    turn_id=t["id"])
    return len(stale)
