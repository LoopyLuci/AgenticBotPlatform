"""VM-Harness API: ABP's page for VM-Harness.

    GET  /api/vm-harness/status                install, hub, window, backends
    POST /api/vm-harness/setup                 clone (if missing) + venv + dependencies, in the background
    POST /api/vm-harness/update                pull from the repo and reinstall, in the background
    GET  /api/vm-harness/jobs                  progress of setup / update
    POST /api/vm-harness/hub/start | /hub/stop
    POST /api/vm-harness/window                open the VM-Harness window
    POST /api/vm-harness/mcp                   add VM-Harness's MCP server to ABP's external MCP servers
    GET  /api/vm-harness/vms                   every VM with its state
    GET  /api/vm-harness/operations            the catalog
    POST /api/vm-harness/call                  {"operation", "args"}
    GET  /api/vm-harness/audit                 VM-Harness's own audit log (newest first)

Reading uses the dashboard's normal auth; anything that changes something needs the desktop dashboard token, and a
call to an operation that changes something is also written to ABP's audit log.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.vm_harness import client, harness
    from bot.vm_harness.client import HarnessError

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def run(fn, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except HarnessError as exc:
            status = exc.status if 400 <= (exc.status or 0) < 500 else (503 if exc.code in ("unavailable", "not_installed") else 502)
            raise HTTPException(status_code=status, detail={"error": str(exc), "code": exc.code}) from exc

    def audit(action: str, detail: str) -> None:
        db.log_audit(actor="dashboard", action=f"vm_harness_{action}", detail=detail[:500])

    @app.get("/api/vm-harness/status", dependencies=read)
    async def vmh_status(backends: bool = Query(True)):
        return await run(harness.status, backends=backends)

    @app.post("/api/vm-harness/setup", dependencies=write)
    async def vmh_setup():
        audit("setup", str(harness.install_dir()))
        return await run(harness.setup)

    @app.post("/api/vm-harness/update", dependencies=write)
    async def vmh_update():
        audit("update", str(harness.install_dir()))
        return await run(harness.update)

    @app.get("/api/vm-harness/jobs", dependencies=read)
    async def vmh_jobs():
        return await run(harness.jobs)

    @app.post("/api/vm-harness/hub/start", dependencies=write)
    async def vmh_start():
        return await run(harness.start_hub)

    @app.post("/api/vm-harness/hub/stop", dependencies=write)
    async def vmh_stop():
        return await run(harness.stop_hub)

    @app.post("/api/vm-harness/window", dependencies=write)
    async def vmh_window():
        return await run(harness.open_window)

    @app.post("/api/vm-harness/mcp", dependencies=write)
    async def vmh_mcp(body: dict = Body(default={})):
        audit("register_mcp", "")
        return await run(harness.register_mcp, bool(body.get("compact", True)))

    @app.get("/api/vm-harness/vms", dependencies=read)
    async def vmh_vms(backend: str = Query("")):
        return await run(client.call, "vm.list", {"backend": backend})

    @app.get("/api/vm-harness/operations", dependencies=read)
    async def vmh_operations(refresh: bool = Query(False)):
        return await run(client.operations, refresh)

    @app.post("/api/vm-harness/call", dependencies=write)
    async def vmh_call(body: dict = Body(...)):
        op_id = str(body.get("operation") or "")
        if not op_id:
            raise HTTPException(status_code=400, detail="operation is required")
        args = body.get("args") if isinstance(body.get("args"), dict) else {}

        def go():
            op = client.operation(op_id)
            if op["mutating"]:
                audit("call", f"{op_id} {sorted(args)}")
            return client.call(op_id, args, timeout=float(body.get("timeout_s") or 900))
        return {"result": await run(go)}

    @app.get("/api/vm-harness/audit", dependencies=read)
    async def vmh_audit(limit: int = Query(100, ge=1, le=1000)):
        return await run(client.call, "audit.query", {"limit": limit})
