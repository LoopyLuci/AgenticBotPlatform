"""`/api/browser/*` - the ABP <-> browser-extension bridge (bot/browser_bridge.py).

Auth tiers, on purpose:
  * hello / pairing (extension side)  : loopback only + an extension Origin; the pairing CODE (or the desktop approval)
                                        is the secret, so no token is involved.
  * WS /api/browser/ws                : loopback only + extension Origin + a paired `browser_ext` key (first message).
  * everything else                   : the strict desktop token (a phone, a linked server or the extension itself
                                        cannot manage pairings, policy or call the browser directly).
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket

from bot import browser_bridge as bb, browser_policy, db


def _to_http(exc: bb.BridgeError) -> HTTPException:
    status = {"E_AUTH": 401, "E_RATE_LIMITED": 429, "E_PARAMS": 400, "E_NOT_CONNECTED": 503, "E_TIMEOUT": 504,
              "E_NOT_ALLOWED": 403, "E_SENSITIVE_SITE": 403, "E_METHOD": 400, "E_PROTOCOL": 400}.get(exc.code, 502 if exc.code != "E_PARAMS" else 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable, "hint": exc.hint, "data": exc.data})


def register(app: FastAPI, strict_auth: Callable) -> None:
    dep = [Depends(strict_auth)]

    def _extension_caller(request: Request) -> tuple[str, str]:
        """Loopback + extension Origin, or refuse. Returns (extension_id, origin)."""
        if not bb.is_loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="the browser bridge only accepts connections from this machine")
        origin = request.headers.get("origin") or ""
        ext_id = bb.origin_ok(origin)
        if ext_id is None:
            raise HTTPException(status_code=403, detail="this endpoint is for the ABP browser extension only")
        return ext_id, origin

    # ---------------------------------------------------------------- extension-facing (no token)
    @app.get("/api/browser/hello")
    async def browser_hello(request: Request):
        if not bb.is_loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail="loopback only")
        return {"abp": True, "protocol": bb.PROTOCOL, "server_id": await asyncio.to_thread(bb.server_id),
                "pairing_open": bb.pairing.code_open()}

    @app.post("/api/browser/pair/complete")
    async def browser_pair_complete(request: Request, body: dict = Body(...)):
        ext_id, origin = _extension_caller(request)
        try:
            return await asyncio.to_thread(bb.pairing.complete_code, str(body.get("code", "")), extension_id=ext_id, origin=origin,
                                           browser=str(body.get("browser", ""))[:40], version=str(body.get("version", ""))[:20])
        except bb.BridgeError as exc:
            raise _to_http(exc)

    @app.post("/api/browser/pair/request")
    async def browser_pair_request(request: Request, body: dict = Body(default={})):
        ext_id, origin = _extension_caller(request)
        try:
            return bb.pairing.request(extension_id=ext_id, origin=origin, browser=str(body.get("browser", ""))[:40],
                                      version=str(body.get("version", ""))[:20])
        except bb.BridgeError as exc:
            raise _to_http(exc)

    @app.post("/api/browser/pair/collect")
    async def browser_pair_collect(request: Request, body: dict = Body(...)):
        _extension_caller(request)
        try:
            return await asyncio.to_thread(bb.pairing.collect, str(body.get("request_id", "")), str(body.get("nonce", "")))
        except bb.BridgeError as exc:
            raise _to_http(exc)

    @app.websocket("/api/browser/ws")
    async def browser_ws(websocket: WebSocket):
        host = websocket.client.host if websocket.client else None
        if not bb.is_loopback(host) or bb.origin_ok(websocket.headers.get("origin")) is None:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        await bb.bridge.serve(websocket, origin=websocket.headers.get("origin"))

    # ---------------------------------------------------------------- desktop-facing (strict token)
    @app.post("/api/browser/pair/code", dependencies=dep)
    async def browser_pair_code():
        return bb.pairing.start_code()

    @app.delete("/api/browser/pair/code", dependencies=dep)
    async def browser_pair_code_cancel():
        bb.pairing.cancel_code()
        return {"ok": True}

    @app.get("/api/browser/pair/pending", dependencies=dep)
    async def browser_pair_pending():
        return {"pending": bb.pairing.pending()}

    @app.post("/api/browser/pair/{request_id}/{decision}", dependencies=dep)
    async def browser_pair_decide(request_id: str, decision: str):
        if decision not in ("approve", "deny"):
            raise HTTPException(status_code=404, detail="decision must be approve or deny")
        try:
            return await asyncio.to_thread(bb.pairing.decide, request_id, decision == "approve")
        except bb.BridgeError as exc:
            raise _to_http(exc)

    @app.get("/api/browser/status", dependencies=dep)
    async def browser_status():
        return {**bb.bridge.status(), "paired": await asyncio.to_thread(bb.paired)}

    @app.delete("/api/browser/browsers/{key_id}", dependencies=dep)
    async def browser_unpair(key_id: int):
        if not await asyncio.to_thread(bb.unpair, key_id):
            raise HTTPException(status_code=404, detail="no such paired browser")
        return {"ok": True}

    @app.get("/api/browser/policy", dependencies=dep)
    async def browser_policy_get():
        return browser_policy.effective()

    @app.get("/api/browser/classify", dependencies=dep)
    async def browser_classify(url: str):
        v = browser_policy.check_url(url)
        return {"allowed": v.allowed, "sensitive": v.sensitive, "category": v.category, "reason": v.reason}

    @app.post("/api/browser/rpc", dependencies=dep)
    async def browser_rpc(body: dict = Body(...)):
        """Call any extension method (CLI, dashboard, tests). The server-side policy still applies to URL-bearing methods."""
        method = str(body.get("method", ""))
        params = body.get("params") or {}
        if not method or "." not in method:
            raise HTTPException(status_code=400, detail="method must look like tab.snapshot")
        for key in ("url",):
            if key in params and method in ("tab.navigate", "tabs.open"):
                v = browser_policy.navigation_verdict(str(params[key]))
                if not v.allowed:
                    raise _to_http(bb.BridgeError("E_SENSITIVE_SITE", v.reason))
        try:
            result = await bb.bridge.call(method, params, deadline_ms=int(body.get("deadline_ms") or bb.DEFAULT_DEADLINE_MS))
        except bb.BridgeError as exc:
            raise _to_http(exc)
        await asyncio.to_thread(db.log_audit, "dashboard", "browser_rpc", method)
        return {"result": result}
