"""/api/docker/git-stacks: git-backed compose stacks and their poller (bot/git_stacks.py). Managing them needs the
desktop token; reading them (no secrets: env shows only its variable names) and deploying are also open to an
integration key with docker:read / docker:deploy."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import git_stacks as gs


async def _do(fn, *a, **kw):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except gs.StackError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 - docker_mgr.DockerError and friends
        if type(e).__name__ == "DockerError":
            raise HTTPException(status_code=400, detail=str(e)) from e
        raise


def register(app: FastAPI, require_desktop: Callable, read_auth: Callable, deploy_auth: Callable) -> None:
    P = "/api/docker/git-stacks"

    @app.get(P, dependencies=[Depends(read_auth)])
    async def gs_list():
        return {"stacks": await _do(gs.listing), "poller": gs.settings()}

    @app.post(P, dependencies=[Depends(require_desktop)])
    async def gs_add(payload: dict = Body(...)):
        return await _do(gs.add, str(payload.get("name") or ""), str(payload.get("repo") or ""),
                         str(payload.get("ref") or "main"), str(payload.get("compose_file") or "docker-compose.yml"),
                         env=payload.get("env") or {}, auto_deploy=bool(payload.get("auto_deploy")),
                         pull=bool(payload.get("pull")))

    @app.get(P + "/events", dependencies=[Depends(read_auth)])
    async def gs_events(limit: int = 100, stack: str = ""):
        return {"events": await _do(gs.events, max(1, min(limit, 1000)), stack or None)}

    @app.post(P + "/poller/run", dependencies=[Depends(require_desktop)])
    async def gs_poll():
        return {"decisions": await _do(gs.poll_once)}

    @app.get(P + "/{name}", dependencies=[Depends(read_auth)])
    async def gs_get(name: str):
        return await _do(gs.get, name)

    @app.patch(P + "/{name}", dependencies=[Depends(require_desktop)])
    async def gs_update(name: str, payload: dict = Body(...)):
        allowed = {k: payload[k] for k in ("repo", "ref", "compose_file", "auto_deploy", "pull", "env") if k in payload}
        return await _do(gs.update, name, **allowed)

    @app.delete(P + "/{name}", dependencies=[Depends(require_desktop)])
    async def gs_remove(name: str, down: bool = False):
        return await _do(gs.remove, name, down=down)

    @app.post(P + "/{name}/check", dependencies=[Depends(read_auth)])
    async def gs_check(name: str):
        return {"remote_commit": await _do(gs.remote_head, name), "stack": await _do(gs.get, name)}

    @app.post(P + "/{name}/deploy", dependencies=[Depends(deploy_auth)])
    async def gs_deploy(name: str, payload: dict = Body(default={})):
        return await _do(gs.deploy, name, pull=payload.get("pull"), commit=payload.get("commit") or None, reason="api")
