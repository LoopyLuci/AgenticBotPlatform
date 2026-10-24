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
    GET    /api/local-chat/grok-mode                  conductor.grok.mode: set value, choices, what a Factory run gets now
    PUT    /api/local-chat/grok-mode                  {"mode"?: off|advisor|confined|auto, "network_approved"?: bool} change
                                                      either (audited; the next run reads it)

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
Linked sessions (S3):
    POST   /api/local-chat/sessions/{sid}/fork        {title?, base?} a new session with this conversation, in its own worktree
    POST   /api/local-chat/sessions/{sid}/task        {brief, title?, start?} a fresh session on a brief (started unless false)
    POST   /api/local-chat/sessions/{sid}/handoff     {plan, title?, stop?} the in-progress task moves to a new session
    GET    /api/local-chat/sessions/{sid}/children    the sessions linked under it, with their status
When a linked session ends a turn, a note with its recap lands in the parent (local_chat_note frame).

Agent and Factory in chat (P4):
    POST   /api/local-chat/sessions/{sid}/send        {"text", "cloud"?} cloud: this one message goes to the cloud backend
                                                      (refused when the session's cloud assist is off)
    GET    /api/local-chat/sessions/{sid}/context     the context meter (estimated tokens vs the model's context per request),
                                                      tokens used (local / cloud), the tools this mode offers, the autonomy
                                                      policy, cloud assist and the capacity governor's state
    POST   /api/local-chat/sessions/{sid}/factory/run {"plan_id", "confirm"?} the owner's Run of a Factory plan: confined
                                                      workers (refused without the sandbox); the first run in a repository
                                                      needs confirm (409 until then)
    POST   /api/local-chat/sessions/{sid}/factory/stop
    GET    /api/local-chat/sessions/{sid}/factory     this session's Factory board: worker sessions and push-queue entries

Long sessions (S4):
    POST   /api/local-chat/sessions/{sid}/compact     summarize the older turns now (a visible marker; nothing is deleted)
    POST   /api/local-chat/sessions/{sid}/chapters    {"title"} a chapter marker
    GET    /api/local-chat/sessions/{sid}/export?format=md|json   the whole session as a file
Auto-archive (local_chat.auto_archive_days) runs at start and at most hourly with the status call.
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
    housekeeping_at = {"t": 0.0}

    def _housekeep() -> None:
        import time as _time

        if _time.time() - housekeeping_at["t"] < 3600:
            return
        housekeeping_at["t"] = _time.time()
        try:
            from bot.localchat import housekeeping

            housekeeping.auto_archive()
        except Exception:  # noqa: BLE001 - housekeeping never breaks a request
            pass
    _housekeep()

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
        from bot import localchat, ollama
        from bot.localchat import chat, store
        from bot.modelmistress import supervisor

        await asyncio.to_thread(_housekeep)
        hub = await asyncio.to_thread(supervisor.status)
        return {"hub": hub, "settings": localchat.settings(), "running": [t for t in store.running_turns()
                                                                         if chat.is_running(t["session_id"])],
                "ollama_enabled": ollama.enabled()}      # Settings → Local models shows and flips it

    def _grok_mode_doc() -> dict:
        """conductor.grok.mode as the Workspace's dropdown shows it: the set value, every choice, and what a Factory
        run would actually get right now (confined falls back or refuses when network is not approved)."""
        from bot.conductor import grok_worker

        gcfg = grok_worker.settings()
        mode = str(gcfg.get("mode") or "off")
        try:
            effective, why = grok_worker.resolve_mode(gcfg), ""
        except grok_worker.GrokUnavailable as e:
            effective, why = "off", str(e)
        return {"mode": mode, "modes": list(grok_worker.MODES), "effective": effective, "why": why,
                "network_approved": bool(gcfg.get("network_approved"))}

    @app.get("/api/local-chat/grok-mode", dependencies=read)
    async def local_chat_grok_mode():
        return await asyncio.to_thread(_grok_mode_doc)

    @app.put("/api/local-chat/grok-mode", dependencies=write)
    def local_chat_set_grok_mode(payload: dict = Body(...)):
        """Change how Grok joins Factory runs, and whether a confined Grok leaf may use the internet, on the fly (the next
        run reads it; nothing restarts). Either field may be sent alone; both switch back off the same way."""
        from bot import db
        from bot.conductor import grok_worker
        from bot.config import config

        changes: dict = {}
        if "mode" in payload:
            mode = str(payload.get("mode") or "").strip().lower()
            if mode not in grok_worker.MODES:
                raise HTTPException(status_code=400, detail=f"mode must be one of: {', '.join(grok_worker.MODES)}")
            changes[("conductor", "grok", "mode")] = mode
        if "network_approved" in payload:
            if not isinstance(payload["network_approved"], bool):
                raise HTTPException(status_code=400, detail="network_approved must be true or false")
            changes[("conductor", "grok", "network_approved")] = payload["network_approved"]
        if not changes:
            raise HTTPException(status_code=400, detail="send mode and/or network_approved")
        config.set_values(changes, actor="dashboard")
        db.log_audit(actor="dashboard", action="grok_mode_set",
                     detail=", ".join(f"{k[-1]}={v}" for k, v in changes.items()))
        return _grok_mode_doc()

    @app.get("/api/local-chat/models", dependencies=read)
    async def local_chat_models(refresh: bool = Query(False)):
        from bot.localchat import catalog

        cloud = await asyncio.to_thread(catalog.cloud_rows)          # Grok models: picked explicitly, never a default
        try:
            doc = await asyncio.to_thread(catalog.get, refresh=refresh)
        except Exception as e:  # noqa: BLE001 - no hub is a normal state on the first screen: say why, not an HTTP error
            return catalog.with_cloud({"models": [], "groups": [], "default_model": "", "factory_models": [],
                                       "error": f"ModelMistress did not answer: {e}"}, cloud)
        from bot.localchat import factory

        return catalog.with_cloud({**doc, "default_model": catalog.default_model(doc), "factory_models": sorted(factory.busy_models())},
                                  cloud)

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
            return await chat.send(sid, str(payload.get("text") or ""), _broadcast, cloud=bool(payload.get("cloud")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except LookupError as e:
            # 502, not 503: the dashboard's api() reads 503 as "not signed in" and shows the sign-in notice instead
            raise HTTPException(status_code=502, detail=str(e)) from e
        except Exception as e:
            from bot.modelmistress.client import MMError

            if isinstance(e, MMError):           # no hub, or one that cannot be reached: said plainly, at once
                raise HTTPException(status_code=502, detail=str(e)) from e
            if isinstance(e, RuntimeError):
                raise HTTPException(status_code=409, detail=str(e)) from e
            raise

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

    # ---- linked sessions (S3) -----------------------------------------------------------------------------------------------
    def _link_errors(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/local-chat/sessions/{sid}/fork", dependencies=write)
    async def local_chat_fork(sid: str, payload: Optional[dict] = Body(None)):
        from bot.localchat import links

        _session_or_404(sid)
        p = payload or {}
        res = await asyncio.to_thread(_link_errors, links.fork, sid, title=str(p.get("title") or ""), base=str(p.get("base") or "") or None)
        await _announce(res["session"])
        return res

    @app.post("/api/local-chat/sessions/{sid}/task", dependencies=write)
    async def local_chat_task(sid: str, payload: dict = Body(...)):
        from bot.localchat import chat, links

        _session_or_404(sid)
        res = await asyncio.to_thread(_link_errors, links.task, sid, str(payload.get("brief") or ""), title=str(payload.get("title") or ""))
        await _announce(res["session"])
        if payload.get("start", True):
            try:
                res["turn"] = await chat.send(res["session"]["id"], res["brief"], _broadcast)
            except (RuntimeError, ValueError, LookupError) as e:
                res["turn_error"] = str(e)
        return res

    @app.post("/api/local-chat/sessions/{sid}/handoff", dependencies=write)
    async def local_chat_handoff(sid: str, payload: dict = Body(...)):
        from bot.localchat import chat, links

        _session_or_404(sid)
        if payload.get("stop") and chat.is_running(sid):
            await chat.cancel(sid)
        res = await asyncio.to_thread(_link_errors, links.handoff, sid, str(payload.get("plan") or ""), title=str(payload.get("title") or ""))
        await _announce(res["session"])
        if payload.get("start", True):
            try:
                res["turn"] = await chat.send(res["session"]["id"], "Continue with what is left to do.", _broadcast)
            except (RuntimeError, ValueError, LookupError) as e:
                res["turn_error"] = str(e)
        return res

    @app.get("/api/local-chat/sessions/{sid}/children", dependencies=read)
    async def local_chat_children(sid: str):
        from bot.localchat import links

        _session_or_404(sid)
        return {"children": links.children(sid)}

    # ---- agent and Factory in chat (P4) ---------------------------------------------------------------------------------
    @app.get("/api/local-chat/sessions/{sid}/context", dependencies=read)
    async def local_chat_context(sid: str):
        from bot.localchat import autonomy, catalog, chat, store

        s = _session_or_404(sid)
        msgs = store.messages(sid, include_reverted=False)
        chars = sum(len(m["text"]) for m in msgs if m["role"] in ("user", "assistant", "system"))
        per_slot = None
        try:
            doc = await asyncio.to_thread(catalog.get)
            row = next((r for r in doc.get("models", []) if r["id"] == s["model"]), None)
            per_slot = ((row or {}).get("plan") or {}).get("n_ctx_per_slot")
        except Exception:  # noqa: BLE001 - no hub: the meter says it does not know the limit
            pass
        tools = chat.tools_for(s["mode"])
        try:
            from bot.capacity import governor

            gstate = governor.effective_state()
        except Exception:  # noqa: BLE001
            gstate = None
        pol = autonomy.policy(s.get("autonomy", 2))
        return {"meter": {"used": chars // 4, "source": "estimated (characters / 4)", "limit": per_slot,
                          "limit_source": "planned (the hub serves this many tokens per request)" if per_slot else None},
                "tokens": {"local": sum(m["tokens"] for m in msgs if m["role"] == "assistant" and m["model"] != chat.CLOUD),
                           "cloud": sum(m["tokens"] for m in msgs if m["role"] == "assistant" and m["model"] == chat.CLOUD)},
                "tools": "none" if tools == frozenset() else ("all" if tools is None else sorted(tools)),
                "autonomy": {k: pol[k] for k in ("level", "label", "summary", "permission_mode")},
                "cloud": {"assist": s["cloud_assist"], "governor": gstate}}

    def _factory_errors(e: Exception):
        from bot.localchat import factory

        if isinstance(e, factory.NeedsConfirmation):
            return HTTPException(status_code=409, detail={"message": str(e), "needs_confirmation": True})
        if isinstance(e, factory.FactoryRefused):
            return HTTPException(status_code=400, detail=str(e))
        if isinstance(e, LookupError):
            return HTTPException(status_code=404, detail=str(e))
        return HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    @app.post("/api/local-chat/sessions/{sid}/factory/run", dependencies=write)
    async def local_chat_factory_run(sid: str, payload: dict = Body(...)):
        from bot.localchat import factory

        _session_or_404(sid)
        try:
            return await factory.start(sid, str(payload.get("plan_id") or ""), _broadcast, confirm=bool(payload.get("confirm")))
        except (factory.FactoryRefused, LookupError) as e:
            raise _factory_errors(e) from e

    @app.post("/api/local-chat/sessions/{sid}/factory/stop", dependencies=write)
    async def local_chat_factory_stop(sid: str):
        from bot.localchat import factory

        _session_or_404(sid)
        return {"stopped": await factory.stop(sid)}

    @app.get("/api/local-chat/sessions/{sid}/factory", dependencies=read)
    async def local_chat_factory_board(sid: str):
        from bot.localchat import factory

        _session_or_404(sid)
        return factory.board(sid)

    # ---- long sessions (S4) -------------------------------------------------------------------------------------------------
    @app.post("/api/local-chat/sessions/{sid}/compact", dependencies=write)
    async def local_chat_compact(sid: str):
        from bot.localchat import chat, housekeeping

        s = _session_or_404(sid)
        if chat.is_running(sid):
            raise HTTPException(status_code=409, detail="wait for the running turn to finish")
        summarize = (lambda t: chat.summarize_with(s["model"], sid, t)) if s["model"] else None
        return await housekeeping.compact(sid, summarize, reason="asked for")

    @app.post("/api/local-chat/sessions/{sid}/chapters", dependencies=write)
    async def local_chat_chapter(sid: str, payload: dict = Body(...)):
        from bot.localchat import housekeeping

        _session_or_404(sid)
        try:
            return {"id": housekeeping.add_chapter(sid, str(payload.get("title") or ""))}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/local-chat/sessions/{sid}/export", dependencies=read)
    async def local_chat_export(sid: str, format: str = Query("md", pattern="^(md|json)$")):
        from fastapi.responses import Response

        from bot.localchat import housekeeping

        s = _session_or_404(sid)
        body, media = housekeeping.export(sid, format)
        name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (s["title"] or "session"))[:60] or "session"
        return Response(content=body.encode("utf-8"), media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}.{format}"'})
