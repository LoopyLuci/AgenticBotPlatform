"""Dashboard routes: schedules.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key

    # Surfaces bot/scheduler.py's existing recurring-prompt store (already
    # backing /cron, /loop, /heartbeat in chat) over HTTP — previously only
    # reachable from inside a chat with the bot, invisible to the dashboard/
    # desktop UI and any future TUI.

    @app.get("/api/bots/{instance_id}/schedules", dependencies=[Depends(_require_token_or_api_key)])
    def api_bots_schedules_list(instance_id: int):
        from bot import scheduler

        return scheduler.list_for_chat(instance_id, chat_id=None)

    @app.post("/api/bots/{instance_id}/schedules", dependencies=[Depends(_require_token_or_api_key)])
    def api_bots_schedules_create(instance_id: int, payload: dict = Body(...)):
        from bot import scheduler

        try:
            interval_s = scheduler.parse_duration(str(payload.get("interval", "")))
            sched_id = scheduler.create(
                instance_id,
                payload.get("chat_id"),
                payload.get("kind", "cron"),
                payload.get("prompt", ""),
                interval_s,
                max_runs=payload.get("max_runs"),
                thread_id=payload.get("thread_id"),
            )
        except scheduler.ScheduleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="schedule_create", detail=f"instance {instance_id}, schedule {sched_id}")
        return {"ok": True, "id": sched_id}

    @app.post("/api/bots/{instance_id}/schedules/{sched_id}/pause", dependencies=[Depends(_require_token_or_api_key)])
    def api_bots_schedules_pause(instance_id: int, sched_id: int):
        from bot import scheduler

        scheduler.pause(sched_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/schedules/{sched_id}/resume", dependencies=[Depends(_require_token_or_api_key)])
    def api_bots_schedules_resume(instance_id: int, sched_id: int):
        from bot import scheduler

        scheduler.resume(sched_id)
        return {"ok": True}

    @app.delete("/api/bots/{instance_id}/schedules/{sched_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_bots_schedules_delete(instance_id: int, sched_id: int):
        from bot import scheduler

        scheduler.remove(sched_id)
        db.log_audit(actor="dashboard", action="schedule_delete", detail=f"instance {instance_id}, schedule {sched_id}")
        return {"ok": True}
