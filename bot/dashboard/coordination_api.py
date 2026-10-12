"""Coordination API: the Coordination screen's read-only routes over the work board (bot/coordination).

    GET /api/coordination/summary     which board is in use (AgentSentinel's or ABP's own), per board the task counts,
                                      who holds what, leases about to expire, idle agents, blocked tasks and why
    GET /api/coordination/tasks       tasks, newest last. Filters: board, state, owner, limit
    GET /api/coordination/locks       the locks held now. Filters: resource_prefix, owner

Nothing here changes the board; agents change it through their own work sessions.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable) -> None:
    from bot import coordination
    from bot.coordination.model import BoardError

    read = [Depends(read_auth)]

    async def _call(fn_name: str, *args, **kwargs):
        board = await coordination.aget_board()
        try:
            data = await asyncio.to_thread(lambda: getattr(board, fn_name)(*args, **kwargs))
        except BoardError as exc:
            raise HTTPException(status_code=502 if exc.code == "UNAVAILABLE" else exc.status,
                                detail=f"{exc.code}: {exc.message}") from exc
        return board, data

    @app.get("/api/coordination/summary", dependencies=read)
    async def coordination_summary(board: Optional[str] = Query(None, max_length=200)):
        used, data = await _call("summary", board)
        return {"backend": used.name, "enabled": coordination.enabled(), "summary": data}

    @app.get("/api/coordination/tasks", dependencies=read)
    async def coordination_tasks(board: Optional[str] = Query(None, max_length=200), state: Optional[str] = Query(None, max_length=20),
                                 owner: Optional[str] = Query(None, max_length=200), limit: int = Query(200, ge=1, le=1000)):
        used, data = await _call("list_tasks", board, state=state, owner=owner, limit=limit)
        return {"backend": used.name, "tasks": data}

    @app.get("/api/coordination/locks", dependencies=read)
    async def coordination_locks(resource_prefix: str = Query("", max_length=300), owner: str = Query("", max_length=200)):
        used, data = await _call("list_locks", resource_prefix=resource_prefix, owner=owner)
        return {"backend": used.name, "locks": data}
