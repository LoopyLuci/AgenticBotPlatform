"""Dashboard routes: devices.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from bot import db


def register(app: FastAPI) -> None:
    from bot.dashboard.server import (
        _NON_DEVICE_KINDS,
        _annotate_online,
        _manager,
        _require_mobile_key_id,
        _require_token_or_api_key,
        _tokens_match,
    )

    # Live presence view — /api/devices for the initial snapshot on screen
    # load, /api/ws for deltas after that. Same _require_token_or_api_key
    # scope as chat/sessions: any paired device can see the presence list
    # (that's the point — a phone should see the tablet come online), only
    # mobile-key create/revoke (device *management*) stays desktop-only.

    @app.get("/api/devices", dependencies=[Depends(_require_token_or_api_key)])
    async def api_devices():
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        return _annotate_online([dict(d) for d in devices])

    @app.websocket("/api/ws")
    async def ws_devices(
        websocket: WebSocket,
        token: Optional[str] = None,
        x_dashboard_token: Optional[str] = Header(default=None, alias="X-Dashboard-Token"),
    ):
        # A browser's native WebSocket API can't set a custom header on the
        # handshake the way fetch() can, so the desktop Electron client's
        # auth travels as a query param here instead of X-Dashboard-Token —
        # kept for that client. OkHttp *can* set handshake headers, so the
        # Android client sends the real header instead (via the shared
        # DynamicHostInterceptor every other request already goes through)
        # rather than putting its token in a URL, where it's more likely to
        # be logged. Either is accepted; the header wins if both are present.
        supplied = x_dashboard_token or token
        expected = os.environ.get("DASHBOARD_TOKEN")
        authed = bool(expected and _tokens_match(supplied, expected))
        device_id = None if authed else db.verify_api_key(supplied or "")
        # A linked peer server's key must never reach this socket: it would
        # otherwise receive every live broadcast this dashboard emits (SSH
        # session output, job events, activity — see bot/ssh_session_monitor.py
        # and every other _broadcast_soon caller), not just the narrow
        # overview/bots/lifecycle surface bot/peers.py's own REST proxy uses.
        if device_id is not None and db.api_key_kind(supplied or "") in _NON_DEVICE_KINDS:
            device_id = None
            authed = False
        if not authed and device_id is None:
            await websocket.close(code=4401)
            return
        await _manager.connect(websocket)
        if device_id is not None:
            # Only a real paired device (not the desktop dashboard token) can
            # be a WebRTC signaling target/source — see send_to_device above.
            await _manager.register_device(websocket, device_id)
        try:
            devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
            await websocket.send_json({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
            while True:
                raw = await websocket.receive_text()
                if device_id is None:
                    continue  # dashboard connections don't originate signals
                try:
                    message = json.loads(raw) if raw else {}
                    if message.get("type") != "webrtc_signal":
                        continue
                    to_id = message.get("to_api_key_id")
                    if not isinstance(to_id, int):
                        continue
                    await _manager.send_to_device(
                        to_id, {"type": "webrtc_signal", "from_api_key_id": device_id, "data": message.get("data")},
                    )
                except (ValueError, TypeError, AttributeError):
                    continue  # malformed signaling message — drop it, keep the socket's device_list duty alive
        except WebSocketDisconnect:
            pass
        finally:
            await _manager.disconnect(websocket)

    @app.post("/api/push/register")
    def api_push_register(payload: dict = Body(...), api_key_id: int = Depends(_require_mobile_key_id)):
        fcm_token = (payload.get("fcm_token") or "").strip()
        if not fcm_token:
            raise HTTPException(status_code=400, detail="payload must be {fcm_token: str}")
        db.upsert_push_token(api_key_id, fcm_token)
        return {"ok": True}

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception):
        return JSONResponse(status_code=500, content={"detail": str(exc)})
