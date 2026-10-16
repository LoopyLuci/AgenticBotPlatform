"""The conductor's own records: plans, review reports, integrations, the push queue and escalations.

One small SQLite file in the agent state folder (data/agent/conductor/conductor.db). A connection is opened per call, so nothing
here is cached and a reload of this module loses nothing. The push queue is the only place a pending push exists: entries are
`awaiting_approval` until the owner (or Claude, when the owner delegates) approves them; nothing in bot/conductor pushes.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

QUEUE_STATES = ("awaiting_approval", "approved", "rejected")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, goal TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, task_key TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL, created REAL NOT NULL, report TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS reviews_task ON reviews(plan_id, task_key);
CREATE TABLE IF NOT EXISTS integrations (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL,
    status TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, integration_id TEXT NOT NULL, plan_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL, head_sha TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS escalations (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, task_key TEXT NOT NULL,
    reason TEXT NOT NULL, created REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS toolcalls (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, task_key TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, summary TEXT NOT NULL DEFAULT '', data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS toolcalls_task ON toolcalls(plan_id, task_key);
"""


def db_path() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("conductor") / "conductor.db"


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


def _json(text: Optional[str]) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


# ---------------------------------------------------------------- plans
def save_plan(plan: dict) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO plans(id, goal, source, created, data) VALUES (?,?,?,?,?)",
                  (plan["id"], plan["goal"], plan.get("source", ""), time.time(), json.dumps(plan)))


def get_plan(plan_id: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT data FROM plans WHERE id = ?", (plan_id,)).fetchone()
    return _json(row["data"]) if row else None


def list_plans(limit: int = 30) -> list[dict]:
    with _db() as c:
        rows = c.execute("SELECT id, goal, source, created, data FROM plans ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "goal": r["goal"], "source": r["source"], "created": r["created"],
             "tasks": len((_json(r["data"]) or {}).get("tasks") or [])} for r in rows]


# -------------------------------------------------------------- reviews
def add_review(plan_id: str, task_key: str, branch: str, report: dict) -> int:
    with _db() as c:
        cur = c.execute("INSERT INTO reviews(plan_id, task_key, branch, verdict, created, report) VALUES (?,?,?,?,?,?)",
                        (plan_id, task_key, branch, report.get("verdict", ""), time.time(), json.dumps(report)))
        return int(cur.lastrowid or 0)


def rejections(plan_id: str, task_key: str) -> int:
    """Rejections since the task's last resolved escalation (a decision starts it afresh)."""
    with _db() as c:
        since = 0
        for r in c.execute("SELECT data FROM escalations WHERE plan_id = ? AND task_key = ? AND resolved = 1", (plan_id, task_key)):
            since = max(since, int((_json(r["data"]) or {}).get("review_id") or 0))
        row = c.execute("SELECT COUNT(*) n FROM reviews WHERE plan_id = ? AND task_key = ? AND verdict = 'reject' AND id > ?",
                        (plan_id, task_key, since)).fetchone()
    return int(row["n"])


def list_reviews(plan_id: str = "", task_key: str = "", limit: int = 50) -> list[dict]:
    sql, args = "SELECT * FROM reviews WHERE 1=1", []
    if plan_id:
        sql, args = sql + " AND plan_id = ?", args + [plan_id]
    if task_key:
        sql, args = sql + " AND task_key = ?", args + [task_key]
    with _db() as c:
        rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{"id": r["id"], "plan_id": r["plan_id"], "task_key": r["task_key"], "branch": r["branch"], "verdict": r["verdict"],
             "created": r["created"], "report": _json(r["report"])} for r in rows]


# --------------------------------------------------------- integrations
def save_integration(rec: dict) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO integrations(id, plan_id, branch, status, created, data) VALUES (?,?,?,?,?,?)",
                  (rec["id"], rec.get("plan_id", ""), rec["branch"], rec["status"], rec.get("created") or time.time(), json.dumps(rec)))


def list_integrations(limit: int = 20) -> list[dict]:
    with _db() as c:
        rows = c.execute("SELECT data FROM integrations ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [_json(r["data"]) or {} for r in rows]


# ---------------------------------------------------------- push queue
def _queue_data(integration: dict) -> dict:
    keep = ("base", "tasks", "merged", "flaky", "suite", "tests_run", "log")
    return {k: integration.get(k) for k in keep if k in integration}


def _queue_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "integration_id": r["integration_id"], "plan_id": r["plan_id"], "branch": r["branch"],
            "head_sha": r["head_sha"], "status": r["status"], "created": r["created"], "updated": r["updated"],
            "decided_by": r["decided_by"], "note": r["note"], **(_json(r["data"]) or {})}


