"""/api/integrations: integration keys (bot/integrations.py) and the framing allowlist, plus the gate that holds
every integration-key request to its key's scopes before any route sees it."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from bot import integrations


def register(app: FastAPI, require_desktop: Callable, identify_caller: Callable) -> None:

    @app.middleware("http")
    async def integration_gate(request: Request, call_next):
        token = request.headers.get("x-dashboard-token") or ""
        if token:
            scopes = await asyncio.to_thread(integrations.scopes_for, token)
            if scopes is not None and not integrations.allows(scopes, request.method, request.url.path):
                return JSONResponse({"detail": f"this integration key cannot call {request.method} {request.url.path} "
                                               f"(its scopes: {', '.join(scopes)})"}, status_code=403)
        return await call_next(request)

    @app.get("/api/integrations", dependencies=[Depends(require_desktop)])
    async def integrations_overview():
        return {"keys": await asyncio.to_thread(integrations.list_keys),
                "scopes": {s: [f"{m} {p}" for m, p in rules] for s, rules in integrations.SCOPES.items()},
                "presets": integrations.PRESETS,
                "frame_ancestors": integrations.frame_ancestors(), "frame_src": integrations.frame_src()}

    @app.post("/api/integrations/keys", dependencies=[Depends(require_desktop)])
    async def integrations_mint(payload: dict = Body(...)):
        preset = str(payload.get("preset") or "")
        if preset and preset not in integrations.PRESETS:
            raise HTTPException(status_code=422, detail=f"unknown preset {preset!r}")
        p = integrations.PRESETS.get(preset, {})
        scopes = payload.get("scopes") or p.get("scopes") or []
        label = str(payload.get("label") or p.get("label") or "integration")
        origin = str(payload.get("origin") or "")
        try:
            key_id, plaintext = await asyncio.to_thread(integrations.mint, label, scopes, origin=origin, preset=preset)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if origin and payload.get("allow_framing", True):
            await asyncio.to_thread(_add_origin, integrations.normalize_origin(origin))
        return {"id": key_id, "key": plaintext, "scopes": integrations.validate_scopes(scopes),
                "note": "Shown once. Paste it where the other program keeps ABP's token (octopus-router: Settings -> "
                        "Bot Platform). It is not the dashboard token and reaches only these scopes."}

    @app.delete("/api/integrations/keys/{key_id}", dependencies=[Depends(require_desktop)])
    async def integrations_revoke(key_id: int):
        try:
            await asyncio.to_thread(integrations.revoke, key_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.put("/api/integrations/framing", dependencies=[Depends(require_desktop)])
    async def integrations_framing(payload: dict = Body(...)):
        from bot.config import config
        changes = {}
        for key in ("frame_ancestors", "frame_src"):
            if key in payload:
                vals = payload[key] if isinstance(payload[key], list) else [payload[key]]
                try:
                    changes[("integrations", key)] = [o for o in (integrations.normalize_origin(str(v)) for v in vals) if o]
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
        if changes:
            await asyncio.to_thread(config.set_values, changes, "dashboard")
        return {"frame_ancestors": integrations.frame_ancestors(), "frame_src": integrations.frame_src()}

    @app.get("/api/integrations/whoami")
    async def integrations_whoami(caller: str = Depends(identify_caller), request: Request = None):
        token = request.headers.get("x-dashboard-token") or "" if request else ""
        scopes = await asyncio.to_thread(integrations.scopes_for, token) if caller == "integration" else None
        return {"caller": caller, "scopes": scopes}


def _add_origin(origin: str) -> None:
    """A key minted for a program with a web UI (the Router) usually wants to show ABP in a pane, and ABP to show it."""
    from bot.config import config
    changes = {}
    for key, have in (("frame_ancestors", integrations.frame_ancestors()), ("frame_src", integrations.frame_src())):
        if origin not in have:
            changes[("integrations", key)] = [*have, origin]
    if changes:
        config.set_values(changes, "dashboard")
