"""Local Chat sessions and their messages.

One small SQLite file in the agent state folder (data/agent/localchat/localchat.db), a connection per call (nothing cached, a reload
loses nothing). A session keeps its own choices - model, mode, autonomy, workspace, cloud assist - so reopening it after a restart
(or a crash) puts everything back as it was. A turn's reply is stored when it finishes; a turn that never finished is marked as
such on the next start instead of looking complete.

Sessions and worktrees (PLAN-local-chat.md 4b, S1): a session also has a status (working / needs_you / idle / error / done), unread
and the last message read, a group, a parent and a link (fork | task | worker | handoff), its git worktree (path, branch, base,
keep), an effort and when it was archived. `groups` names the sidebar's user groups; `checkpoints` maps a turn to the workspace
checkpoint taken before its tools ran (rewind); `events` records status changes for the sidebar and notifications; `queued` holds
messages waiting for the next turn. Imported sessions carry their `source` and imported messages their `ext_id`. Older databases are migrated in place (columns added, nothing dropped).
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
STATUSES = ("working", "needs_you", "idle", "error", "done")
LINKS = ("", "fork", "task", "worker", "handoff")
EFFORTS = ("", "low", "medium", "high")

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
CREATE TABLE IF NOT EXISTS groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS checkpoints (turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, workspace TEXT NOT NULL,
    commit_id TEXT NOT NULL, created REAL NOT NULL, rewound REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS queued (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
"""

# Columns added after P1: (table, column, declaration). Added when missing; never dropped or changed.
_COLUMNS = (
    ("sessions", "status", "TEXT NOT NULL DEFAULT 'idle'"),
    ("sessions", "unread", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "last_read_id", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "group_id", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "parent_id", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "link", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "worktree", "TEXT NOT NULL DEFAULT '{}'"),
    ("sessions", "effort", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "archived_at", "REAL"),
    ("messages", "reverted", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "source", "TEXT NOT NULL DEFAULT ''"),     # "" = made here; hermes | opencode = imported (bot/localchat/imports.py)
    ("messages", "ext_id", "TEXT NOT NULL DEFAULT ''"),     # an imported message's "<source>:<its id>" (re-imports never duplicate)
)

EDITABLE = ("title", "model", "mode", "autonomy", "workspace", "cloud_assist", "pinned", "archived", "group_id", "effort")


def db_path() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("localchat") / "localchat.db"


def _migrate(conn: sqlite3.Connection) -> None:
    have: dict[str, set] = {}
    for table, column, decl in _COLUMNS:
        if table not in have:
            have[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            have[table].add(column)


@contextlib.contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _session(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["pinned"], d["archived"], d["unread"] = bool(d["pinned"]), bool(d["archived"]), bool(d.get("unread"))
    try:
        d["worktree"] = json.loads(d.get("worktree") or "{}") or None
    except ValueError:
        d["worktree"] = None
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
    if field == "effort" and (value or "") not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(e or '(default)' for e in EFFORTS)}")
    if field == "group_id" and value and not get_group(str(value)):
        raise ValueError(f"no group {value}")
    if field in ("title", "model", "workspace", "group_id", "effort"):
        return str(value or "")[:500]
    return value


def create(*, title: str = "", model: str = "", mode: str = "build", autonomy: int = 2, workspace: str = "",
           cloud_assist: str = "ask", parent_id: str = "", link: str = "", group_id: str = "", effort: str = "") -> dict[str, Any]:
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    vals = {f: _check(f, v) for f, v in (("title", title), ("model", model), ("mode", mode), ("autonomy", autonomy),
                                         ("workspace", workspace), ("cloud_assist", cloud_assist), ("group_id", group_id),
                                         ("effort", effort))}
    if link not in LINKS:
        raise ValueError(f"link must be one of {', '.join(x for x in LINKS if x)}")
    with _db() as c:
        c.execute("INSERT INTO sessions (id, title, created, updated, model, mode, autonomy, workspace, cloud_assist, parent_id, link, "
                  "group_id, effort) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, vals["title"], now, now, vals["model"], vals["mode"], vals["autonomy"], vals["workspace"],
                   vals["cloud_assist"], str(parent_id or ""), link, vals["group_id"], vals["effort"]))
    return get(sid) or {}


