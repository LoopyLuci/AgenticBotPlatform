"""Model knowledge and allowance API (roadmap PM).

    GET    /api/models/info?provider=&model=     everything known about a model, where each fact came from,
                                                 and how much of its allowance is used / left
    GET    /api/models/usage?days=1              calls, tokens and rate-limit hits per model
    GET    /api/models/usage-limits              use over 1 and 7 days plus limit windows and blocks, per model (the Usage & limits screen)
    GET    /api/models/find?...                catalogued models that fit a need, with current headroom
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

    @app.get("/api/models/usage-limits", dependencies=read)
    async def model_usage_limits():
        """What the Usage & limits screen shows: per model, the last 1 and 7 days of use plus every limited
        window (used, limit, remaining, resets_at) and when it is blocked until. Read-only."""
        import time

        now = time.time()
        by_period = {1: usage_limits.report(1, now=now), 7: usage_limits.report(7, now=now)}
        keys: dict[tuple[str, str], dict] = {}
        for days in (7, 1):
            for row in by_period[days]:
                keys.setdefault((row["provider"], row["model"]), {})
        models = []
        for provider, model in keys:
            snap = usage_limits.snapshot(provider, model, now=now)
            entry: dict[str, Any] = {"provider": provider, "model": model, "windows": snap["windows"],
                                     "blocked_until": snap["blocked_until"], "headroom": snap["headroom"]}
            for days in (1, 7):
                row = next((r for r in by_period[days] if r["provider"] == provider and r["model"] == model), None)
                entry[f"d{days}"] = ({k: row[k] for k in ("calls", "tokens", "rate_limited", "errors", "last_used")} if row else
                                     {"calls": 0, "tokens": 0, "rate_limited": 0, "errors": 0, "last_used": None})
            models.append(entry)
        models.sort(key=lambda m: (-m["d7"]["calls"], m["provider"], m["model"]))
        return {"now": now, "timezone": str(usage_limits.tz()), "models": models}

    @app.get("/api/models/find", dependencies=read)
    async def find_models(provider: str = "", query: str = "", free_only: bool = False, min_context: int = 0,
                          needs: str = "", limit: int = Query(20, ge=1, le=100)):
        wanted = tuple(n for n in needs.split(",") if n in ("tools", "reasoning", "vision", "structured"))
        rows = model_catalog.search(provider=provider, free_only=free_only, min_context=min_context, needs=wanted, query=query, limit=limit)
        for r in rows:
            r["headroom"] = usage_limits.headroom(r["provider"], r["model"])
        return {"models": rows}

    @app.get("/api/agent/sessions/{session_key:path}/export", dependencies=write)
    async def export_session(session_key: str, format: str = Query("md", pattern="^(md|json)$")):
        """A conversation as Markdown or JSON, shortened and with secrets removed. Needs the dashboard token itself
        (a paired device's key is not enough): a conversation may hold things its owner did not mean to share."""
        from fastapi.responses import PlainTextResponse

        from bot.agent_runtime import session_export

        try:
            text = session_export.export_session(session_key, format)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        media = "application/json" if format == "json" else "text/markdown; charset=utf-8"
        return PlainTextResponse(text, media_type=media)

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
