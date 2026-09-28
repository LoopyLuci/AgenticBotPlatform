"""Hermes Manager API: ABP's page for Hermes Manager.

    GET  /api/hermes-manager/status              install, build, bridge, window, Hermes's health
    POST /api/hermes-manager/setup | /update     in the background; GET /api/hermes-manager/jobs for progress
    POST /api/hermes-manager/bridge/start | /bridge/stop
    POST /api/hermes-manager/window              open the Hermes Manager window
    POST /api/hermes-manager/mcp                 add its MCP server to ABP's external MCP servers
    GET  /api/hermes-manager/operations          bridge and window operations
    POST /api/hermes-manager/call                {"operation", "args"}

Reading uses the dashboard's normal auth; anything that changes something needs the desktop dashboard token, and
calls that change something are written to ABP's audit log.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.hermes_manager import client, harness
    from bot.hermes_manager.client import ManagerError

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def run(fn, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ManagerError as exc:
            status = exc.status if 400 <= (exc.status or 0) < 500 else (503 if exc.code in ("unavailable", "not_installed") else 502)
            raise HTTPException(status_code=status, detail={"error": str(exc), "code": exc.code}) from exc

    def audit(action: str, detail: str) -> None:
        db.log_audit(actor="dashboard", action=f"hermes_manager_{action}", detail=detail[:500])

    @app.get("/api/hermes-manager/status", dependencies=read)
    async def hm_status():
        return await run(harness.status)

    @app.post("/api/hermes-manager/setup", dependencies=write)
    async def hm_setup():
        audit("setup", str(harness.install_dir()))
        return await run(harness.setup)

    @app.post("/api/hermes-manager/update", dependencies=write)
    async def hm_update():
        audit("update", str(harness.install_dir()))
        return await run(harness.update)

    @app.get("/api/hermes-manager/jobs", dependencies=read)
    async def hm_jobs():
        return await run(harness.jobs)

    @app.post("/api/hermes-manager/bridge/start", dependencies=write)
    async def hm_start():
        return await run(harness.start_bridge)

    @app.post("/api/hermes-manager/bridge/stop", dependencies=write)
    async def hm_stop():
        return await run(harness.stop_bridge)

    @app.post("/api/hermes-manager/window", dependencies=write)
    async def hm_window():
        return await run(harness.open_window)

    @app.post("/api/hermes-manager/mcp", dependencies=write)
    async def hm_mcp(body: dict = Body(default={})):
        audit("register_mcp", "")
        return await run(harness.register_mcp, bool(body.get("compact", True)))

    @app.get("/api/hermes-manager/operations", dependencies=read)
    async def hm_operations(refresh: bool = Query(False)):
        return await run(client.operations, refresh)

    @app.post("/api/hermes-manager/call", dependencies=write)
    async def hm_call(body: dict = Body(...)):
        op_id = str(body.get("operation") or "")
        if not op_id:
            raise HTTPException(status_code=400, detail="operation is required")
        args = body.get("args") if isinstance(body.get("args"), dict) else {}

        def go():
            if client.operation(op_id)["mutating"]:
                audit("call", f"{op_id} {sorted(args)}")
            return client.call(op_id, args)
        return {"result": await run(go)}
