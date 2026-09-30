"""/api/private_project: the PrivateProject estate (catalog + live status), private_project-router (status, models, usage, conversations,
chat, runs, workspaces, missions), and SSO through private_project-auth. Desktop token only: these act as the owner."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db
from bot.modules import harness
from bot.private_project import connectors, estate, router, sso

if connectors.on_hub_started not in harness.HUB_STARTED:
    harness.HUB_STARTED.append(connectors.on_hub_started)


async def _router(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except router.RouterError as e:
        raise HTTPException(status_code=e.status if 400 <= e.status < 600 else 502, detail=str(e)) from e


def register(app: FastAPI, require_desktop: Callable) -> None:
    dep = [Depends(require_desktop)]

    # ---- estate
    @app.get("/api/private_project/estate", dependencies=dep)
    async def private_project_estate(refresh: bool = False):
        st = await estate.status(refresh)
        return {"domain": estate.domain(), "services": [{**s, "status": st.get(s["id"], {})} for s in estate.services()]}

    # ---- sso
    @app.get("/api/private_project/sso", dependencies=dep)
    async def private_project_sso():
        return await asyncio.to_thread(sso.state)

    @app.post("/api/private_project/sso/login", dependencies=dep)
    async def private_project_sso_login(payload: dict = Body(...)):
        try:
            out = await asyncio.to_thread(sso.login, str(payload.get("username") or ""), str(payload.get("password") or ""),
                                          str(payload.get("code") or ""))
        except sso.SsoError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        db.log_audit(actor="dashboard", action="private_project_sso_login", detail=str(out.get("username") or ""))
        out["connectors"] = await asyncio.to_thread(connectors.push)
        return out

    @app.post("/api/private_project/sso/logout", dependencies=dep)
    async def private_project_sso_logout():
        out = await asyncio.to_thread(sso.logout)
        out["connectors"] = await asyncio.to_thread(connectors.push)
        return out

    @app.get("/api/private_project/connectors", dependencies=dep)
    async def private_project_connectors():
        from bot.modules import client, registry

        def rows():
            out = []
            for sid, name, kind in [*((s, n, "sso") for s, n in connectors.CONNECTORS),
                                    *((s, n, "runner") for s, n, _ in connectors.RUNNERS)]:
                m = registry.get(sid)
                d = registry.install_dir(m)
                out.append({"id": sid, "name": name, "repo": m.repo, "installed": registry.is_checkout(m, d),
                            "running": client.find(m, timeout=0.5) is not None, "kind": kind})
            return out
        return {"connectors": await asyncio.to_thread(rows)}

    @app.post("/api/private_project/connectors/push", dependencies=dep)
    async def private_project_connectors_push():
        return await asyncio.to_thread(connectors.push)

    @app.get("/api/private_project/sso/verify", dependencies=dep)
    async def private_project_sso_verify():
        try:
            return await asyncio.to_thread(sso.verify)
        except sso.SsoError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    # ---- running the Router itself on this machine (bot/private_project/router_app.py)
    @app.get("/api/private_project/router-app", dependencies=dep)
    async def private_project_router_app():
        from bot.private_project import router_app
        return await asyncio.to_thread(router_app.status)

    @app.post("/api/private_project/router-app/{action}", dependencies=dep)
    async def private_project_router_app_action(action: str):
        from bot.private_project import router_app
        fns = {"install": lambda: router_app.install(False), "update": lambda: router_app.install(True),
               "start": router_app.start, "stop": router_app.stop}
        if action not in fns:
            raise HTTPException(status_code=404, detail=f"no action {action!r}")
        try:
            out = await asyncio.to_thread(fns[action])
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        db.log_audit(actor="dashboard", action=f"private_project_router_app_{action}", detail=str(out.get("dir")))
        return out

    # ---- router
    @app.get("/api/private_project/router", dependencies=dep)
    async def private_project_router_status():
        return await asyncio.to_thread(router.status)

    @app.put("/api/private_project/router/token", dependencies=dep)
    async def private_project_router_token(payload: dict = Body(...)):
        try:
            await asyncio.to_thread(router.set_token, str(payload.get("token") or ""))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        db.log_audit(actor="dashboard", action="private_project_router_token", detail="set" if payload.get("token") else "cleared")
        from bot import providers
        providers._module_cache = (0.0, {})
        await asyncio.to_thread(connectors.push, "private_project-router")
        return await asyncio.to_thread(router.status)

    @app.put("/api/private_project/router/url", dependencies=dep)
    async def private_project_router_url(payload: dict = Body(...)):
        from bot import integrations
        from bot.config import config
        try:
            origin = integrations.normalize_origin(str(payload.get("url") or router.DEFAULT_URL))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        await asyncio.to_thread(config.set_value, ["private_project", "router_url"], origin, "dashboard")
        if origin not in integrations.frame_src():   # so ABP's Router pane can show the Router's own UI
            await asyncio.to_thread(config.set_value, ["integrations", "frame_src"], [*integrations.frame_src(), origin],
                                    "dashboard")
        return await asyncio.to_thread(router.status)

    reads = {"models": router.models, "usage": router.usage, "keys": router.keys, "conversations": router.conversations,
             "runs": router.runs, "workspaces": router.workspaces, "missions": router.missions,
             "settings": router.settings, "botplatform": router.botplatform_status}

    @app.get("/api/private_project/router/{what}", dependencies=dep)
    async def private_project_router_read(what: str):
        fn = reads.get(what)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"no Router view {what!r}")
        return await _router(fn)

    @app.get("/api/private_project/router/conversations/{cid}/messages", dependencies=dep)
    async def private_project_router_messages(cid: str):
        return await _router(router.messages, cid)

    @app.get("/api/private_project/router/missions/{mid}", dependencies=dep)
    async def private_project_router_mission(mid: str):
        return await _router(router.mission, mid)

    @app.post("/api/private_project/router/chat", dependencies=dep)
    async def private_project_router_chat(payload: dict = Body(...)):
        msgs = payload.get("messages")
        if not msgs and payload.get("prompt"):
            msgs = [{"role": "user", "content": str(payload["prompt"])}]
        return await _router(router.chat, msgs or [], str(payload.get("model") or "auto"), payload.get("system"),
                             payload.get("max_cost"))

    @app.post("/api/private_project/router/route-preview", dependencies=dep)
    async def private_project_router_preview(payload: dict = Body(...)):
        return await _router(router.route_preview, payload.get("messages") or [], str(payload.get("model") or "auto"))

    @app.post("/api/private_project/router/runs", dependencies=dep)
    async def private_project_router_start_run(payload: dict = Body(...)):
        return await _router(router.start_run, payload)

    @app.post("/api/private_project/router/runs/{rid}/confirm", dependencies=dep)
    async def private_project_router_confirm(rid: str, payload: dict = Body(...)):
        return await _router(router.confirm_run, rid, payload)

    @app.post("/api/private_project/router/runs/{rid}/cancel", dependencies=dep)
    async def private_project_router_cancel(rid: str):
        return await _router(router.cancel_run, rid)
