"""The context vault over HTTP (read-only): its tiers, counters and recent items.

    GET /api/context-vault/status          enabled, per-tier items and bytes, counters, hit rate
    GET /api/context-vault/items?kind=&limit=   what is stored (no bodies)
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Depends, FastAPI, Query


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]

    def _status() -> dict:
        from bot import context_vault

        v = context_vault.get_vault()
        if v is None:
            cfg = context_vault.settings.load()
            return {"enabled": False, "items": 0, "tiers": [
                {"name": t.get("name"), "volatile": t.get("kind") == "ram", "items": 0, "bytes": 0,
                 "max_bytes": t.get("max_bytes") or 0, "path": t.get("path") or ""} for t in cfg.get("tiers") or []],
                "counters": {}, "hit_rate": None}
        return v.stats()

    @app.get("/api/context-vault/status", dependencies=dep)
    async def cv_status():
        return await asyncio.to_thread(_status)

    @app.get("/api/context-vault/items", dependencies=dep)
    async def cv_items(kind: str = Query(""), limit: int = Query(50, ge=1, le=500)):
        def go():
            from bot import context_vault

            v = context_vault.get_vault()
            return {"items": v.list_items(limit, kind or None) if v else []}
        return await asyncio.to_thread(go)
