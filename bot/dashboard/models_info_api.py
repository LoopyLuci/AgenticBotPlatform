"""Model knowledge and allowance API (roadmap PM).

    GET    /api/models/info?provider=&model=     everything known about a model, where each fact came from,
                                                 and how much of its allowance is used / left
    GET    /api/models/usage?days=1              calls, tokens and rate-limit hits per model
    GET    /api/models/find?...                  catalogued models that fit a need, with current headroom
    POST   /api/models/refresh                   download the catalog again (models.dev)
    PUT    /api/models/limits                    set your own limits for a model (or a pattern like openrouter/*:free)
    DELETE /api/models/limits?key=               remove your override

Reads follow the dashboard's normal auth; changing limits needs the dashboard token itself.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel


class _LimitsBody(BaseModel):
    key: str
    rpm: Optional[int] = None
    rpd: Optional[int] = None
    tpm: Optional[int] = None
    tpd: Optional[int] = None
    concurrent: Optional[int] = None
    reset: Optional[str] = None
    scope: Optional[str] = None


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import model_catalog
    from bot.agent_runtime import model_tools, usage_limits
    from bot.config import config

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/models/info", dependencies=read)
    async def model_info(provider: str = Query(..., min_length=1), model: str = Query(..., min_length=1)):
        body = model_catalog.lookup(provider, model).to_dict()
        body["quota"] = model_tools.quota(provider, model)
        body["catalog_age_s"] = model_catalog.catalog_age_s()
        return body

    @app.get("/api/models/usage", dependencies=read)
    async def model_usage(days: int = Query(1, ge=1, le=45)):
        return {"days": days, "models": usage_limits.report(days), "timezone": str(usage_limits.tz())}

    @app.get("/api/models/find", dependencies=read)
    async def find_models(provider: str = "", query: str = "", free_only: bool = False, min_context: int = 0,
                          needs: str = "", limit: int = Query(20, ge=1, le=100)):
        wanted = tuple(n for n in needs.split(",") if n in ("tools", "reasoning", "vision", "structured"))
        rows = model_catalog.search(provider=provider, free_only=free_only, min_context=min_context, needs=wanted, query=query, limit=limit)
        for r in rows:
            r["headroom"] = usage_limits.headroom(r["provider"], r["model"])
        return {"models": rows}

    @app.post("/api/models/refresh", dependencies=write)
    async def refresh_catalog():
        return {"source": await model_catalog.refresh(force=True), "age_s": model_catalog.catalog_age_s()}

    @app.put("/api/models/limits", dependencies=write)
    async def set_limits(body: _LimitsBody):
        key = body.key.strip()
        if not key or len(key) > 200:
            raise HTTPException(status_code=400, detail="key must be provider/model or a pattern such as openrouter/*:free")
        values: dict[str, Any] = {k: v for k, v in body.model_dump().items() if k != "key" and v is not None}
        if body.reset is not None and body.reset not in ("rolling", "utc_midnight", "pacific_midnight"):
            raise HTTPException(status_code=400, detail="reset must be rolling, utc_midnight or pacific_midnight")
        if any(isinstance(v, int) and v < 0 for v in values.values()):
            raise HTTPException(status_code=400, detail="limits cannot be negative")
        if not values:
            raise HTTPException(status_code=400, detail="give at least one limit")
        config.set_value(["native_agent", "models", "limits", key], values, actor="dashboard")
        return {"key": key, "limits": values}

    @app.delete("/api/models/limits", dependencies=write)
    async def clear_limits(key: str = Query(..., min_length=1)):
        limits = dict((((config.current.get("native_agent") or {}).get("models") or {}).get("limits")) or {})
        if key not in limits:
            raise HTTPException(status_code=404, detail="no override with that key")
        limits.pop(key)
        config.set_value(["native_agent", "models", "limits"], limits, actor="dashboard")
        return {"removed": key}
