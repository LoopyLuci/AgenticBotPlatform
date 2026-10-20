"""Capability index API (bot/capabilities, PLAN-local-chat.md P5): search everything ABP can do.

    GET /api/capabilities?q=&kind=&limit=&offline=   entries ranked by the words of q (kind: page|action|route|module|setting|
                                                     tool|mcp|command, repeatable; offline: only what needs no internet / cloud)
    GET /api/capabilities/stats                      how many of each kind
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, FastAPI, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    read = [Depends(read_auth)]

    @app.get("/api/capabilities", dependencies=read)
    async def capabilities_search(q: str = Query(""), kind: Optional[list[str]] = Query(None), limit: int = Query(20, ge=1, le=200),
                                  offline: bool = Query(False)):
        import asyncio

        from bot import capabilities

        rows = await asyncio.to_thread(capabilities.search, q, kinds=kind or None, limit=limit, offline=offline)
        return {"query": q, "results": rows}

    @app.get("/api/capabilities/stats", dependencies=read)
    async def capabilities_stats():
        import asyncio

        from bot import capabilities

        return await asyncio.to_thread(capabilities.stats)
