"""/api/kestrion: ABP's link to Kestrion (bot/kestrion.py).

    GET    /api/kestrion                                  the link and whether Kestrion answers
    POST   /api/kestrion/link   {base_url, device_token?, agent_type?, label?}
           Kestrion calls this itself (with its "kestrion" integration key) when its owner lets ABP use it as a
           backend, and again on every start, because its session API's port changes. The token is stored in .env and
           never returned.
    DELETE /api/kestrion/link                             forget it
    GET    /api/kestrion/sessions | /models | /sessions/{agent_type}/messages | /sessions/{agent_type}/permissions
    POST   /api/kestrion/ask    {prompt, agent_type?}     one turn through the "kestrion" backend
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db, kestrion


async def _k(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except kestrion.KestrionError as e:
        raise HTTPException(status_code=e.status if 400 <= e.status < 600 else 502, detail=str(e)) from e


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]

    @app.get("/api/kestrion", dependencies=dep)
    async def kestrion_status():
        return await _k(kestrion.status)

    @app.post("/api/kestrion/link", dependencies=dep)
    async def kestrion_link(body: dict = Body(...)):
        out = await _k(kestrion.link, str(body.get("base_url") or ""), str(body.get("device_token") or ""),
                       agent_type=str(body.get("agent_type") or ""), label=str(body.get("label") or ""))
        await asyncio.to_thread(db.log_audit, "kestrion", "kestrion_link", str(out.get("base_url")))
        return out

    @app.delete("/api/kestrion/link", dependencies=dep)
    async def kestrion_unlink():
        await asyncio.to_thread(db.log_audit, "dashboard", "kestrion_unlink", "")
        return await _k(kestrion.unlink)

    @app.get("/api/kestrion/sessions", dependencies=dep)
    async def kestrion_sessions():
        return {"sessions": await _k(kestrion.sessions)}

    @app.get("/api/kestrion/models", dependencies=dep)
    async def kestrion_models():
        return {"models": await _k(kestrion.models)}

    @app.get("/api/kestrion/sessions/{agent_type}/messages", dependencies=dep)
    async def kestrion_messages(agent_type: str):
        return {"messages": await _k(kestrion.messages, agent_type)}

    @app.get("/api/kestrion/sessions/{agent_type}/permissions", dependencies=dep)
    async def kestrion_permissions(agent_type: str):
        return {"permissions": await _k(kestrion.permissions, agent_type)}

    @app.post("/api/kestrion/ask", dependencies=dep)
    async def kestrion_ask(body: dict = Body(...)):
        from bot.backends.base import BackendError
        from bot.backends.kestrion_backend import KestrionBackend
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        backend = KestrionBackend(kestrion.base_url(), kestrion.token(),
                                  str(body.get("agent_type") or kestrion.default_agent_type()))
        try:
            r = await backend.ask(prompt, timeout_s=float(body.get("timeout_s") or 130))
        except BackendError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"text": r.text, "tokens": r.tokens, "raw": r.raw}