def get(sid: str) -> Optional[dict[str, Any]]:
    with _db() as c:
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return _session(row) if row else None


def list_sessions(*, query: str = "", archived: bool = False, limit: int = 100, parent_id: Optional[str] = None,
                  status: str = "", workspace: str = "", model: str = "", source: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sessions WHERE archived=?"
    args: list[Any] = [1 if archived else 0]
    for col, val in (("parent_id", parent_id), ("status", status or None), ("workspace", workspace or None), ("model", model or None),
                     ("source", source)):
        if val is not None:
            sql += f" AND {col}=?"
            args.append(val)
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
    if "archived" in vals:
        vals["archived_at"] = time.time() if vals["archived"] else None
    with _db() as c:
        n = c.execute(f"UPDATE sessions SET {', '.join(f'{f}=?' for f in vals)}, updated=? WHERE id=?",
                      [*vals.values(), time.time(), sid]).rowcount
    if not n:
        raise KeyError(sid)
    return get(sid) or {}


def delete(sid: str) -> bool:
    with _db() as c:
        for table in ("messages", "turns", "checkpoints", "events", "queued"):
            c.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
        c.execute("UPDATE sessions SET parent_id='' WHERE parent_id=?", (sid,))     # children stay, unlinked
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


def messages(sid: str, *, after: int = 0, limit: int = 500, include_reverted: bool = True) -> list[dict[str, Any]]:
    with _db() as c:
        rows = c.execute("SELECT * FROM messages WHERE session_id=? AND id>? " + ("" if include_reverted else "AND reverted=0 ")
                         + "ORDER BY id LIMIT ?", (sid, int(after), int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reverted"] = bool(d.get("reverted"))
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


def turns(sid: str) -> list[dict[str, Any]]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM turns WHERE session_id=? ORDER BY started", (sid,)).fetchall()]


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
        try:
            set_status(t["session_id"], "error", reason="interrupted")
        except KeyError:
            pass
    with _db() as c:                         # nothing can be working or waiting on a person after a restart
        c.execute("UPDATE sessions SET status='idle' WHERE status IN ('working', 'needs_you')")
    return len(stale)


# ---- status, unread, events ------------------------------------------------------------------------------------------------
def set_status(sid: str, status: str, *, unread: bool = False, **data: Any) -> Optional[dict[str, Any]]:
    """Change a session's status (recorded as an event); `unread` also marks it unread (a turn ended; reading clears it).
    Returns the session when something changed, None when nothing did."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with _db() as c:
        row = c.execute("SELECT status, unread FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError(sid)
        if row["status"] == status and (not unread or row["unread"]):
            return None
        c.execute("UPDATE sessions SET status=?, unread=? WHERE id=?", (status, 1 if unread or row["unread"] else 0, sid))
        c.execute("INSERT INTO events (session_id, at, kind, data) VALUES (?,?,?,?)",
                  (sid, time.time(), "status", json.dumps({"status": status, "was": row["status"], **data}, default=str)))
    return get(sid)


def mark_read(sid: str) -> dict[str, Any]:
    with _db() as c:
        last = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
        if not c.execute("UPDATE sessions SET unread=0, last_read_id=? WHERE id=?", (int(last), sid)).rowcount:
            raise KeyError(sid)
    return get(sid) or {}


def add_event(sid: str, kind: str, **data: Any) -> int:
    with _db() as c:
        return int(c.execute("INSERT INTO events (session_id, at, kind, data) VALUES (?,?,?,?)",
                             (sid, time.time(), kind, json.dumps(data, default=str))).lastrowid)


def events(*, after: int = 0, session_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM events WHERE id>?", [int(after)]
    if session_id:
        sql += " AND session_id=?"
        args.append(session_id)
    with _db() as c:
        rows = c.execute(sql + " ORDER BY id LIMIT ?", [*args, int(limit)]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(d["data"] or "{}")
        except ValueError:
            d["data"] = {}
        out.append(d)
    return out


# ---- the worktree record ------------------------------------------------------------------------------------------------------
def set_worktree(sid: str, wt: Optional[dict]) -> dict[str, Any]:
    with _db() as c:
        if not c.execute("UPDATE sessions SET worktree=?, updated=? WHERE id=?", (json.dumps(wt or {}), time.time(), sid)).rowcount:
            raise KeyError(sid)
    return get(sid) or {}


def sessions_with_worktrees() -> list[dict[str, Any]]:
    with _db() as c:
        rows = c.execute("SELECT * FROM sessions WHERE worktree NOT IN ('', '{}', 'null')").fetchall()
    return [s for s in (_session(r) for r in rows) if s["worktree"]]


# ---- groups --------------------------------------------------------------------------------------------------------------------
def _group_name(name: str) -> str:
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise ValueError("a group needs a name")
    return name


def create_group(name: str) -> dict[str, Any]:
    name = _group_name(name)
    gid = uuid.uuid4().hex[:10]
    with _db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM groups").fetchone()[0]
        c.execute("INSERT INTO groups (id, name, created, position) VALUES (?,?,?,?)", (gid, name, time.time(), int(pos)))
    return get_group(gid) or {}


def get_group(gid: str) -> Optional[dict[str, Any]]:
    with _db() as c:
        row = c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    return dict(row) if row else None


def list_groups() -> list[dict[str, Any]]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM groups ORDER BY position, created").fetchall()]


def rename_group(gid: str, name: str) -> dict[str, Any]:
    name = _group_name(name)
    with _db() as c:
        if not c.execute("UPDATE groups SET name=? WHERE id=?", (name, gid)).rowcount:
            raise KeyError(gid)
    return get_group(gid) or {}


def delete_group(gid: str) -> bool:
    """The group goes; its sessions stay (ungrouped)."""
    with _db() as c:
        c.execute("UPDATE sessions SET group_id='' WHERE group_id=?", (gid,))
        return c.execute("DELETE FROM groups WHERE id=?", (gid,)).rowcount > 0


# ---- checkpoints (rewind) -----------------------------------------------------------------------------------------------------
def add_checkpoint(turn_id: str, sid: str, workspace: str, commit_id: str) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO checkpoints (turn_id, session_id, workspace, commit_id, created) VALUES (?,?,?,?,?)",
                  (turn_id, sid, workspace, commit_id, time.time()))


def checkpoints(sid: str) -> list[dict[str, Any]]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM checkpoints WHERE session_id=? ORDER BY created", (sid,)).fetchall()]


def mark_rewound(sid: str, turn_id: str) -> int:
    """The turn and every later one are reverted: their messages stay, marked, and their checkpoints are marked rewound.
    Returns how many messages were marked."""
    now = time.time()
    with _db() as c:
        row = c.execute("SELECT started FROM turns WHERE id=? AND session_id=?", (turn_id, sid)).fetchone()
        if row is None:
            raise KeyError(turn_id)
        later = [r["id"] for r in c.execute("SELECT id FROM turns WHERE session_id=? AND started>=?", (sid, row["started"])).fetchall()]
        marks = ",".join("?" * len(later))
        n = c.execute(f"UPDATE messages SET reverted=1 WHERE session_id=? AND turn_id IN ({marks})", [sid, *later]).rowcount
        c.execute(f"UPDATE checkpoints SET rewound=? WHERE session_id=? AND turn_id IN ({marks}) AND rewound IS NULL", [now, sid, *later])
    return int(n)


# ---- messages waiting for the next turn ----------------------------------------------------------------------------------------
def queue_message(sid: str, text: str) -> int:
    with _db() as c:
        return int(c.execute("INSERT INTO queued (session_id, text, created) VALUES (?,?,?)", (sid, text, time.time())).lastrowid)


def queued(sid: str) -> list[dict[str, Any]]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM queued WHERE session_id=? ORDER BY id", (sid,)).fetchall()]


def pop_queued(sid: str) -> Optional[str]:
    with _db() as c:
        row = c.execute("SELECT id, text FROM queued WHERE session_id=? ORDER BY id LIMIT 1", (sid,)).fetchone()
        if row is None:
            return None
        c.execute("DELETE FROM queued WHERE id=?", (row["id"],))
    return str(row["text"])


def drop_queued(sid: str, qid: int) -> bool:
    with _db() as c:
        return c.execute("DELETE FROM queued WHERE id=? AND session_id=?", (int(qid), sid)).rowcount > 0
