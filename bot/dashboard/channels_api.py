"""HTTP surface for the channels, canvas and nodes added in roadmap P7.

Webhooks (no dashboard token possible - the sender is a third party; each is authenticated by its own secret):

    POST /webhooks/sms                Twilio: form-encoded, signed with X-Twilio-Signature
    POST /webhooks/bluebubbles?token= BlueBubbles: JSON, authenticated by the instance's webhook_token

Both check first and answer at once (an empty 200), and produce the agent's answer in the background, because the sender
gives up after a few seconds and an agent turn can take minutes.

Canvas (a live page the agent draws; see bot/canvas.py):

    GET  /api/canvas                          the canvases (dashboard auth)
    POST /api/canvas/{name}/link              a signed, 15-minute address to open one in a browser
    GET  /canvas/{name}/view?sig=             the viewer (frame + live reload)
    GET  /canvas/{name}/version?sig=          {"version", "src"} for the viewer's polling
    GET  /canvas/{name}?sig=                  the page itself, served in a sandbox with no network access

Nodes (paired phones the agent can ask things of; see bot/nodes.py). The first three are for a paired device's own key; the
last two for the dashboard token:

    POST /api/nodes/register   GET /api/nodes/poll?wait=25   POST /api/nodes/result
    GET  /api/nodes            PUT /api/nodes/{device_id}/consent   {"capability", "mode": "deny|device|allow"}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

logger = logging.getLogger("bot.dashboard.channels")
_tasks: set[asyncio.Task] = set()


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    def _log(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error("background channel task failed: %s", t.exception())

    task.add_done_callback(_log)


class _Register(BaseModel):
    name: str = ""
    capabilities: list[str] = []


class _Result(BaseModel):
    id: str
    ok: bool
    data: Optional[dict[str, Any]] = None
    error: str = ""


class _Consent(BaseModel):
    capability: str
    mode: str


def register(app: FastAPI, read_auth: Callable, write_auth: Callable, caller_device_id: Callable) -> None:
    from bot import canvas, nodes
    from bot.platforms import imessage_platform, sms_platform

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    # ---- webhooks -------------------------------------------------------------------------------------------------
    @app.post("/webhooks/sms")
    async def sms_webhook(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        instance, sender, why = sms_platform.check(form, str(request.url), request.headers.get("x-twilio-signature", ""))
        if instance is None:
            if why in ("bad signature",):
                raise HTTPException(status_code=403, detail="bad signature")
            return Response(content="<Response/>", media_type="application/xml")     # stray or disallowed: acknowledge, do nothing
        _background(sms_platform.deliver(instance, sender, form.get("Body", "")))
        return Response(content="<Response/>", media_type="application/xml")

    @app.post("/webhooks/bluebubbles")
    async def bluebubbles_webhook(request: Request, token: str = Query("")):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="not JSON")
        instance, got, why = imessage_platform.check(payload if isinstance(payload, dict) else {}, token)
        if instance is None:
            if why == "bad token":
                raise HTTPException(status_code=403, detail="bad token")
            return JSONResponse({"status": 200})
        _background(imessage_platform.deliver(instance, got))
        return JSONResponse({"status": 200})

    # ---- canvas ---------------------------------------------------------------------------------------------------------
    def _name_and_sig(name: str, sig: str) -> str:
        try:
            name = canvas._check_name(name)
        except canvas.CanvasError:
            raise HTTPException(status_code=404, detail="no such canvas")
        if not canvas.verify(name, sig):
            raise HTTPException(status_code=403, detail="the link has expired or is not valid; ask the dashboard for a new one")
        return name

    @app.get("/api/canvas", dependencies=read)
    async def list_canvases():
        return {"canvases": canvas.listing()}

    @app.post("/api/canvas/{name}/link", dependencies=read)
    async def canvas_link(name: str):
        try:
            name = canvas._check_name(name)
        except canvas.CanvasError:
            raise HTTPException(status_code=404, detail="no such canvas")
        if canvas.info(name) is None:
            raise HTTPException(status_code=404, detail="no such canvas")
        return {"url": f"/canvas/{name}/view?sig={canvas.sign(name)}", "expires_in_s": canvas.SIG_TTL_S}

    @app.get("/canvas/{name}/view")
    async def canvas_view(name: str, sig: str = ""):
        name = _name_and_sig(name, sig)
        try:
            page = canvas.viewer_page(name)
        except canvas.CanvasError:
            raise HTTPException(status_code=404, detail="no such canvas")
        return HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/canvas/{name}/version")
    async def canvas_version(name: str, sig: str = ""):
        name = _name_and_sig(name, sig)
        meta = canvas.info(name)
        if meta is None:
            raise HTTPException(status_code=404, detail="no such canvas")
        fresh = canvas.sign(name)
        return {"version": meta["version"], "src": f"/canvas/{name}?sig={fresh}", "sig": fresh}

    @app.get("/canvas/{name}")
    async def canvas_page(name: str, sig: str = ""):
        name = _name_and_sig(name, sig)
        try:
            body = canvas.read(name)
        except canvas.CanvasError:
            raise HTTPException(status_code=404, detail="no such canvas")
        return HTMLResponse(body, headers={"Content-Security-Policy": canvas.CSP, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                           "X-Content-Type-Options": "nosniff"})

    # ---- nodes: a paired device's own calls ---------------------------------------------------------------------------
    def _device(device_id: Optional[int]) -> int:
        if device_id is None:
            raise HTTPException(status_code=403, detail="these calls are for a paired device's own key, not the dashboard token")
        return device_id

    @app.post("/api/nodes/register", dependencies=read)
    async def node_register(body: _Register, device_id: Optional[int] = Depends(caller_device_id)):
        kept = nodes.register(_device(device_id), body.name, body.capabilities)
        return {"capabilities": kept, "known": list(nodes.CAPABILITIES)}

    @app.get("/api/nodes/poll", dependencies=read)
    async def node_poll(wait: float = Query(25.0, ge=0, le=55), device_id: Optional[int] = Depends(caller_device_id)):
        return {"commands": await nodes.poll(_device(device_id), wait)}

    @app.post("/api/nodes/result", dependencies=read)
    async def node_result(body: _Result, device_id: Optional[int] = Depends(caller_device_id)):
        if not nodes.submit_result(_device(device_id), body.id, body.ok, body.data, body.error):
            raise HTTPException(status_code=404, detail="no such waiting command for this device")
        return {"accepted": True}

    # ---- nodes: for the owner ------------------------------------------------------------------------------------------
    @app.get("/api/nodes", dependencies=read)
    async def node_list():
        return {"nodes": nodes.listing(), "capabilities": nodes.CAPABILITIES}

    @app.put("/api/nodes/{device_id}/consent", dependencies=write)
    async def node_consent(device_id: int, body: _Consent):
        if not any(n["device_id"] == device_id for n in nodes.listing()):
            raise HTTPException(status_code=404, detail="no such node")
        try:
            nodes.set_consent(device_id, body.capability, body.mode)
        except nodes.NodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"device_id": device_id, "capability": body.capability, "mode": body.mode}
