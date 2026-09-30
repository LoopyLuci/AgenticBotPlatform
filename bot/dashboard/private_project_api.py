"""/api/private_project: ABP's link to PrivateProject (bot/private_project.py).

    GET    /api/private_project                                  the link and whether PrivateProject answers
    POST   /api/private_project/link   {base_url, device_token?, agent_type?, label?}
           PrivateProject calls this itself (with its "private_project" integration key) when its owner lets ABP use it as a
           backend, and again on every start, because its session API's port changes. The token is stored in .env and
           never returned.
    DELETE /api/private_project/link                             forget it
    GET    /api/private_project/sessions | /models | /sessions/{agent_type}/messages | /sessions/{agent_type}/permissions
    POST   /api/private_project/ask    {prompt, agent_type?}     one turn through the "private_project" backend
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db, private_project


async def _k(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except private_project.PrivateProjectError as e:
        raise HTTPException(status_code=e.status if 400 <= e.status < 600 else 502, detail=str(e)) from e


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]

    @app.get("/api/private_project", dependencies=dep)
    async def private_project_status():
        return await _k(private_project.status)

    @app.post("/api/private_project/link", dependencies=dep)
    async def private_project_link(body: dict = Body(...)):
        out = await _k(private_project.link, str(body.get("base_url") or ""), str(body.get("device_token") or ""),
                       agent_type=str(body.get("agent_type") or ""), label=str(body.get("label") or ""))
        await asyncio.to_thread(db.log_audit, "private_project", "private_project_link", str(out.get("base_url")))
        return out

    @app.delete("/api/private_project/link", dependencies=dep)
    async def private_project_unlink():
        await asyncio.to_thread(db.log_audit, "dashboard", "private_project_unlink", "")
        return await _k(private_project.unlink)

    @app.get("/api/private_project/sessions", dependencies=dep)
    async def private_project_sessions():
        return {"sessions": await _k(private_project.sessions)}

    @app.get("/api/private_project/models", dependencies=dep)
    async def private_project_models():
        return {"models": await _k(private_project.models)}

    @app.get("/api/private_project/sessions/{agent_type}/messages", dependencies=dep)
    async def private_project_messages(agent_type: str):
        return {"messages": await _k(private_project.messages, agent_type)}

    @app.get("/api/private_project/sessions/{agent_type}/permissions", dependencies=dep)
    async def private_project_permissions(agent_type: str):
        return {"permissions": await _k(private_project.permissions, agent_type)}

    @app.post("/api/private_project/ask", dependencies=dep)
    async def private_project_ask(body: dict = Body(...)):
        from bot.backends.base import BackendError
        from bot.backends.private_project_backend import PrivateProjectBackend
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        backend = PrivateProjectBackend(private_project.base_url(), private_project.token(),
                                  str(body.get("agent_type") or private_project.default_agent_type()))
        try:
            r = await backend.ask(prompt, timeout_s=float(body.get("timeout_s") or 130))
        except BackendError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"text": r.text, "tokens": r.tokens, "raw": r.raw}
