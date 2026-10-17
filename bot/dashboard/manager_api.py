"""Manager API: the token-cheap channel for a supervising model (bot/manager, docs/local-manager.md).

    GET  /api/manager/digest?since=&mark=&text=   the capped digest since `since` (default: the last-digest marker). mark=1 moves the
                                                  marker and counts the tokens; text=1 adds the short human form
    GET  /api/manager/escalations?limit=          everything waiting for a decision, each with the minimum context to decide
    GET  /api/manager/checkin                     the check-in schedule: enabled, queued (waiting for capacity), recent, token total
    POST /api/manager/checkin                     {"force"} run a check-in now (refused with status "queued" while local-only)

Reading uses the dashboard's normal auth; running a check-in needs the dashboard token.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/manager/digest", dependencies=read)
    async def manager_digest(since: Optional[float] = Query(None), mark: bool = Query(False), text: bool = Query(False)):
        import asyncio

        from bot.manager import digest

        doc = await asyncio.to_thread(digest.build, since, mark=mark)
        if text:
            doc = {**doc, "text": digest.to_text(doc)}
        return doc

    @app.get("/api/manager/escalations", dependencies=read)
    async def manager_escalations(limit: int = Query(50, ge=1, le=200)):
        import asyncio

        from bot.manager import escalations

        return await asyncio.to_thread(escalations.view, limit)

    @app.get("/api/manager/checkin", dependencies=read)
    async def manager_checkin_state():
        from bot.manager import checkin

        return checkin.view()

    @app.post("/api/manager/checkin", dependencies=write)
    async def manager_checkin_run(body: Optional[dict] = Body(None)):
        import asyncio

        from bot.manager import checkin

        return await asyncio.to_thread(checkin.run_once, force=bool((body or {}).get("force")))
