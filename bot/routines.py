"""Routines: a task done once, saved as a reusable, parameterised, schedulable job (roadmap P6).

The workflow is simple on purpose:

1. A person does something with the agent ("every Monday, gather last week's open pull requests and post a summary").
2. They say "save that as a routine". The agent - which has the whole conversation - writes the routine itself with
   the `routine_save` tool: a name, a description, a **prompt template** with `{{placeholders}}` for the parts that vary,
   and what each placeholder means. The tools the run used are stored alongside as a hint of how it was done.
3. `/routine run <name> key=value ...` runs it now; `/routine schedule <name> every 7d key=value ...` runs it on a
   schedule (the existing scheduler); `pause`, `resume`, `history` and `delete` do what they say.

A routine is only a stored prompt. Every run is an ordinary agent turn with the ordinary tools, permission rules,
approvals and traces; **a scheduled run of a routine has no more rights than the person who saved it would have had
in that chat**, and nothing about a routine skips an approval. Parameter values are substituted as plain text into
the template - they are the scheduling person's own words, not something fetched from the web.

Storage is two small tables created on first use (`routines`, `routine_runs`); scheduling uses `scheduled_commands`
with `kind = 'routine'` (bot/scheduler.py records each run here).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from bot import db

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
MAX_TEMPLATE = 8000
MAX_PARAMS = 20
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class RoutineError(Exception):
    pass


def _conn():
    conn = db.get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            template TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            steps TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            UNIQUE (instance_id, name)
        );
        CREATE TABLE IF NOT EXISTS routine_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_id INTEGER NOT NULL,
            schedule_id INTEGER,
            started_at REAL NOT NULL,
            outcome TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_routine_runs ON routine_runs (routine_id, started_at);
        CREATE TABLE IF NOT EXISTS routine_schedules (
            schedule_id INTEGER PRIMARY KEY,
            routine_id INTEGER NOT NULL
        );
    """)
    return conn


def placeholders(template: str) -> list[str]:
    seen: list[str] = []
    for m in _PLACEHOLDER.finditer(template):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def save(instance_id: int, name: str, template: str, *, description: str = "", params: Optional[dict] = None,
         steps: Optional[list] = None, replace: bool = False) -> int:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise RoutineError("a routine name is lowercase letters, digits, - and _ (at most 48)")
    template = (template or "").strip()
    if len(template) < 10:
        raise RoutineError("the template is empty or too short to be a task")
    if len(template) > MAX_TEMPLATE:
        raise RoutineError(f"the template is longer than {MAX_TEMPLATE} characters")
    used = placeholders(template)
    declared = {str(k): (v if isinstance(v, dict) else {"description": str(v)}) for k, v in (params or {}).items()}
    if len(declared) > MAX_PARAMS:
        raise RoutineError(f"at most {MAX_PARAMS} parameters")
    undeclared = [p for p in used if p not in declared]
    if undeclared:
        raise RoutineError("the template uses {{" + "}}, {{".join(undeclared) + "}} but they are not described in params")
    unused = [p for p in declared if p not in used]
    if unused:
        raise RoutineError("params describes " + ", ".join(unused) + " but the template never uses it")
    conn = _conn()
    existing = conn.execute("SELECT id FROM routines WHERE instance_id=? AND name=?", (instance_id, name)).fetchone()
    if existing and not replace:
        raise RoutineError(f"a routine named {name!r} already exists (pass replace to overwrite it)")
    with db._lock:
        if existing:
            conn.execute("UPDATE routines SET description=?, template=?, params=?, steps=? WHERE id=?",
                         (description[:300], template, json.dumps(declared), json.dumps(steps or [])[:20000], existing["id"]))
            conn.commit()
            return existing["id"]
        cur = conn.execute("INSERT INTO routines (instance_id, name, description, template, params, steps, created_at) VALUES (?,?,?,?,?,?,?)",
                           (instance_id, name, description[:300], template, json.dumps(declared), json.dumps(steps or [])[:20000], time.time()))
        conn.commit()
        return cur.lastrowid


def _row(row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d["params"] or "{}")
    d["steps"] = json.loads(d["steps"] or "[]")
    return d


def get(instance_id: int, name: str) -> Optional[dict]:
    row = _conn().execute("SELECT * FROM routines WHERE instance_id=? AND name=?", (instance_id, (name or "").strip().lower())).fetchone()
    return _row(row) if row else None


def get_by_id(routine_id: int) -> Optional[dict]:
    row = _conn().execute("SELECT * FROM routines WHERE id=?", (routine_id,)).fetchone()
    return _row(row) if row else None


