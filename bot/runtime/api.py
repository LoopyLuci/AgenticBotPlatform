"""HTTP API of the unit runtime (docs/runtime.md).

    GET  /api/runtime                         generation, units, slots, health, last transaction
    GET  /api/runtime/plan?target=<branch>    the diff live -> branch, without doing anything
    GET  /api/runtime/events?since=N          committed changes after generation N (polling fallback)
    GET  /api/ui/manifest                     generation + nav entries + panels of live instances
    GET  /api/runtime/branches                the stored branches
    POST /api/runtime/branches                {name, from}      fork a branch
    POST /api/runtime/switch                  {branch}          one transaction from live to that branch
    POST /api/runtime/rollback                back to the manifest before the last commit
    POST /api/runtime/preview                 {branch}          start a shadow of the branch (end: DELETE)
    POST /api/runtime/units/{unit}/load       {instance?, version?, settings?, branch?}
    POST /api/runtime/units/{unit}/unload     {instance?, branch?}
    POST /api/runtime/units/{unit}/swap       {version, instance?, settings?, branch?}
    POST /api/runtime/instances               {unit, instance, version?, settings?, branch?}

`branch` on a unit call edits that stored branch instead of the live system. Auth follows the other dashboard
routers: reads need `read_auth`, changes need `write_auth`.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from bot.runtime.engine import Report, RuntimeEngine


def build_router(engine: RuntimeEngine, read_auth: Callable, write_auth: Callable) -> APIRouter:
    router = APIRouter()
    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    def outcome(report: Report) -> dict[str, Any]:
        if not report.ok:
            raise HTTPException(status_code=409, detail=report.to_dict())
        return report.to_dict()

    def missing(exc: KeyError) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc.args[0]) if exc.args else "not found")

    @router.get("/api/runtime", dependencies=read)
    async def runtime_status():
        return engine.status()

    @router.get("/api/runtime/plan", dependencies=read)
    async def runtime_plan(target: str = Query("live")):
        manifest = engine.get_branch(target)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"no branch {target!r}")
        return {"target": target, **engine.plan(manifest).to_dict()}

    @router.get("/api/runtime/events", dependencies=read)
    async def runtime_events(since: int = Query(0)):
        return {"generation": engine.generation, "events": engine.events_since(since)}

    @router.get("/api/ui/manifest", dependencies=read)
    async def ui_manifest():
        return engine.ui_manifest()

    @router.get("/api/runtime/branches", dependencies=read)
    async def list_branches():
        return {"branches": engine.list_branches()}

    @router.post("/api/runtime/branches", dependencies=write)
    async def fork_branch(body: dict = Body(...)):
        try:
            manifest = engine.fork(str(body.get("name") or ""), str(body.get("from") or "live"))
        except KeyError as exc:
            raise missing(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": body["name"], "manifest_id": manifest.id}

    @router.post("/api/runtime/switch", dependencies=write)
    async def switch(body: dict = Body(...)):
        try:
            return outcome(await engine.switch(str(body.get("branch") or "")))
        except KeyError as exc:
            raise missing(exc) from exc

    @router.post("/api/runtime/rollback", dependencies=write)
    async def rollback():
        try:
            return outcome(await engine.rollback())
        except KeyError as exc:
            raise missing(exc) from exc

    @router.post("/api/runtime/preview", dependencies=write)
    async def preview(body: dict = Body(...)):
        try:
            return outcome(await engine.preview(str(body.get("branch") or "")))
        except KeyError as exc:
            raise missing(exc) from exc

    @router.delete("/api/runtime/preview/{branch}", dependencies=write)
    async def end_preview(branch: str):
        await engine.end_preview(branch)
        return {"ended": branch}

    async def unit_call(coro):
        try:
            return outcome(await coro)
        except KeyError as exc:
            raise missing(exc) from exc

    @router.post("/api/runtime/units/{unit}/load", dependencies=write)
    async def load_unit(unit: str, body: dict = Body(default={})):
        return await unit_call(engine.load(
            unit, instance=body.get("instance") or "default", version=str(body.get("version") or "1"),
            settings=body.get("settings"), branch=body.get("branch")))

    @router.post("/api/runtime/units/{unit}/unload", dependencies=write)
    async def unload_unit(unit: str, body: dict = Body(default={})):
        return await unit_call(engine.unload(unit, instance=body.get("instance") or "default",
                                             branch=body.get("branch")))

    @router.post("/api/runtime/units/{unit}/swap", dependencies=write)
    async def swap_unit(unit: str, body: dict = Body(default={})):
        version = body.get("version")
        if not version and engine.version_resolver is not None:
            version = engine.version_resolver(unit)           # a reload: the unit's current version on disk
        if not version:
            raise HTTPException(status_code=400, detail="version is required")
        return await unit_call(engine.swap(
            unit, str(version), instance=body.get("instance") or "default",
            settings=body.get("settings"), branch=body.get("branch")))

    @router.post("/api/runtime/instances", dependencies=write)
    async def add_instance(body: dict = Body(...)):
        if not body.get("unit") or not body.get("instance"):
            raise HTTPException(status_code=400, detail="unit and instance are required")
        return await unit_call(engine.load(
            str(body["unit"]), instance=str(body["instance"]), version=str(body.get("version") or "1"),
            settings=body.get("settings"), branch=body.get("branch")))

    return router
