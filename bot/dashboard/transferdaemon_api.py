"""TransferDaemon API: ABP's page for TransferDaemon.

    GET  /api/transferdaemon/status              install, build, daemon, identity, window/TUI, relays
    POST /api/transferdaemon/setup | /update     in the background; GET /api/transferdaemon/jobs for progress
    POST /api/transferdaemon/daemon/start | /daemon/stop
    POST /api/transferdaemon/window              open the window;  POST /api/transferdaemon/tui  {headless, width, height}
    POST /api/transferdaemon/mcp                 add its MCP server (transferd-cli mcp) to ABP's external MCP servers
    GET  /api/transferdaemon/operations          every operation (daemon API, gui, tui, relay), with schemas
    POST /api/transferdaemon/call                {"operation", "args"}
    POST /api/transferdaemon/send                {"to": contact name or id, "text" | "file"}
    GET  /api/transferdaemon/audit               changes made through TransferDaemon's control hub

Reading uses the dashboard's normal auth; anything that changes something needs the desktop dashboard token (or a
linked server this machine allows: peers.remote_control), and is written to ABP's audit log.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.transferdaemon import client, harness
    from bot.transferdaemon.client import DaemonError

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def run(fn, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except DaemonError as exc:
            status = exc.status if 400 <= (exc.status or 0) < 500 else (503 if exc.code in ("unavailable", "not_installed") else 502)
            raise HTTPException(status_code=status, detail={"error": str(exc), "code": exc.code}) from exc

    def audit(action: str, detail: str) -> None:
        db.log_audit(actor="dashboard", action=f"transferdaemon_{action}", detail=detail[:500])

    @app.get("/api/transferdaemon/status", dependencies=read)
    async def td_status():
        return await run(harness.status)

    @app.post("/api/transferdaemon/setup", dependencies=write)
    async def td_setup():
        audit("setup", str(harness.install_dir()))
        return await run(harness.setup)

    @app.post("/api/transferdaemon/update", dependencies=write)
    async def td_update():
        audit("update", str(harness.install_dir()))
        return await run(harness.update)

    @app.get("/api/transferdaemon/jobs", dependencies=read)
    async def td_jobs():
        return await run(harness.jobs)

    @app.post("/api/transferdaemon/daemon/start", dependencies=write)
    async def td_start():
        return await run(harness.start_daemon)

    @app.post("/api/transferdaemon/daemon/stop", dependencies=write)
    async def td_stop():
        audit("stop", "")
        return await run(harness.stop_daemon)

    @app.post("/api/transferdaemon/window", dependencies=write)
    async def td_window():
        return await run(harness.open_window)

    @app.post("/api/transferdaemon/tui", dependencies=write)
    async def td_tui(body: dict = Body(default={})):
        return await run(harness.open_tui, bool(body.get("headless", True)), int(body.get("width") or 120),
                         int(body.get("height") or 40))

    @app.post("/api/transferdaemon/mcp", dependencies=write)
    async def td_mcp(body: dict = Body(default={})):
        audit("register_mcp", "")
        return await run(harness.register_mcp, bool(body.get("compact", True)))

    @app.get("/api/transferdaemon/operations", dependencies=read)
    async def td_operations(refresh: bool = Query(False)):
        return await run(client.operations, refresh)

    @app.post("/api/transferdaemon/call", dependencies=write)
    async def td_call(body: dict = Body(...)):
        op_id = str(body.get("operation") or "")
        if not op_id:
            raise HTTPException(status_code=400, detail="operation is required")
        args = body.get("args") if isinstance(body.get("args"), dict) else {}

        def go():
            if client.operation(op_id)["mutating"]:
                audit("call", f"{op_id} {sorted(args)}")
            return client.call(op_id, args, timeout=float(body.get("timeout_s") or 900))
        return {"result": await run(go)}

    @app.post("/api/transferdaemon/send", dependencies=write)
    async def td_send(body: dict = Body(...)):
        from bot.transferdaemon.tools import _contact_id
        to, text, path = str(body.get("to") or ""), body.get("text"), body.get("file")
        if not to or not (text or path):
            raise HTTPException(status_code=400, detail="to and text or file are required")

        def go():
            cid = _contact_id(to)
            audit("send", f"{'file' if path else 'text'} to {cid[:16]}")
            if path:
                return client.call("transfers.send_file", {"contact_id": cid, "file_path": str(path)})
            return client.call("messages.send_text", {"contact_id": cid, "text": str(text)})
        return {"result": await run(go)}

    @app.get("/api/transferdaemon/audit", dependencies=read)
    async def td_audit(limit: int = Query(100, ge=1, le=2000)):
        return await run(client.audit, limit)
