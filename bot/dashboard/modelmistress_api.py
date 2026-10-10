"""ModelMistress API: ABP's routes for the hub that replaces Ollama / Unsloth Studio for local models.

    GET  /api/modelmistress/status?peer=   version, loaded models, backends, running downloads (a peer: its model list)
    GET  /api/modelmistress/models?peer=   models that can be served (&refresh=true rescans the disk)
    GET  /api/modelmistress/jobs           downloads, running and finished
    POST /api/modelmistress/pull           {"model", "wait", "source"} -> a download job
    POST /api/modelmistress/load           {"model", "ctx_size", "gpu_layers", "parallel"}
    POST /api/modelmistress/unload         {"model"}  ("*" unloads every model)

Reading uses the dashboard's normal auth; anything that changes the hub needs the desktop dashboard token.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.modelmistress import harness
    from bot.modelmistress.client import MMError

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def run(fn, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except MMError as exc:
            code = 503 if exc.code == "unavailable" else (exc.status if exc.status and exc.status < 500 else 502)
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    def need(payload: dict, key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"{key} is required")
        return value

    @app.get("/api/modelmistress/status", dependencies=read)
    async def modelmistress_status(peer: str = ""):
        return await run(harness.status, peer)

    @app.get("/api/modelmistress/models", dependencies=read)
    async def modelmistress_models(peer: str = "", refresh: bool = False):
        return {"models": await run(harness.models, peer, refresh=refresh)}

    @app.get("/api/modelmistress/jobs", dependencies=read)
    async def modelmistress_jobs():
        return {"jobs": await run(harness.jobs)}

    @app.post("/api/modelmistress/pull", dependencies=write)
    async def modelmistress_pull(payload: dict = Body(...)):
        model = need(payload, "model")
        db.log_audit(actor="dashboard", action="modelmistress_pull", detail=model[:500])
        return await run(harness.pull, model, wait=bool(payload.get("wait")), source=payload.get("source") or None)

    @app.post("/api/modelmistress/load", dependencies=write)
    async def modelmistress_load(payload: dict = Body(...)):
        model = need(payload, "model")
        options = {k: payload[k] for k in ("ctx_size", "gpu_layers", "parallel", "threads", "embeddings", "reranking", "reload")
                   if payload.get(k) is not None}
        db.log_audit(actor="dashboard", action="modelmistress_load", detail=model[:500])
        return await run(harness.load, model, **options)

    @app.post("/api/modelmistress/unload", dependencies=write)
    async def modelmistress_unload(payload: dict = Body(...)):
        model = need(payload, "model")
        db.log_audit(actor="dashboard", action="modelmistress_unload", detail=model[:500])
        return await run(harness.unload, model)
