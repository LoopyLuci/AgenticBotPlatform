"""Local Chat API (bot/localchat, PLAN-local-chat.md P1): chat with local models on ModelMistress, with ABP's agent.

    GET    /api/local-chat/status                     the hub (up, version, degraded), defaults, running turns
    GET    /api/local-chat/models?refresh=            the model picker's catalog: every hub model, figures labelled by source
    POST   /api/local-chat/models/benchmark           {"model"} measure its speed here (the hub's bench.run) and keep it
    GET    /api/local-chat/sessions?q=&archived=      sessions, pinned first, newest first; q searches titles and messages
    POST   /api/local-chat/sessions                   {title?, model?, mode?, autonomy?, workspace?, cloud_assist?} a new one
    GET    /api/local-chat/sessions/{sid}?after=      a session with its messages (after a message id) and whether it is answering
    PATCH  /api/local-chat/sessions/{sid}             change title, model, mode, autonomy, workspace, cloud_assist, pinned, archived
    DELETE /api/local-chat/sessions/{sid}
    POST   /api/local-chat/sessions/{sid}/send        {"text"} start a turn; the reply streams on /api/ws (chat_stream frames,
                                                      chat_id "localchat:<sid>"), approvals as local_chat_approval frames
                                                      (decide them through /api/approvals), the end as local_chat_turn
    POST   /api/local-chat/sessions/{sid}/cancel      stop the running turn

Reading uses the dashboard's normal auth; anything that starts or changes work needs the dashboard token.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    read = [Depends(read_auth)]
    write = [Depends(write_auth)]
    try:                        # a turn left 'running' by a process that is gone is marked interrupted, never complete
        from bot.localchat import store

        store.recover()
    except Exception:  # noqa: BLE001 - recovery must never stop the dashboard from starting
        pass

    async def _broadcast(frame: dict) -> None:
        from bot.dashboard.server import _manager

        await _manager.broadcast(frame)

    def _session_or_404(sid: str) -> dict:
        from bot.localchat import store

        s = store.get(sid)
        if s is None:
            raise HTTPException(status_code=404, detail="no such session")
        return s

    @app.get("/api/local-chat/status", dependencies=read)
    async def local_chat_status():
        from bot import localchat
        from bot.localchat import chat, store
        from bot.modelmistress import supervisor

        hub = await asyncio.to_thread(supervisor.status)
        return {"hub": hub, "settings": localchat.settings(), "running": [t for t in store.running_turns()
                                                                         if chat.is_running(t["session_id"])]}

    @app.get("/api/local-chat/models", dependencies=read)
    async def local_chat_models(refresh: bool = Query(False)):
        from bot.localchat import catalog

        try:
            doc = await asyncio.to_thread(catalog.get, refresh=refresh)
        except Exception as e:  # noqa: BLE001 - say why instead of a bare 500
            raise HTTPException(status_code=503, detail=f"ModelMistress did not answer: {e}") from e
        return {**doc, "default_model": catalog.default_model(doc)}

    @app.post("/api/local-chat/models/benchmark", dependencies=write)
    async def local_chat_benchmark(payload: dict = Body(...)):
        from bot.localchat import chat

        model = str(payload.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        try:
            return await chat.benchmark(model)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"benchmark failed: {e}") from e

    @app.get("/api/local-chat/sessions", dependencies=read)
    async def local_chat_sessions(q: str = Query(""), archived: bool = Query(False), limit: int = Query(100, ge=1, le=500)):
        from bot.localchat import store

        return {"sessions": await asyncio.to_thread(store.list_sessions, query=q, archived=archived, limit=limit)}

    @app.post("/api/local-chat/sessions", dependencies=write)
    async def local_chat_create(payload: Optional[dict] = Body(None)):
        from bot import localchat
        from bot.localchat import store

        p = payload or {}
        cfg = localchat.settings()
        try:
            return store.create(title=str(p.get("title") or ""), model=str(p.get("model") or ""),
                                mode=str(p.get("mode") or cfg["default_mode"]),
                                autonomy=int(p.get("autonomy", cfg["default_autonomy"])),
                                workspace=str(p.get("workspace") or ""), cloud_assist=str(p.get("cloud_assist") or cfg["cloud_assist"]))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/local-chat/sessions/{sid}", dependencies=read)
    async def local_chat_session(sid: str, after: int = Query(0, ge=0)):
        from bot.localchat import chat, store

        s = _session_or_404(sid)
        return {"session": s, "messages": store.messages(sid, after=after), "running": chat.is_running(sid),
                "chat_id": chat.chat_id(sid)}

    @app.patch("/api/local-chat/sessions/{sid}", dependencies=write)
    async def local_chat_update(sid: str, payload: dict = Body(...)):
        from bot.localchat import store

        _session_or_404(sid)
        try:
            return store.update(sid, **payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.delete("/api/local-chat/sessions/{sid}", dependencies=write)
    async def local_chat_delete(sid: str):
        from bot.localchat import chat, store

        _session_or_404(sid)
        await chat.cancel(sid)
        return {"deleted": store.delete(sid)}

    @app.post("/api/local-chat/sessions/{sid}/send", dependencies=write)
    async def local_chat_send(sid: str, payload: dict = Body(...)):
        from bot.localchat import chat

        _session_or_404(sid)
        try:
            return await chat.send(sid, str(payload.get("text") or ""), _broadcast)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except LookupError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @app.post("/api/local-chat/sessions/{sid}/cancel", dependencies=write)
    async def local_chat_cancel(sid: str):
        from bot.localchat import chat

        _session_or_404(sid)
        return {"cancelled": await chat.cancel(sid)}
