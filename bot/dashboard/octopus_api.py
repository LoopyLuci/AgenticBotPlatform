"""/api/octopus: the Octopus estate (catalog + live status), octopus-router (status, models, usage, conversations,
chat, runs, workspaces, missions), and SSO through octopus-auth. Desktop token only: these act as the owner."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db
from bot.octopus import estate, router, sso


async def _router(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except router.RouterError as e:
        raise HTTPException(status_code=e.status if 400 <= e.status < 600 else 502, detail=str(e)) from e


def register(app: FastAPI, require_desktop: Callable) -> None:
    dep = [Depends(require_desktop)]

    # ---- estate
    @app.get("/api/octopus/estate", dependencies=dep)
    async def octopus_estate(refresh: bool = False):
        st = await estate.status(refresh)
        return {"domain": estate.domain(), "services": [{**s, "status": st.get(s["id"], {})} for s in estate.services()]}

    # ---- sso
    @app.get("/api/octopus/sso", dependencies=dep)
    async def octopus_sso():
        return await asyncio.to_thread(sso.state)

    @app.post("/api/octopus/sso/login", dependencies=dep)
    async def octopus_sso_login(payload: dict = Body(...)):
        try:
            out = await asyncio.to_thread(sso.login, str(payload.get("username") or ""), str(payload.get("password") or ""),
                                          str(payload.get("code") or ""))
        except sso.SsoError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        db.log_audit(actor="dashboard", action="octopus_sso_login", detail=str(out.get("username") or ""))
        return out

    @app.post("/api/octopus/sso/logout", dependencies=dep)
    async def octopus_sso_logout():
        return await asyncio.to_thread(sso.logout)

    @app.get("/api/octopus/sso/verify", dependencies=dep)
    async def octopus_sso_verify():
        try:
            return await asyncio.to_thread(sso.verify)
        except sso.SsoError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    # ---- router
    @app.get("/api/octopus/router", dependencies=dep)
    async def octopus_router_status():
        return await asyncio.to_thread(router.status)

    @app.put("/api/octopus/router/token", dependencies=dep)
    async def octopus_router_token(payload: dict = Body(...)):
        try:
            await asyncio.to_thread(router.set_token, str(payload.get("token") or ""))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        db.log_audit(actor="dashboard", action="octopus_router_token", detail="set" if payload.get("token") else "cleared")
        from bot import providers
        providers._module_cache = (0.0, {})
        return await asyncio.to_thread(router.status)

    @app.put("/api/octopus/router/url", dependencies=dep)
    async def octopus_router_url(payload: dict = Body(...)):
        from bot import integrations
        from bot.config import config
        try:
            origin = integrations.normalize_origin(str(payload.get("url") or router.DEFAULT_URL))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        await asyncio.to_thread(config.set_value, ["octopus", "router_url"], origin, "dashboard")
        if origin not in integrations.frame_src():   # so ABP's Router pane can show the Router's own UI
            await asyncio.to_thread(config.set_value, ["integrations", "frame_src"], [*integrations.frame_src(), origin],
                                    "dashboard")
        return await asyncio.to_thread(router.status)

    reads = {"models": router.models, "usage": router.usage, "keys": router.keys, "conversations": router.conversations,
             "runs": router.runs, "workspaces": router.workspaces, "missions": router.missions,
             "settings": router.settings, "botplatform": router.botplatform_status}

    @app.get("/api/octopus/router/{what}", dependencies=dep)
    async def octopus_router_read(what: str):
        fn = reads.get(what)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"no Router view {what!r}")
        return await _router(fn)

    @app.get("/api/octopus/router/conversations/{cid}/messages", dependencies=dep)
    async def octopus_router_messages(cid: str):
        return await _router(router.messages, cid)

    @app.get("/api/octopus/router/missions/{mid}", dependencies=dep)
    async def octopus_router_mission(mid: str):
        return await _router(router.mission, mid)

    @app.post("/api/octopus/router/chat", dependencies=dep)
    async def octopus_router_chat(payload: dict = Body(...)):
        msgs = payload.get("messages")
        if not msgs and payload.get("prompt"):
            msgs = [{"role": "user", "content": str(payload["prompt"])}]
        return await _router(router.chat, msgs or [], str(payload.get("model") or "auto"), payload.get("system"),
                             payload.get("max_cost"))

    @app.post("/api/octopus/router/route-preview", dependencies=dep)
    async def octopus_router_preview(payload: dict = Body(...)):
        return await _router(router.route_preview, payload.get("messages") or [], str(payload.get("model") or "auto"))

    @app.post("/api/octopus/router/runs", dependencies=dep)
    async def octopus_router_start_run(payload: dict = Body(...)):
        return await _router(router.start_run, payload)

    @app.post("/api/octopus/router/runs/{rid}/confirm", dependencies=dep)
    async def octopus_router_confirm(rid: str, payload: dict = Body(...)):
        return await _router(router.confirm_run, rid, payload)

    @app.post("/api/octopus/router/runs/{rid}/cancel", dependencies=dep)
    async def octopus_router_cancel(rid: str):
        return await _router(router.cancel_run, rid)