def enqueue(integration: dict) -> dict:
    """Put a green integration branch in the push queue, awaiting approval. One live entry per branch."""
    now = time.time()
    with _db() as c:
        row = c.execute("SELECT id FROM queue WHERE branch = ? AND status = 'awaiting_approval'", (integration["branch"],)).fetchone()
        if row:
            qid = row["id"]
            c.execute("UPDATE queue SET integration_id = ?, head_sha = ?, updated = ?, data = ? WHERE id = ?",
                      (integration["id"], integration.get("head_sha", ""), now, json.dumps(_queue_data(integration)), qid))
        else:
            qid = uuid.uuid4().hex[:12]
            c.execute("INSERT INTO queue(id, integration_id, plan_id, branch, head_sha, status, created, updated, data) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (qid, integration["id"], integration.get("plan_id", ""), integration["branch"], integration.get("head_sha", ""),
                       "awaiting_approval", now, now, json.dumps(_queue_data(integration))))
    return get_queue_entry(qid) or {}


def get_queue_entry(queue_id: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM queue WHERE id = ?", (queue_id,)).fetchone()
    return _queue_row(row) if row else None


def list_queue(status: str = "", limit: int = 50) -> list[dict]:
    with _db() as c:
        if status:
            rows = c.execute("SELECT * FROM queue WHERE status = ? ORDER BY created DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM queue ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [_queue_row(r) for r in rows]


def decide(queue_id: str, status: str, by: str, note: str = "") -> dict:
    """Approve or reject a queue entry. Only an entry still awaiting approval can be decided; ValueError otherwise."""
    if status not in ("approved", "rejected"):
        raise ValueError("status must be 'approved' or 'rejected'")
    if not str(by or "").strip():
        raise ValueError("say who decides (by)")
    with _db() as c:
        row = c.execute("SELECT status FROM queue WHERE id = ?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(queue_id)
        if row["status"] != "awaiting_approval":
            raise ValueError(f"queue entry {queue_id} is already {row['status']}")
        c.execute("UPDATE queue SET status = ?, decided_by = ?, note = ?, updated = ? WHERE id = ?",
                  (status, str(by)[:80], str(note)[:500], time.time(), queue_id))
    return get_queue_entry(queue_id) or {}


# ---------------------------------------------------------- escalations
def escalate(plan_id: str, task_key: str, reason: str, data: Optional[dict] = None) -> int:
    """Record that a task needs a decision. `data` carries the blocked context (kind, question, failing check, diff excerpt)."""
    with _db() as c:
        last = c.execute("SELECT MAX(id) m FROM reviews WHERE plan_id = ? AND task_key = ?", (plan_id, task_key)).fetchone()["m"]
        data = {**(data or {}), "review_id": int(last or 0)}
        cur = c.execute("INSERT INTO escalations(plan_id, task_key, reason, created, data) VALUES (?,?,?,?,?)",
                        (plan_id, task_key, reason[:500], time.time(), json.dumps(data)))
        return int(cur.lastrowid or 0)


def list_escalations(open_only: bool = True, limit: int = 50) -> list[dict]:
    with _db() as c:
        rows = c.execute("SELECT * FROM escalations" + (" WHERE resolved = 0" if open_only else "") + " ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()
    return [{"id": r["id"], "plan_id": r["plan_id"], "task_key": r["task_key"], "reason": r["reason"], "created": r["created"],
             "resolved": bool(r["resolved"]), "data": _json(r["data"]) or {}} for r in rows]


def resolve_escalation(esc_id: int) -> None:
    with _db() as c:
        c.execute("UPDATE escalations SET resolved = 1 WHERE id = ?", (esc_id,))


def open_escalation(plan_id: str, task_key: str) -> Optional[dict]:
    with _db() as c:
        r = c.execute("SELECT * FROM escalations WHERE plan_id = ? AND task_key = ? AND resolved = 0 ORDER BY id DESC LIMIT 1",
                      (plan_id, task_key)).fetchone()
    return None if r is None else {"id": r["id"], "plan_id": r["plan_id"], "task_key": r["task_key"], "reason": r["reason"],
                                   "created": r["created"], "resolved": False, "data": _json(r["data"]) or {}}


# ----------------------------------------------------------- tool calls
def add_tool_calls(plan_id: str, task_key: str, attempt: int, calls: list, summary: str = "") -> int:
    """Every tool call a worker's agent made on a task attempt (the confined runner's audit trail), as the agent's trace has them."""
    with _db() as c:
        cur = c.execute("INSERT INTO toolcalls(plan_id, task_key, attempt, created, summary, data) VALUES (?,?,?,?,?,?)",
                        (plan_id, task_key, int(attempt), time.time(), summary[:500], json.dumps(list(calls), default=str)))
        return int(cur.lastrowid or 0)


def list_tool_calls(plan_id: str, task_key: str = "") -> list[dict]:
    sql, args = "SELECT * FROM toolcalls WHERE plan_id = ?", [plan_id]
    if task_key:
        sql, args = sql + " AND task_key = ?", args + [task_key]
    with _db() as c:
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
    return [{"id": r["id"], "task_key": r["task_key"], "attempt": r["attempt"], "created": r["created"], "summary": r["summary"],
             "calls": _json(r["data"]) or []} for r in rows]