def listing(instance_id: int) -> list[dict]:
    out = []
    conn = _conn()
    for row in conn.execute("SELECT * FROM routines WHERE instance_id=? ORDER BY name", (instance_id,)).fetchall():
        r = _row(row)
        r["schedules"] = schedules(r["id"])
        last = conn.execute("SELECT outcome, started_at FROM routine_runs WHERE routine_id=? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        r["last_run"] = dict(last) if last else None
        out.append(r)
    return out


def delete(routine_id: int) -> None:
    conn = _conn()
    for s in conn.execute("SELECT schedule_id FROM routine_schedules WHERE routine_id=?", (routine_id,)).fetchall():
        with db._lock:
            conn.execute("DELETE FROM scheduled_commands WHERE id=?", (s["schedule_id"],))
    with db._lock:
        conn.execute("DELETE FROM routine_schedules WHERE routine_id=?", (routine_id,))
        conn.execute("DELETE FROM routine_runs WHERE routine_id=?", (routine_id,))
        conn.execute("DELETE FROM routines WHERE id=?", (routine_id,))
        conn.commit()


def render(routine: dict, values: Optional[dict] = None) -> str:
    """The prompt for one run: the template with each placeholder replaced. A missing value with no default is an error."""
    values = {str(k): str(v) for k, v in (values or {}).items()}
    unknown = [k for k in values if k not in routine["params"]]
    if unknown:
        raise RoutineError(f"{routine['name']} has no parameter {', '.join(unknown)} (it has: {', '.join(routine['params']) or 'none'})")
    missing = []

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        default = routine["params"].get(key, {}).get("default")
        if default is not None:
            return str(default)
        missing.append(key)
        return m.group(0)

    text = _PLACEHOLDER.sub(sub, routine["template"])
    if missing:
        raise RoutineError(f"{routine['name']} needs a value for: {', '.join(dict.fromkeys(missing))}")
    return text


# ---- scheduling and history ---------------------------------------------------------------------------------
def schedule(routine: dict, chat_id: Any, interval_s: int, values: Optional[dict] = None, *, thread_id: Any = None,
             max_runs: Optional[int] = None) -> int:
    from bot import scheduler

    prompt = render(routine, values)                         # fail now, not at 3 a.m.
    try:
        sched_id = scheduler.create(routine["instance_id"], chat_id, "routine", prompt, interval_s, max_runs=max_runs, thread_id=thread_id)
    except scheduler.ScheduleError as exc:
        raise RoutineError(str(exc))
    with db._lock:
        conn = _conn()
        conn.execute("INSERT OR REPLACE INTO routine_schedules (schedule_id, routine_id) VALUES (?,?)", (sched_id, routine["id"]))
        conn.commit()
    return sched_id


def schedules(routine_id: int) -> list[dict]:
    rows = _conn().execute(
        "SELECT s.id, s.interval_s, s.enabled, s.next_run_at, s.last_run_at, s.run_count FROM routine_schedules r "
        "JOIN scheduled_commands s ON s.id = r.schedule_id WHERE r.routine_id=? ORDER BY s.id", (routine_id,)).fetchall()
    return [dict(r) for r in rows]


def set_active(routine_id: int, active: bool) -> int:
    """Pause or resume every schedule of a routine. Returns how many were changed."""
    n = 0
    for s in schedules(routine_id):
        db.set_scheduled_command_enabled(s["id"], active)
        n += 1
    return n


def routine_for_schedule(schedule_id: int) -> Optional[int]:
    row = _conn().execute("SELECT routine_id FROM routine_schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
    return row["routine_id"] if row else None


def record_run(routine_id: int, outcome: str, summary: str = "", schedule_id: Optional[int] = None) -> None:
    with db._lock:
        conn = _conn()
        conn.execute("INSERT INTO routine_runs (routine_id, schedule_id, started_at, outcome, summary) VALUES (?,?,?,?,?)",
                     (routine_id, schedule_id, time.time(), outcome, (summary or "")[:500]))
        conn.execute("DELETE FROM routine_runs WHERE routine_id=? AND id NOT IN (SELECT id FROM routine_runs WHERE routine_id=? ORDER BY id DESC LIMIT 200)",
                     (routine_id, routine_id))
        conn.commit()


def record_scheduled_run(schedule_id: int, outcome: str, summary: str = "") -> None:
    """Called by the scheduler after a routine's scheduled run. Never raises."""
    try:
        rid = routine_for_schedule(schedule_id)
        if rid is not None:
            record_run(rid, outcome, summary, schedule_id)
    except Exception:  # noqa: BLE001
        pass


def history(routine_id: int, limit: int = 20) -> list[dict]:
    return [dict(r) for r in _conn().execute(
        "SELECT id, schedule_id, started_at, outcome, summary FROM routine_runs WHERE routine_id=? ORDER BY id DESC LIMIT ?",
        (routine_id, limit)).fetchall()]


# ---- the agent's tool ---------------------------------------------------------------------------------------------
async def _routine_save(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    from bot.agent_runtime.errors import ToolError

    if instance_id is None:
        raise ToolError("routines belong to a bot instance; this conversation has none")
    steps = []
    try:
        from bot.agent_runtime import trace

        run = trace.active()
        if getattr(run, "run_id", None):
            summary = trace.summarize(run.run_id)
            steps = [{"tool": c["tool"], "target": c.get("target", "")} for c in summary.get("tool_calls", [])][:60]
    except Exception:  # noqa: BLE001
        steps = []
    try:
        rid = save(instance_id, str(inp.get("name") or ""), str(inp.get("template") or ""), description=str(inp.get("description") or ""),
                   params=inp.get("params") if isinstance(inp.get("params"), dict) else {}, steps=steps, replace=bool(inp.get("replace")))
    except RoutineError as exc:
        raise ToolError(str(exc))
    r = get_by_id(rid)
    return (f"Saved routine {r['name']!r} with {len(r['params'])} parameter(s). Run it with /routine run {r['name']}"
            + "".join(f" {k}=..." for k in r["params"]) + f", or schedule it with /routine schedule {r['name']} every <interval>.")


def register_all() -> None:
    from bot.agent_runtime import toolspec

    S = {"type": "string"}
    toolspec.register(
        {"name": "routine_save",
         "description": "Save the task you just did as a reusable routine. Write `template` as the instruction someone would give to do this "
                        "again, with {{name}} placeholders for whatever changes between runs (a date, a repository, a topic) and describe each "
                        "placeholder in `params` ({name: {description, default?}}). Keep it self-contained: it runs later, in a fresh "
                        "conversation, with no memory of this one. Never put secrets in it.",
         "input_schema": {"type": "object", "properties": {
             "name": S, "description": S, "template": S, "params": {"type": "object"}, "replace": {"type": "boolean"}},
             "required": ["name", "template"]}},
        toolspec.ToolSpec("routine_save", "config", origin="registered"), _routine_save)


register_all()
