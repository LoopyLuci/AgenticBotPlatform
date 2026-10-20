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

Sessions and worktrees (S1):
    GET    /api/local-chat/groups                     the sidebar's user groups; POST {name} adds one
    PATCH  /api/local-chat/groups/{gid}               {name}; DELETE removes the group (its sessions stay, ungrouped)
    GET    /api/local-chat/events?after=&session_id=  status changes and worktree actions, oldest first (notifications)
    POST   /api/local-chat/sessions/{sid}/read        mark it read (clears unread)
    POST   /api/local-chat/sessions/{sid}/steer       {"text", "how": steer|queue} a message while it works
    GET    /api/local-chat/sessions/{sid}/queued      messages waiting for the next turn; DELETE .../queued/{qid} drops one
    GET    /api/local-chat/sessions/{sid}/checkpoints the turns that can be rewound
    POST   /api/local-chat/sessions/{sid}/rewind      {"turn_id"} files back to before that turn; its messages marked reverted
    GET    /api/local-chat/sessions/{sid}/worktree    branch, ahead / behind, dirty files, conflicts (null when none)
    POST   /api/local-chat/sessions/{sid}/worktree    {base?, name?} give the session its own worktree
    GET    /api/local-chat/sessions/{sid}/worktree/diff          the session's changes against its base
    POST   /api/local-chat/sessions/{sid}/worktree/sync          {hand_to_agent?} merge the base in; conflicts listed
    GET    /api/local-chat/sessions/{sid}/worktree/commit-message a drafted message for the owner to confirm
    POST   /api/local-chat/sessions/{sid}/worktree/commit        {message}
    POST   /api/local-chat/sessions/{sid}/worktree/keep          {keep}
    POST   /api/local-chat/sessions/{sid}/worktree/discard       {confirm?} 409 with what would be lost unless confirmed
    POST   /api/local-chat/sessions/{sid}/worktree/queue-merge   into the conductor's push queue (owner approval there)
    POST   /api/local-chat/worktrees/prune                       {clean_merged?} orphans (and merged, clean ones)
