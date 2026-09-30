"""Modules API: every module ABP knows (VM-Harness, Hermes-Manager, TransferDaemon, ModelMistress, Continuum,
TridentDroid, BrainBuilder, Wrightspace, and any added in config), on one page.

    GET  /api/modules                           every module in a line, and manifest problems
    GET  /api/modules/{id}?fetch=1              one module: install, commit and updates, hub, host, toolchain, jobs
    POST /api/modules/{id}/setup | /update | /build | /pipeline      background jobs
    GET  /api/modules/{id}/jobs,  GET /api/modules/jobs/{job_id}     their progress and logs
    POST /api/modules/{id}/hub/start | /hub/stop
    POST /api/modules/{id}/gui | /tui           open its window, or its terminal UI in a console
    POST /api/modules/{id}/mcp                  add its MCP server to ABP's external MCP servers
    GET  /api/modules/{id}/operations           what its hub can do (with schemas)
    POST /api/modules/{id}/call                 {"operation", "args", "timeout_s"}
    POST /api/modules/{id}/conformance          check it against the module contract (starts its hub if needed)
    POST /api/modules/adopt                     {"path", "id"?, "name"?, "dry_run"?, "force"?}: make a project a module
    GET  /api/modules/candidates?folder=...     project folders there, and which are modules already
    POST /api/modules/{id}/publish              {"push"?}: its own private GitHub repo (a job)
    POST /api/modules/{id}/forget               stop listing an adopted project (its files stay)

Reading needs the dashboard's normal auth; changes need the desktop dashboard token, or a linked server this machine
allows (peers.remote_control: [modules]). Changes are written to ABP's audit log.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.modules import harness, registry
    from bot.modules.client import ModuleError

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def run(fn, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ModuleError as exc:
            status = exc.status if 400 <= (exc.status or 0) < 500 else (
                503 if exc.code in ("unavailable", "not_installed") else 502)
            raise HTTPException(status_code=status, detail={"error": str(exc), "code": exc.code}) from exc

    def audit(mid: str, action: str, detail: str = "") -> None:
        db.log_audit(actor="dashboard", action=f"module_{action}", detail=f"{mid} {detail}"[:500])

    @app.get("/api/modules", dependencies=read)
    async def modules_list():
        rows = await run(harness.overview)
        return {"modules": rows, "manifest_errors": registry.manifest_errors()}

    @app.post("/api/modules/adopt", dependencies=write)
    async def modules_adopt(body: dict = Body(...)):
        from bot.modules import adoption
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        audit(str(body.get("id") or path), "adopt", path)
        return await run(adoption.adopt, path, mid=str(body.get("id") or ""), name=str(body.get("name") or ""),
                         dry_run=bool(body.get("dry_run")), force=bool(body.get("force")))

    @app.get("/api/modules/candidates", dependencies=read)
    async def modules_candidates(folder: str = Query(...)):
        from bot.modules import adoption
        return {"folder": folder, "projects": await run(adoption.candidates, folder)}

    @app.post("/api/modules/{mid}/publish", dependencies=write)
    async def module_publish(mid: str, body: dict = Body(default={})):
        from bot.modules import adoption
        audit(mid, "publish", "push" if body.get("push") else "")
        return await run(adoption.publish, mid, push=bool(body.get("push")))

    @app.post("/api/modules/{mid}/forget", dependencies=write)
    async def module_forget(mid: str):
        from bot.modules import adoption
        audit(mid, "forget")
        return await run(adoption.unregister, mid)

    @app.get("/api/modules/jobs/{job_id}", dependencies=read)
    async def modules_job(job_id: str):
        return await run(harness.job, job_id)

    @app.get("/api/modules/{mid}", dependencies=read)
    async def module_status(mid: str, fetch: bool = Query(False)):
        return await run(harness.status, mid, fetch=fetch)

    def job_route(action: str, fn):
        async def route(mid: str):
            audit(mid, action)
            return await run(fn, mid)
        return route

    for action, fn in (("setup", harness.setup), ("update", harness.update), ("build", harness.build),
                       ("pipeline", harness.run_pipeline), ("hub/start", harness.start_hub),
                       ("hub/stop", harness.stop_hub), ("gui", harness.open_gui), ("tui", harness.open_tui),
                       ("mcp", harness.register_mcp)):
        app.post(f"/api/modules/{{mid}}/{action}", dependencies=write, name=f"module_{action.replace('/', '_')}")(
            job_route(action.replace("/", "_"), fn))

    @app.post("/api/modules/{mid}/conformance", dependencies=write)
    async def module_conformance(mid: str):
        from bot.modules import conformance
        return await run(conformance.check, mid)

    @app.get("/api/modules/{mid}/jobs", dependencies=read)
    async def module_jobs(mid: str):
        return await run(harness.jobs, mid)

    @app.get("/api/modules/{mid}/operations", dependencies=read)
    async def module_operations(mid: str, refresh: bool = Query(False)):
        return await run(harness.operations, mid, refresh)

    @app.post("/api/modules/{mid}/call", dependencies=write)
    async def module_call(mid: str, body: dict = Body(...)):
        op_id = str(body.get("operation") or "")
        if not op_id:
            raise HTTPException(status_code=400, detail="operation is required")
        args = body.get("args") if isinstance(body.get("args"), dict) else {}

        def go():
            if harness.operation(mid, op_id)["mutating"]:
                audit(mid, "call", f"{op_id} {sorted(args)}")
            return harness.call(mid, op_id, args, float(body.get("timeout_s") or 900))
        return {"result": await run(go)}
