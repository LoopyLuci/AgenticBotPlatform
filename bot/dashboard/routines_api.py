"""Routines API (roadmap P6): the saved, parameterised tasks a person can see and run from anywhere.

    GET    /api/routines?instance_id=              every routine, with its schedule, paused state,
                                                  last run and next run
    GET    /api/routines/{id}                      one, with its schedules and its run history
    GET    /api/routines/{id}/history?limit=       just its run history
    POST   /api/routines/{id}/run   {values}       run it now, with values for its parameters
    POST   /api/routines/{id}/pause                stop every one of its schedules
    POST   /api/routines/{id}/resume               start them again
    PUT    /api/routines/{id}/schedule             {interval, values, chat_id?, thread_id?}
    DELETE /api/routines/{id}

Reading follows the dashboard's normal auth (the desktop token or a paired device's key), so a phone
can see what is scheduled. Everything else needs the dashboard token itself: running a routine is an
agent turn with the ordinary tools, and pausing, re-timing and deleting one are an owner's decisions
about work that will happen unattended. State lives in bot/routines.py via bot/routines_view.py, which
is on bot/hotreload.py's denylist; these routes are registered once, at startup.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import routines
    from bot import routines_view

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    def _audit(action: str, detail: str) -> None:
        from bot import db

        try:
            db.log_audit(actor="dashboard", action=action, detail=detail[:500])
        except Exception:  # noqa: BLE001 - the audit log must never be the reason a routine stops working
            pass

    @app.get("/api/routines", dependencies=read)
    async def list_routines(instance_id: Optional[int] = Query(default=None, description="only this bot instance's routines")):
        """Every routine, each with its parameters, its schedules, whether it is paused, and when it last and next runs."""
        return {"routines": routines_view.listing(instance_id)}

    @app.get("/api/routines/{routine_id}", dependencies=read)
    async def get_routine(routine_id: int, history_limit: int = Query(default=20, ge=1, le=200)):
        """One routine: its template, its parameters, its schedules and its recent runs."""
        try:
            return routines_view.describe(routine_id, history_limit=history_limit)
        except routines.RoutineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/routines/{routine_id}/history", dependencies=read)
    async def get_routine_history(routine_id: int, limit: int = Query(default=20, ge=1, le=200)):
        """The routine's runs, newest first: when, the outcome, and what came of it."""
        try:
            return {"history": routines_view.run_history(routine_id, limit)}
        except routines.RoutineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/routines/{routine_id}/run", dependencies=write)
    async def run_routine(routine_id: int, payload: dict = Body(default={})):
        """Run it now with `{"values": {"param": "..."}}`. A missing value is a 400, not a run that
        fails later; the turn itself is a background one and lands in the routine's history."""
        values = payload.get("values")
        if not isinstance(values, dict):
            values = {}
        try:
            result = await routines_view.run_now(routine_id, values, chat_id=payload.get("chat_id"),
                                                 thread_id=payload.get("thread_id"))
        except routines.RoutineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit("routine_run", f"routine {routine_id} ({result['routine']}), state {result['state']}")
        return result

    @app.post("/api/routines/{routine_id}/pause", dependencies=write)
    async def pause_routine(routine_id: int):
        """Stop every schedule of this routine. Its history stays."""
        try:
            result = routines_view.set_paused(routine_id, True)
        except routines.RoutineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _audit("routine_pause", f"routine {routine_id}, {result['changed']} schedule(s)")
        return result

    @app.post("/api/routines/{routine_id}/resume", dependencies=write)
    async def resume_routine(routine_id: int):
        """Start this routine's schedules again."""
        try:
            result = routines_view.set_paused(routine_id, False)
        except routines.RoutineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _audit("routine_resume", f"routine {routine_id}, {result['changed']} schedule(s)")
        return result

    @app.put("/api/routines/{routine_id}/schedule", dependencies=write)
    async def set_routine_schedule(routine_id: int, payload: dict = Body(...)):
        """Re-time this routine. `interval` is "30m"/"2h"/"7d" or seconds; `values` are the
        parameter values its new prompt is rendered with. Both are checked before anything is
        saved, so a bad interval or a missing value is a 400 and the routine is left as it was."""
        from bot import scheduler

        try:
            interval_s = scheduler.parse_duration(str(payload.get("interval", "")))
        except scheduler.ScheduleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        values = payload.get("values")
        if not isinstance(values, dict):
            values = {}
        try:
            return routines_view.set_schedule(routine_id, interval_s, values, chat_id=payload.get("chat_id"),
                                              thread_id=payload.get("thread_id"))
        except routines.RoutineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/routines/{routine_id}", dependencies=write)
    async def delete_routine(routine_id: int):
        """Delete the routine, its schedules and its history. Nothing is kept."""
        try:
            result = routines_view.delete(routine_id)
        except routines.RoutineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _audit("routine_delete", f"routine {routine_id} ({result['deleted']}), {result['schedules']} schedule(s)")
        return result
