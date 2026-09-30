"""`/api/browser/v1/*` - the OpenAI-compatible model gateway (bot/browser_gateway.py) and the web-session admin routes.

Auth: the desktop dashboard token, sent either as `X-Dashboard-Token` or as an OpenAI-style `Authorization: Bearer` (so any OpenAI client,
and providers.yaml's `api_key_env: DASHBOARD_TOKEN`, work unchanged). A paired browser's own key is NOT accepted here: the extension can
ask to be *used* as a model, but cannot spend the desktop's models or providers.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from bot import browser_bridge as bb, db
from bot import browser_gateway as gw

NAMESPACES = gw.RESERVED_PROVIDERS


def _gateway_auth(x_dashboard_token: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("DASHBOARD_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    supplied = x_dashboard_token or (authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else "")
    if supplied and hmac.compare_digest(supplied.encode(), expected.encode()):
        return
    if supplied:
        # an integration key with models:use (bot/integrations.py); the gate already held it to that scope's routes
        from bot import integrations
        scopes = integrations.scopes_for(supplied)
        if scopes is not None and "models:use" in scopes:
            return
    raise HTTPException(status_code=401, detail="invalid dashboard token (or an integration key without models:use)")


def _error(exc: gw.GatewayError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)


def register(app: FastAPI, strict_auth) -> None:
    gdep = [Depends(_gateway_auth)]
    dep = [Depends(strict_auth)]

    async def _chat(request: Request, body: dict, ns: Optional[str]):
        if ns:
            model = str(body.get("model") or "")
            body = {**body, "model": model if model.startswith(f"{ns}/") else f"{ns}/{model}"}
        sensitive = request.headers.get("x-abp-sensitive", "").lower() in ("1", "true", "yes") or bool((body.get("metadata") or {}).get("sensitive"))
        try:
            kind, result = await gw.gateway.chat(body, sensitive=sensitive)
        except gw.GatewayError as exc:
            return _error(exc)
        if kind == "json":
            return JSONResponse(result)
        return StreamingResponse(result, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/browser/v1/chat/completions", dependencies=gdep)
    async def v1_chat(request: Request, body: dict = Body(...)):
        return await _chat(request, body, None)

    @app.post("/api/browser/v1/{ns}/chat/completions", dependencies=gdep)
    async def v1_chat_ns(ns: str, request: Request, body: dict = Body(...)):
        if ns not in NAMESPACES:
            raise HTTPException(status_code=404, detail="unknown model namespace")
        return await _chat(request, body, ns)

    @app.get("/api/browser/v1/models", dependencies=gdep)
    async def v1_models():
        return await gw.gateway.models()

    @app.get("/api/browser/v1/{ns}/models", dependencies=gdep)
    async def v1_models_ns(ns: str):
        if ns not in NAMESPACES:
            raise HTTPException(status_code=404, detail="unknown model namespace")
        allm = await gw.gateway.models()
        pre = f"{ns}/"
        return {"object": "list", "data": [{**m, "id": m["id"][len(pre):]} for m in allm["data"] if m["id"].startswith(pre)]}

    @app.post("/api/browser/v1/embeddings", dependencies=gdep)
    async def v1_embeddings(body: dict = Body(...)):
        try:
            return JSONResponse(await gw.gateway.embeddings(body))
        except gw.GatewayError as exc:
            return _error(exc)

    def _unavailable(path: str, name: str, note: str) -> None:
        async def handler():
            return JSONResponse(gw.GatewayError(501, note, code="not_implemented").body(), status_code=501)
        handler.__name__ = name
        app.post(path, dependencies=gdep)(handler)

    _unavailable("/api/browser/v1/audio/transcriptions", "v1_transcriptions",
                 "speech-to-text has no HTTP endpoint yet - a whisper catalog model can be driven from the extension side, but this route is not wired")
    _unavailable("/api/browser/v1/audio/speech", "v1_speech", "text-to-speech is not in the in-browser model catalog yet")

    @app.get("/api/browser/models/catalog", dependencies=dep)
    async def models_catalog(task: Optional[str] = None):
        return {"models": await gw.gateway.local_catalog(task)}

    @app.post("/api/browser/models/{model_id}/load", dependencies=dep)
    async def models_load(model_id: str):
        """Loading downloads and compiles real model weights in the browser - can take minutes on a first run, hence the
        long deadline. Progress itself is only shown in the extension's own options page (Models); this just waits."""
        try:
            result = await bb.bridge.call("llm.load", {"id": model_id}, deadline_ms=15 * 60_000)
        except bb.BridgeError as exc:
            return _error(gw.from_bridge(exc))
        await __import__("asyncio").to_thread(db.log_audit, "dashboard", "model_load", model_id)
        return result

    @app.get("/api/browser/web/adapters", dependencies=dep)
    async def web_adapters():
        return {"adapters": await gw.gateway.web_adapters(), "connected": bb.bridge.connected()}

    @app.post("/api/browser/web/{adapter}/selftest", dependencies=dep)
    async def web_selftest(adapter: str):
        try:
            result = await bb.bridge.call("web.selftest", {"adapter": adapter}, deadline_ms=120_000)
        except bb.BridgeError as exc:
            return _error(gw.from_bridge(exc))
        await __import__("asyncio").to_thread(db.log_audit, "dashboard", "web_selftest", adapter)
        return result

    @app.post("/api/browser/gateway/register", dependencies=dep)
    async def gateway_register(request: Request):
        port = request.url.port or 8787
        return gw.register_providers(port)
