"""Control-plane API for the CI/CD platform: `/api/cicd/*`.

Every route is a thin wrapper over `abp_cicd.service.Service`, the single
definition of the response shapes — the same object the CLI's local mode uses —
so the API, CLI, TUI and GUI can never disagree. Read access follows the
dashboard's normal auth (the desktop token or a paired device's key).

Registered from build_app(); if the `abp_cicd` package is missing (an older
bundle) the routes are simply not registered and the server still starts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from bot import envfile

logger = logging.getLogger(__name__)

KEEPALIVE_S = 15.0
POLL_S = 1.0


def _service():
    from abp_cicd.service import Service
    from abp_cicd.store import default_db_path, get_store

    return Service(get_store(default_db_path(envfile.PROJECT_ROOT)))


def register(app: FastAPI, read_auth: Callable) -> bool:
    try:
        import abp_cicd  # noqa: F401
    except ImportError:
        logger.warning("abp_cicd is not installed alongside bot/ — /api/cicd/* is disabled")
        return False

    dep = [Depends(read_auth)]

    def _or_404(value, what: str):
        if value is None:
            raise HTTPException(status_code=404, detail=f"no such {what}")
        return value

    @app.get("/api/cicd/summary", dependencies=dep)
    async def cicd_summary():
        return await asyncio.to_thread(lambda: _service().summary())

    @app.get("/api/cicd/runs", dependencies=dep)
    async def cicd_runs(limit: int = Query(50, ge=1, le=500), kind: Optional[str] = None):
        return await asyncio.to_thread(lambda: _service().runs(limit=limit, kind=kind))

    @app.get("/api/cicd/runs/{run_id}", dependencies=dep)
    async def cicd_run(run_id: str):
        return _or_404(await asyncio.to_thread(lambda: _service().run(run_id)), "run")

    @app.get("/api/cicd/runs/{run_id}/explain", dependencies=dep)
    async def cicd_explain(run_id: str):
        return _or_404(await asyncio.to_thread(lambda: _service().explain(run_id)), "run")

    @app.get("/api/cicd/steps/stats", dependencies=dep)
    async def cicd_step_stats(name: Optional[str] = None, run_kind: Optional[str] = None,
                              last_n: int = Query(50, ge=1, le=500)):
        return await asyncio.to_thread(lambda: _service().step_stats(name=name, run_kind=run_kind, last_n=last_n))

    @app.get("/api/cicd/decisions", dependencies=dep)
    async def cicd_decisions(limit: int = Query(100, ge=1, le=1000), run_id: Optional[str] = None):
        return await asyncio.to_thread(lambda: _service().decisions(limit=limit, run_id=run_id))

    @app.get("/api/cicd/workers", dependencies=dep)
    async def cicd_workers():
        return await asyncio.to_thread(lambda: _service().workers())

    @app.get("/api/cicd/events", dependencies=dep)
    async def cicd_events(since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
                          kind: Optional[str] = None, run_id: Optional[str] = None):
        return await asyncio.to_thread(lambda: _service().events(since=since, limit=limit, kind=kind, run_id=run_id))

    @app.get("/api/cicd/chain", dependencies=dep)
    async def cicd_chain():
        return await asyncio.to_thread(lambda: _service().chain())

    @app.get("/api/cicd/events/stream", dependencies=dep)
    async def cicd_stream(request: Request, since: int = Query(0, ge=0), follow: bool = True,
                          kind: Optional[str] = None, run_id: Optional[str] = None):
        """Server-sent events. `follow=false` sends what exists and closes (handy
        for scripts and tests); otherwise it stays open, polling the log, and
        sends a comment every 15 s so proxies keep the connection."""
        async def gen():
            cursor, idle = since, 0.0
            while True:
                batch = await asyncio.to_thread(lambda: _service().events(since=cursor, limit=200, kind=kind, run_id=run_id))
                for ev in batch["events"]:
                    yield f"id: {ev['seq']}\nevent: {ev['kind']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                cursor = batch["last_seq"]
                if not follow and not batch["events"]:
                    return
                if not follow and len(batch["events"]) < 200:
                    return
                if await request.is_disconnected():
                    return
                if not batch["events"]:
                    idle += POLL_S
                    if idle >= KEEPALIVE_S:
                        idle = 0.0
                        yield ": keepalive\n\n"
                    await asyncio.sleep(POLL_S)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return True
