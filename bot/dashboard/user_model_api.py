"""The optional user model over HTTP (bot/agent_runtime/user_model.py): per-person profiles that wait for review.

    GET    /api/user-model                          the switch (native_agent.user_model.*) and who has a profile (counts only)
    GET    /api/user-model/profile?instance_id=&user_id=&status=   one person's items, with evidence
    POST   /api/user-model/items/{id}/approve | /reject   {instance_id, user_id}
    PUT    /api/user-model/items/{id}               {instance_id, user_id, text}   (the person's wording; approves it)
    DELETE /api/user-model/items/{id}?instance_id=&user_id=
    GET    /api/user-model/export?instance_id=&user_id=     everything held about that person
    DELETE /api/user-model/profile?instance_id=&user_id=    forget everything about that person in that bot

A profile is personal data, so every route needs the dashboard token itself (a paired device cannot read one).
An item is only ever found through the instance and user it belongs to.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from bot.agent_runtime import user_model


async def _t(fn, *a, **kw):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]
    P = "/api/user-model"

    @app.get(P, dependencies=dep)
    async def um_overview():
        return {"enabled": user_model.enabled(), "auto_approve": user_model.auto_approve(),
                "learn_every_turns": user_model.learn_every_turns(), "max_chars": user_model.max_chars(),
                "retention_days": user_model.retention_days(), "kinds": list(user_model.KINDS),
                "users": await _t(user_model.users)}

    @app.get(P + "/profile", dependencies=dep)
    async def um_profile(instance_id: int = Query(...), user_id: str = Query(...), status: str = Query("")):
        if status and status not in user_model.STATUSES:
            raise HTTPException(400, "status is pending, approved or rejected")
        return {"items": await _t(user_model.list_items, instance_id, user_id, status or None)}

    @app.post(P + "/items/{item_id}/approve", dependencies=dep)
    async def um_approve(item_id: int, body: dict = Body(...)):
        return await _t(user_model.decide, body.get("instance_id"), body.get("user_id"), item_id, True)

    @app.post(P + "/items/{item_id}/reject", dependencies=dep)
    async def um_reject(item_id: int, body: dict = Body(...)):
        return await _t(user_model.decide, body.get("instance_id"), body.get("user_id"), item_id, False)

    @app.put(P + "/items/{item_id}", dependencies=dep)
    async def um_edit(item_id: int, body: dict = Body(...)):
        return await _t(user_model.edit_item, body.get("instance_id"), body.get("user_id"), item_id, str(body.get("text") or ""))

    @app.delete(P + "/items/{item_id}", dependencies=dep)
    async def um_forget(item_id: int, instance_id: int = Query(...), user_id: str = Query(...)):
        return {"forgotten": await _t(user_model.forget_item, instance_id, user_id, item_id)}

    @app.get(P + "/export", dependencies=dep)
    async def um_export(instance_id: int = Query(...), user_id: str = Query(...)):
        return await _t(user_model.export, instance_id, user_id)

    @app.delete(P + "/profile", dependencies=dep)
    async def um_forget_all(instance_id: int = Query(...), user_id: str = Query(...)):
        return {"forgotten": await _t(user_model.forget_all, instance_id, user_id)}