A session created with a git workspace gets its own worktree unless {"worktree": false} or local_chat.worktrees is off;
DELETE asks (409) when the session's worktree holds unmerged work, unless ?confirm=true.

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

    async def _announce(session: Optional[dict]) -> None:
        from bot.localchat import chat

        frame = chat.session_frame(session)
        if frame:
            try:
                await _broadcast(frame)
            except Exception:  # noqa: BLE001 - a socket problem never fails the request
                pass

    def _wt_errors(fn, *a, **kw):
        from bot.localchat import worktrees

        try:
            return fn(*a, **kw)
        except worktrees.NeedsConfirmation as e:
            raise HTTPException(status_code=409, detail={"message": str(e), "would_lose": e.would_lose}) from e
        except worktrees.WorktreeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

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

        from bot.localchat import worktrees

        p = payload or {}
        cfg = localchat.settings()
        try:
            s = store.create(title=str(p.get("title") or ""), model=str(p.get("model") or ""),
                             mode=str(p.get("mode") or cfg["default_mode"]),
                             autonomy=int(p.get("autonomy", cfg["default_autonomy"])),
                             workspace=str(p.get("workspace") or ""), cloud_assist=str(p.get("cloud_assist") or cfg["cloud_assist"]),
                             group_id=str(p.get("group_id") or ""), effort=str(p.get("effort") or ""))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        want = p.get("worktree")
        auto = want is None and cfg.get("worktrees") == "auto" and s["mode"] != "chat"
        if (want or auto) and s["workspace"]:
            repo = await asyncio.to_thread(worktrees.repo_of, s["workspace"])
            if repo is not None:
                try:
                    await asyncio.to_thread(worktrees.create, s["id"], base=str(p.get("base") or "") or None)
                except worktrees.WorktreeError as e:
                    if want:
                        store.delete(s["id"])
                        raise HTTPException(status_code=400, detail=str(e)) from e
                    store.add_message(s["id"], "system", f"No worktree for this session: {e}")
            elif want:
                store.delete(s["id"])
                raise HTTPException(status_code=400, detail="a worktree needs the workspace to be inside a git repository")
        return store.get(s["id"])

    @app.get("/api/local-chat/sessions/{sid}", dependencies=read)
    async def local_chat_session(sid: str, after: int = Query(0, ge=0)):
        from bot.localchat import chat, store

        s = _session_or_404(sid)
        return {"session": s, "messages": store.messages(sid, after=after), "running": chat.is_running(sid),
                "chat_id": chat.chat_id(sid)}

    @app.patch("/api/local-chat/sessions/{sid}", dependencies=write)
    async def local_chat_update(sid: str, payload: dict = Body(...)):
        from bot.localchat import store

        from bot.localchat import worktrees

        _session_or_404(sid)
        try:
            s = store.update(sid, **payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if payload.get("archived"):          # an unchanged worktree goes with the archive; a changed one is kept
            try:
                s = {**store.get(sid), "archive": await asyncio.to_thread(worktrees.on_archive, sid)}
            except worktrees.WorktreeError as e:
                s = {**s, "archive": {"worktree": "kept", "why": str(e)}}
        await _announce(store.get(sid))
        return s

    @app.delete("/api/local-chat/sessions/{sid}", dependencies=write)
    async def local_chat_delete(sid: str, confirm: bool = Query(False)):
        from bot.localchat import chat, store, worktrees

        s = _session_or_404(sid)
        if s.get("worktree"):                # asks first when work would be lost; removes the worktree with the session
            await asyncio.to_thread(_wt_errors, worktrees.discard, sid, confirm=confirm)
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

    # ---- sessions and worktrees (S1) ----------------------------------------------------------------------------------------
    @app.get("/api/local-chat/groups", dependencies=read)
    async def local_chat_groups():
        from bot.localchat import store

        return {"groups": store.list_groups()}

    @app.post("/api/local-chat/groups", dependencies=write)
    async def local_chat_group_create(payload: dict = Body(...)):
        from bot.localchat import store

        try:
            return store.create_group(str(payload.get("name") or ""))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.patch("/api/local-chat/groups/{gid}", dependencies=write)
    async def local_chat_group_rename(gid: str, payload: dict = Body(...)):
        from bot.localchat import store

        try:
            return store.rename_group(gid, str(payload.get("name") or ""))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except KeyError as e:
            raise HTTPException(status_code=404, detail="no such group") from e

    @app.delete("/api/local-chat/groups/{gid}", dependencies=write)
    async def local_chat_group_delete(gid: str):
        from bot.localchat import store

        return {"deleted": store.delete_group(gid)}

    @app.get("/api/local-chat/events", dependencies=read)
    async def local_chat_events(after: int = Query(0, ge=0), session_id: str = Query(""), limit: int = Query(200, ge=1, le=1000)):
        from bot.localchat import store

        return {"events": store.events(after=after, session_id=session_id, limit=limit)}

    @app.post("/api/local-chat/sessions/{sid}/read", dependencies=write)
    async def local_chat_read(sid: str):
        from bot.localchat import store

        _session_or_404(sid)
        s = store.mark_read(sid)
        await _announce(s)
        return s

    @app.post("/api/local-chat/sessions/{sid}/steer", dependencies=write)
    async def local_chat_steer(sid: str, payload: dict = Body(...)):
        from bot.localchat import chat

        _session_or_404(sid)
        try:
            return chat.steer(sid, str(payload.get("text") or ""), str(payload.get("how") or "steer"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/local-chat/sessions/{sid}/queued", dependencies=read)
    async def local_chat_queued(sid: str):
        from bot.localchat import store

        _session_or_404(sid)
        return {"queued": store.queued(sid)}

    @app.delete("/api/local-chat/sessions/{sid}/queued/{qid}", dependencies=write)
    async def local_chat_queued_drop(sid: str, qid: int):
        from bot.localchat import store

        return {"dropped": store.drop_queued(sid, qid)}

    @app.get("/api/local-chat/sessions/{sid}/checkpoints", dependencies=read)
    async def local_chat_checkpoints(sid: str):
        from bot.localchat import store

        _session_or_404(sid)
        return {"checkpoints": store.checkpoints(sid)}

    @app.post("/api/local-chat/sessions/{sid}/rewind", dependencies=write)
    async def local_chat_rewind(sid: str, payload: dict = Body(...)):
        from bot.localchat import chat

        _session_or_404(sid)
        try:
            return await asyncio.to_thread(chat.rewind, sid, str(payload.get("turn_id") or ""))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:  # noqa: BLE001 - say why the files could not be restored
            raise HTTPException(status_code=500, detail=f"rewind failed: {e}") from e

    @app.get("/api/local-chat/sessions/{sid}/worktree", dependencies=read)
    async def local_chat_worktree(sid: str):
        from bot.localchat import worktrees

        s = _session_or_404(sid)
        if not s.get("worktree"):
            return {"worktree": None}
        return {"worktree": await asyncio.to_thread(_wt_errors, worktrees.status, sid)}

    @app.post("/api/local-chat/sessions/{sid}/worktree", dependencies=write)
    async def local_chat_worktree_create(sid: str, payload: Optional[dict] = Body(None)):
        from bot.localchat import store, worktrees

        _session_or_404(sid)
        p = payload or {}
        wt = await asyncio.to_thread(_wt_errors, worktrees.create, sid, base=str(p.get("base") or "") or None,
                                     name=str(p.get("name") or "") or None)
        await _announce(store.get(sid))
        return {"worktree": wt}

    @app.get("/api/local-chat/sessions/{sid}/worktree/diff", dependencies=read)
    async def local_chat_worktree_diff(sid: str):
        from bot.localchat import worktrees

        _session_or_404(sid)
        return await asyncio.to_thread(_wt_errors, worktrees.diff, sid)

    @app.post("/api/local-chat/sessions/{sid}/worktree/sync", dependencies=write)
    async def local_chat_worktree_sync(sid: str, payload: Optional[dict] = Body(None)):
        from bot.localchat import chat, store, worktrees

        s = _session_or_404(sid)
        res = await asyncio.to_thread(_wt_errors, worktrees.sync, sid)
        if res["conflicts"]:
            prompt = worktrees.conflict_prompt(s["worktree"], res["conflicts"])
            store.add_message(sid, "system", "Syncing with " + s["worktree"]["base"] + " stopped on conflicts in: "
                              + ", ".join(res["conflicts"]) + ". The merge is left in progress for the agent to resolve.")
            res["prompt"] = prompt
            if (payload or {}).get("hand_to_agent"):
                try:
                    res["turn"] = await chat.send(sid, prompt, _broadcast)
                except (RuntimeError, ValueError, LookupError) as e:
                    res["turn_error"] = str(e)
        return res

    @app.get("/api/local-chat/sessions/{sid}/worktree/commit-message", dependencies=read)
    async def local_chat_worktree_draft(sid: str):
        from bot.localchat import worktrees

        _session_or_404(sid)
        return {"message": await asyncio.to_thread(_wt_errors, worktrees.draft_message, sid), "source": "drafted from the changes"}

    @app.post("/api/local-chat/sessions/{sid}/worktree/commit", dependencies=write)
    async def local_chat_worktree_commit(sid: str, payload: dict = Body(...)):
        from bot.localchat import worktrees

        _session_or_404(sid)
        return await asyncio.to_thread(_wt_errors, worktrees.commit, sid, str(payload.get("message") or ""))

    @app.post("/api/local-chat/sessions/{sid}/worktree/keep", dependencies=write)
    async def local_chat_worktree_keep(sid: str, payload: dict = Body(...)):
        from bot.localchat import worktrees

        _session_or_404(sid)
        return await asyncio.to_thread(_wt_errors, worktrees.set_keep, sid, bool(payload.get("keep")))

    @app.post("/api/local-chat/sessions/{sid}/worktree/discard", dependencies=write)
    async def local_chat_worktree_discard(sid: str, payload: Optional[dict] = Body(None)):
        from bot.localchat import store, worktrees

        _session_or_404(sid)
        res = await asyncio.to_thread(_wt_errors, worktrees.discard, sid, confirm=bool((payload or {}).get("confirm")))
        await _announce(store.get(sid))
        return res

    @app.post("/api/local-chat/sessions/{sid}/worktree/queue-merge", dependencies=write)
    async def local_chat_worktree_queue(sid: str):
        from bot.localchat import worktrees

        _session_or_404(sid)
        return {"queued": await asyncio.to_thread(_wt_errors, worktrees.queue_merge, sid)}

    @app.post("/api/local-chat/worktrees/prune", dependencies=write)
    async def local_chat_worktrees_prune(payload: Optional[dict] = Body(None)):
        from bot.localchat import worktrees

        return await asyncio.to_thread(_wt_errors, worktrees.prune, clean_merged=bool((payload or {}).get("clean_merged")))
