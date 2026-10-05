"""Routines as things a person can look at and run from anywhere (roadmap P6).

The agent writes a routine with its `routine_save` tool and `/routine` in chat runs and schedules it
(bot/routines.py, bot/commands.py). That made a routine reachable only from inside a chat with the
bot: a page, the desktop app and the CLI had no way to see one, let alone run it. This module is
that missing view - the same shape bot/approvals_view.py gives approvals - so the dashboard's
Routines screen, the desktop app's copy of it and `abp_cli routine ...` are all just these calls
rather than three reimplementations of the same table.

Nothing here is a second, weaker execution path. Running a routine renders its template and hands
the prompt to **the same agent-loop engine** the scheduler fires every due row through
(bot/scheduler.py's `run_turn(..., action_type="scheduled")`), so a run started from a page gets the
ordinary tools, permission rules, approval gating and trace, and records itself in the routine's
history the same way a scheduled run does. The one thing a caller can add is where the result is
delivered: a chat id, or the routine's own scheduled chat, or nothing at all - a run with nowhere to
deliver to is still a real run and is still in the history.
"""
from __future__ import annotations

from typing import Any, Optional

from bot import db, routines

MAX_HISTORY = 200


def _missing(routine_id: int) -> routines.RoutineError:
    return routines.RoutineError(f"no routine with id {routine_id}")


def scheduled_chat(routine_id: int) -> Optional[str]:
    """The chat a scheduled run of this routine already delivers to, or None."""
    row = db.get_conn().execute(
        "SELECT s.chat_id FROM routine_schedules r JOIN scheduled_commands s ON s.id = r.schedule_id "
        "WHERE r.routine_id=? ORDER BY s.id LIMIT 1", (routine_id,)).fetchone()
    return row["chat_id"] if row else None


def _with_state(routine: dict) -> dict:
    """The routine row plus the three things a page has to show that the table doesn't store: whether
    it is paused, when it next runs, and its current interval."""
    scheds = routine.get("schedules") or []
    live = [s for s in scheds if s["enabled"]]
    out = dict(routine)
    out["scheduled"] = bool(scheds)
    out["paused"] = bool(scheds) and not live
    out["next_run_at"] = min((s["next_run_at"] for s in live), default=None)
    out["interval_s"] = scheds[0]["interval_s"] if scheds else None
    return out


def listing(instance_id: Optional[int] = None) -> list[dict]:
    """Every routine, across every bot instance unless one is named."""
    return [_with_state(r) for r in routines.listing(instance_id)]


def describe(routine_id: int, *, history_limit: int = 20) -> dict:
    """One routine, with its schedules and its recent runs."""
    routine = routines.get_by_id(routine_id)
    if routine is None:
        raise _missing(routine_id)
    out = _with_state(routine)
    out["schedules"] = routines.schedules(routine_id)
    out["history"] = routines.history(routine_id, max(1, min(history_limit, MAX_HISTORY)))
    return out


def run_history(routine_id: int, limit: int = 20) -> list[dict]:
    if routines.get_by_id(routine_id) is None:
        raise _missing(routine_id)
    return routines.history(routine_id, max(1, min(limit, MAX_HISTORY)))


async def run_now(routine_id: int, values: Optional[dict] = None, *, chat_id: Any = None,
                  thread_id: Any = None) -> dict:
    """Run a routine right now, with the given parameter values.

    The template is rendered *before* anything is dispatched, so a missing value is reported to the
    caller rather than discovered by a run that has already been promised. The turn itself is a
    background one - an agent turn takes minutes and an HTTP request must not hold the page for that
    long - and it writes its own outcome into the routine's history when it finishes."""
    from bot import outbox
    from bot.agent_runtime import engine as agent_engine

    routine = routines.get_by_id(routine_id)
    if routine is None:
        raise _missing(routine_id)
    prompt = routines.render(routine, values)                     # fail now, not in a minute
    target = str(chat_id) if chat_id is not None else scheduled_chat(routine_id)
    session = target or f"routine:{routine['name']}"              # a session key even with nowhere to deliver
    instance_id = routine["instance_id"]

    async def _done(outcome: str, result) -> None:
        routines.finish_run(run_id, "ok" if outcome == "ran" else outcome,
                            getattr(result, "text", None) or str(result))
        if outcome == "ran" and target is not None:
            try:
                await outbox.send_message(instance_id, target, f"⏰ {result.text}", thread_id=thread_id)
            except RuntimeError:
                pass                      # the instance isn't connected; the history still has the run

    run_id = routines.record_run(routine_id, "started", "run by hand from a page")
    state, _ = await agent_engine.run_turn(
        prompt, action_type="scheduled", user_id=0, instance_id=instance_id, chat_id=session,
        thread_id=thread_id, background=True, on_result=_done,
    )
    return {"routine": routine["name"], "state": state, "delivered_to": target, "run_id": run_id}


def set_paused(routine_id: int, paused: bool) -> dict:
    """Pause or resume every schedule of a routine. Returns how many changed, like /routine does."""
    if routines.get_by_id(routine_id) is None:
        raise _missing(routine_id)
    return {"paused": paused, "changed": routines.set_active(routine_id, not paused)}


def set_schedule(routine_id: int, interval_s: int, values: Optional[dict] = None, *, chat_id: Any = None,
                 thread_id: Any = None) -> dict:
    """Change how often a routine runs, re-rendering its prompt with the given values.

    Everything is checked before anything is written: the interval has to be one the scheduler
    accepts, and the new prompt has to render - a template that only breaks at 3 a.m. is exactly
    what this avoids. A routine with no schedule yet gets one, which needs somewhere to deliver to."""
    from bot import scheduler

    routine = routines.get_by_id(routine_id)
    if routine is None:
        raise _missing(routine_id)
    try:
        scheduler.check_interval(interval_s)                      # raises ScheduleError
    except scheduler.ScheduleError as exc:
        raise routines.RoutineError(str(exc)) from exc
    prompt = routines.render(routine, values)                     # a missing value is caught here
    existing = routines.schedules(routine_id)
    if not existing and chat_id is None:
        raise routines.RoutineError(
            f"{routine['name']} has no schedule yet, so there is no chat to deliver to - pass one")
    if not existing:
        routines.schedule(routine, chat_id, interval_s, values, thread_id=thread_id)
    else:
        conn = db.get_conn()
        with db._lock:
            for s in existing:
                conn.execute(
                    "UPDATE scheduled_commands SET interval_s=?, prompt=?, next_run_at=?, "
                    "chat_id=COALESCE(?, chat_id), thread_id=COALESCE(?, thread_id) WHERE id=?",
                    (interval_s, prompt, scheduler.next_run_at(interval_s),
                     str(chat_id) if chat_id is not None else None,
                     str(thread_id) if thread_id is not None else None, s["id"]))
            conn.commit()
    return {"routine": routine["name"], "interval_s": interval_s, "schedules": routines.schedules(routine_id)}


def delete(routine_id: int) -> dict:
    """Delete a routine, its schedules and its history. There is no undo; the page asks first."""
    routine = routines.get_by_id(routine_id)
    if routine is None:
        raise _missing(routine_id)
    schedules = routines.schedules(routine_id)
    routines.delete(routine_id)
    return {"deleted": routine["name"], "schedules": len(schedules)}
