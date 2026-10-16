"""Capacity API: the capacity governor's state (bot/capacity, docs/capacity-governor.md).

    GET  /api/capacity/state                  the state now: cloud-led | hybrid | local-only, mode (auto | manual | config), the
                                              automatic part (why, until when), any override, the thresholds in force
    GET  /api/capacity/history?limit=         every change: who or what, when, why
    POST /api/capacity/override               {"state": "local-only|hybrid|cloud-led|auto", "reason", "ttl_s" | "expires_at"}
    POST /api/capacity/clear                  {"reason"} back to automatic
    POST /api/capacity/report                 {"source", "status", "text"} classify a failure seen elsewhere (a CLI agent's
                                              "usage limit reached" line) and feed the governor
    GET  /api/capacity/route?task=            where a task like this would go right now
    GET  /api/capacity/probe                  the recovery probe: enabled, last answer, next time, probes this hour, what it asks
    POST /api/capacity/probe                  {"force"} send a probe now (the hourly limit still holds)
    GET  /api/capacity/policy                 what a worker should do now: state, capabilities to advertise, local models
    GET  /api/capacity/events?since=          the capacity-change feed (the LF5 digest reads it)
    GET  /api/capacity/handoff                the handoff packs on disk (status, model, tests, files per task)
    POST /api/capacity/handoff/prune          {"apply"} remove old finished packs (dry run unless apply is true)
    GET  /api/capacity/handoff/{task_id}      one pack; ?prompt=1 adds the resume prompt for the model taking over

Reading uses the dashboard's normal auth; changing the state needs the dashboard token.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import capacity
    from bot.capacity import governor, handoff, routing

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    def _state() -> dict:
        view = governor.get_state()
        view["settings"] = {k: v for k, v in governor.settings().items()}
        return view

    @app.get("/api/capacity/state", dependencies=read)
    async def capacity_state():
        return _state()

    @app.get("/api/capacity/history", dependencies=read)
    async def capacity_history(limit: int = Query(50, ge=1, le=500)):
        return {"history": governor.history(limit)}

    @app.post("/api/capacity/override", dependencies=write)
    async def capacity_override(body: dict = Body(...)):
        try:
            ttl = body.get("ttl_s")
            exp = body.get("expires_at")
            return governor.set_override(str(body.get("state") or ""), actor=str(body.get("actor") or "dashboard")[:60],
                                         reason=str(body.get("reason") or ""), ttl_s=float(ttl) if ttl else None,
                                         expires_at=float(exp) if exp else None)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/capacity/clear", dependencies=write)
    async def capacity_clear(body: Optional[dict] = Body(None)):
        return governor.clear_override(actor=str((body or {}).get("actor") or "dashboard")[:60], reason=str((body or {}).get("reason") or ""))

    @app.post("/api/capacity/report", dependencies=write)
    async def capacity_report(body: dict = Body(...)):
        status = body.get("status")
        moved = capacity.observe_failure(str(body.get("source") or "external")[:80], str(body.get("model") or "")[:80],
                                         int(status) if isinstance(status, (int, float)) else None, body.get("text") or "")
        return {"changed": moved is not None, "state": governor.get_state()}

    @app.get("/api/capacity/probe", dependencies=read)
    async def capacity_probe_state():
        return governor.get_state()["probe"]

    @app.post("/api/capacity/probe", dependencies=write)
    async def capacity_probe_now(body: Optional[dict] = Body(None)):
        from bot.capacity import probe

        res = await probe.probe_once(force=bool((body or {}).get("force", True)))
        return {"ran": res["ran"], "outcomes": res["outcomes"], "probe": res["state"]["probe"], "state": res["state"]["state"]}

    @app.get("/api/capacity/policy", dependencies=read)
    async def capacity_policy():
        from bot.capacity import follow

        return follow.policy()

    @app.get("/api/capacity/events", dependencies=read)
    async def capacity_events(since: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        from bot.capacity import events

        return {"events": events.recent(since, limit), "lines": events.digest_lines(since, limit)}

    @app.get("/api/capacity/route", dependencies=read)
    async def capacity_route(task: str = Query("", max_length=2000)):
        return routing.decide(task)

    @app.get("/api/capacity/handoff", dependencies=read)
    async def capacity_handoffs(limit: int = Query(100, ge=1, le=500)):
        return {"packs": handoff.list_packs(limit)}

    @app.post("/api/capacity/handoff/prune", dependencies=write)
    async def capacity_handoff_prune(body: Optional[dict] = Body(None)):
        return handoff.prune(apply=bool((body or {}).get("apply", False)))

    @app.get("/api/capacity/handoff/{task_id}", dependencies=read)
    async def capacity_handoff(task_id: str, prompt: bool = Query(False)):
        pack = handoff.read(task_id)
        if pack is None:
            raise HTTPException(status_code=404, detail="no handoff pack for that task")
        out = {"pack": pack.to_dict()}
        if prompt:
            out["prompt"] = handoff.resume_prompt(pack)
        return out
