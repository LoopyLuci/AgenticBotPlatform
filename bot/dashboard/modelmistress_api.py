"""ModelMistress API: ABP's routes for the hub that replaces Ollama / Unsloth Studio for local models.

    GET  /api/modelmistress/hub            up/down, url, version, autostart, home, last restart
    GET  /api/modelmistress/status?peer=   version, loaded models, backends, running downloads (a peer: its model list)
    GET  /api/modelmistress/models?peer=   models that can be served (&refresh=true rescans the disk)
    GET  /api/modelmistress/jobs           downloads, running and finished
    POST /api/modelmistress/pull           {"model", "wait", "source"} -> a download job
    POST /api/modelmistress/load           {"model", "ctx_size", "gpu_layers", "parallel"}
    POST /api/modelmistress/unload         {"model"}  ("*" unloads every model)
    GET  /api/modelmistress/v1/models               the hub's OpenAI routes, relayed: a client that cannot reach the hub
    POST /api/modelmistress/v1/chat/completions     itself (no control file or token where it runs, e.g. a sandboxed agent)
    POST /api/modelmistress/v1/completions          uses ABP's base URL and the desktop token as its API key; streaming
    POST /api/modelmistress/v1/embeddings           answers stream through. The hub's own token never leaves ABP.

Reading uses the dashboard's normal auth; anything that changes the hub needs the desktop dashboard token. The relay
takes the desktop token as X-Dashboard-Token or as an OpenAI-style `Authorization: Bearer` key.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException


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

    @app.get("/api/modelmistress/hub", dependencies=read)
    async def modelmistress_hub():
        from bot.modelmistress import supervisor

        return await asyncio.to_thread(supervisor.status)

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

    relay = [Depends(relay_auth)]

    @app.get("/api/modelmistress/v1/models", dependencies=relay)
    async def modelmistress_relay_models():
        return await _relay("GET", "/v1/models", None)

    @app.post("/api/modelmistress/v1/chat/completions", dependencies=relay)
    async def modelmistress_relay_chat(payload: dict = Body(...)):
        return await _relay("POST", "/v1/chat/completions", payload)

    @app.post("/api/modelmistress/v1/completions", dependencies=relay)
    async def modelmistress_relay_completions(payload: dict = Body(...)):
        return await _relay("POST", "/v1/completions", payload)

    @app.post("/api/modelmistress/v1/embeddings", dependencies=relay)
    async def modelmistress_relay_embeddings(payload: dict = Body(...)):
        return await _relay("POST", "/v1/embeddings", payload)


def relay_auth(x_dashboard_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)) -> None:
    """The desktop token, as X-Dashboard-Token or as an OpenAI client's `Authorization: Bearer` key."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    supplied = x_dashboard_token or (authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else "")
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid dashboard token")


async def _relay(method: str, path: str, body: Optional[dict]) -> Any:
    """One request to the local hub with ABP's own credentials; a streamed answer streams through unchanged."""
    import httpx
    from fastapi.responses import JSONResponse, StreamingResponse

    from bot.modelmistress import client

    hub = await asyncio.to_thread(client.find)
    if hub is None:
        raise HTTPException(status_code=503, detail="the ModelMistress hub is not running")
    url, headers = hub.url + path, client._headers(hub)
    if body and body.get("stream"):
        http = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
        try:
            upstream = await http.send(http.build_request(method, url, json=body, headers=headers), stream=True)
        except httpx.HTTPError as exc:
            await http.aclose()
            raise HTTPException(status_code=502, detail=f"the ModelMistress hub is not reachable: {exc}") from exc

        async def chunks():
            try:
                async for part in upstream.aiter_raw():
                    yield part
            finally:
                await upstream.aclose()
                await http.aclose()
        return StreamingResponse(chunks(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "text/event-stream"),
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as http:
        try:
            r = await http.request(method, url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"the ModelMistress hub is not reachable: {exc}") from exc
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = {"error": {"message": r.text[:2000]}}
    return JSONResponse(data, status_code=r.status_code)
