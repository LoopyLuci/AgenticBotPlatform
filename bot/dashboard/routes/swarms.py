"""Dashboard routes: swarms.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import agent_control, bot_instances, db
from bot.swarm import engine as swarm_engine
from bot.swarm import strategies as swarm_strategies


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token

    # A swarm is a named group of bot instances plus a strategy for how
    # they collaborate — see bot/swarm/strategies.py. Strictly token-gated,
    # same reasoning as /api/bots.

    def _referenced_instance_ids(strategy: str, cfg: dict) -> list[int]:
        if strategy == "custom":
            return [s.get("instance_id") for s in (cfg.get("steps") or []) if s.get("instance_id")]
        ids = list(cfg.get("members") or [])
        for key in ("synthesizer", "leader", "planner", "aggregator"):
            if cfg.get(key):
                ids.append(cfg[key])
        return ids

    def _validate_swarm(strategy: str, cfg: dict) -> None:
        if strategy not in swarm_strategies.STRATEGIES:
            raise HTTPException(status_code=400, detail=f"unknown strategy {strategy!r}")
        ids = _referenced_instance_ids(strategy, cfg)
        if not ids:
            raise HTTPException(status_code=400, detail="swarm config references no bot instances")
        for iid in ids:
            if bot_instances.get_instance(iid) is None:
                raise HTTPException(status_code=400, detail=f"bot instance {iid} referenced in config doesn't exist")

    @app.get("/api/swarms", dependencies=[Depends(_require_token)])
    def api_swarms_list():
        return [dict(r) | {"config": json.loads(r["config"]), "enabled": bool(r["enabled"])} for r in db.list_swarms()]

    # /api/swarms/runs* declared before /api/swarms/{swarm_id} — FastAPI
    # matches routes in declaration order, and {swarm_id}:int would
    # otherwise swallow the literal path segment "runs" as an int and
    # 422 on it before this route is ever reached.
    @app.get("/api/swarms/runs", dependencies=[Depends(_require_token)])
    async def api_swarm_runs_list(swarm_id: Optional[int] = None, limit: int = 50):
        return [swarm_engine.swarm_run_to_dict(r) for r in db.list_swarm_runs(swarm_id=swarm_id, limit=limit)]

    @app.get("/api/swarms/runs/{swarm_run_id}", dependencies=[Depends(_require_token)])
    async def api_swarm_run_get(swarm_run_id: str):
        row = db.get_swarm_run(swarm_run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"swarm run {swarm_run_id} not found")
        return swarm_engine.swarm_run_to_dict(row)

    @app.post("/api/swarms/runs/{swarm_run_id}/cancel", dependencies=[Depends(_require_token)])
    async def api_swarm_run_cancel(swarm_run_id: str):
        cancelled = swarm_engine.cancel_run(swarm_run_id)
        return {"ok": True, "cancelled": cancelled}

    @app.get("/api/swarms/{swarm_id}", dependencies=[Depends(_require_token)])
    def api_swarms_get(swarm_id: int):
        row = db.get_swarm(swarm_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        return dict(row) | {"config": json.loads(row["config"]), "enabled": bool(row["enabled"])}

    @app.post("/api/swarms", dependencies=[Depends(_require_token)])
    def api_swarms_create(payload: dict = Body(...)):
        name = (payload.get("name") or "").strip()
        strategy = payload.get("strategy", "")
        cfg = payload.get("config") or {}
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        _validate_swarm(strategy, cfg)
        try:
            swarm_id = db.create_swarm(name, strategy, json.dumps(cfg), enabled=bool(payload.get("enabled", True)))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(status_code=400, detail=f"a swarm named {name!r} already exists") from exc
            raise
        db.log_audit(actor="dashboard", action="swarm_create", detail=f"created {name!r} ({strategy})")
        return {"ok": True, "id": swarm_id}

    @app.put("/api/swarms/{swarm_id}", dependencies=[Depends(_require_token)])
    def api_swarms_update(swarm_id: int, payload: dict = Body(...)):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        fields: dict = {}
        if "name" in payload:
            fields["name"] = payload["name"]
        if "enabled" in payload:
            fields["enabled"] = bool(payload["enabled"])
        if "strategy" in payload or "config" in payload:
            current = db.get_swarm(swarm_id)
            strategy = payload.get("strategy", current["strategy"])
            cfg = payload.get("config", json.loads(current["config"]))
            _validate_swarm(strategy, cfg)
            fields["strategy"] = strategy
            fields["config"] = json.dumps(cfg)
        db.update_swarm(swarm_id, **fields)
        db.log_audit(actor="dashboard", action="swarm_update", detail=f"updated swarm {swarm_id}")
        return {"ok": True}

    @app.delete("/api/swarms/{swarm_id}", dependencies=[Depends(_require_token)])
    def api_swarms_delete(swarm_id: int):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        db.delete_swarm(swarm_id)
        db.log_audit(actor="dashboard", action="swarm_delete", detail=f"deleted swarm {swarm_id}")
        return {"ok": True}

    @app.post("/api/swarms/{swarm_id}/enable", dependencies=[Depends(_require_token)])
    def api_swarms_enable(swarm_id: int):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        db.update_swarm(swarm_id, enabled=True)
        return {"ok": True}

    @app.post("/api/swarms/{swarm_id}/disable", dependencies=[Depends(_require_token)])
    def api_swarms_disable(swarm_id: int):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        db.update_swarm(swarm_id, enabled=False)
        return {"ok": True}

    @app.post("/api/swarms/{swarm_id}/run", dependencies=[Depends(_require_token)])
    async def api_swarms_run(swarm_id: int, payload: dict = Body(...)):
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="payload must be {prompt: str}")
        requested_by = "dashboard"
        source_instance = payload.get("source_instance")
        if source_instance is not None:
            source = agent_control.resolve_instance(source_instance)
            if source is None:
                raise HTTPException(status_code=404, detail=f"source instance {source_instance!r} not found")
            swarm_row = db.get_swarm(swarm_id)
            if swarm_row is None:
                raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
            member_ids = _referenced_instance_ids(swarm_row["strategy"], json.loads(swarm_row["config"]))
            denied = [iid for iid in member_ids if not agent_control.can_target(source["id"], iid)]
            if denied:
                raise HTTPException(
                    status_code=403,
                    detail=f"{source['name']} is not permitted to target instance(s) {denied} under the current allowlist",
                )
            requested_by = f"agent:{source['name']}"
        try:
            swarm_run_id = swarm_engine.start_swarm_run(swarm_id, prompt, requested_by=requested_by)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "swarm_run_id": swarm_run_id}
