"""Dashboard routes: server chat.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import asyncio
import mimetypes
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from bot import attachments, bot_instances, db, thumbnails


def register(app: FastAPI, *, json_download: Callable) -> None:
    from bot.dashboard.server import MAX_ATTACHMENT_BYTES, _require_device_id, _require_token_or_api_key, _ts_stamp
    _json_download = json_download

    # A permanent, bot-independent messaging/file channel between the
    # devices themselves — the desktop app and every paired Android phone —
    # separate from the platform-facing /api/chat/* routes above (those go
    # through a bot_instance and an external platform SDK; this never
    # does). One shared "Server Chat" group room plus a private 1:1 with
    # every other device, auto-opened the moment a device is paired — see
    # bot/db.py's server_chat_conversations comment and
    # create_conversations_for_new_device().

    @app.get("/api/server-chat/whoami")
    async def api_server_chat_whoami(device_id: int = Depends(_require_device_id)):
        return {"device_id": device_id}

    @app.get("/api/server-chat/conversations")
    def api_server_chat_conversations(device_id: int = Depends(_require_device_id)):
        return db.list_server_chat_conversations(device_id)

    @app.get("/api/server-chat/messages")
    def api_server_chat_messages(
        conversation_id: int,
        after_id: int = 0,
        limit: int = 100,
        device_id: int = Depends(_require_device_id),
    ):
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        return [dict(r) for r in db.list_server_chat_messages(conversation_id, after_id=after_id, limit=limit)]

    @app.get("/api/server-chat/conversations/{conversation_id}/export")
    def api_server_chat_export(conversation_id: int, device_id: int = Depends(_require_device_id)):
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        data = db.export_server_chat_data(conversation_id)
        return _json_download(
            {"conversation_id": conversation_id, "messages": data},
            f"server-chat-{conversation_id}-{_ts_stamp()}.json",
        )

    @app.delete("/api/server-chat/conversations/{conversation_id}")
    def api_server_chat_clear(conversation_id: int, full: bool = False, device_id: int = Depends(_require_device_id)):
        """`full=false` (default): clear every message, keep the
        conversation itself — the group room and every direct
        conversation are structural (see module comment above), so this
        is what "delete chat" meant before `full` existed.
        `full=true`: also drop the conversation row — genuinely removes
        it from the list until either device messages the other again
        (see POST /api/server-chat/conversations, which re-opens exactly
        this row on demand). Refused for the group room: a shared room
        can't be unilaterally deleted out from under every other device."""
        row = db.get_conn().execute(
            "SELECT kind FROM server_chat_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row is None or not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        # The permanent group room can't be deleted OR cleared — it's the
        # one Server Chat conversation meant to survive forever, including
        # its history, per the "Admin control surface" plan's permanence
        # requirement. Direct (1:1) conversations remain fully clearable
        # and deletable exactly as before.
        if row["kind"] == "group":
            raise HTTPException(status_code=400, detail="the group room is permanent and can't be cleared or deleted")
        if full:
            db.delete_server_chat_conversation(conversation_id)
            return {"ok": True, "deleted_conversation": True}
        count = db.clear_server_chat_messages(conversation_id)
        return {"ok": True, "deleted": count}

    @app.post("/api/server-chat/conversations")
    def api_server_chat_open(payload: dict = Body(...), device_id: int = Depends(_require_device_id)):
        """Opens (or re-opens, if it was previously fully deleted) a
        direct conversation with another paired device — the entry point
        for "message this device" after a full delete, since a deleted
        direct conversation has no id left to reference."""
        peer_device_id = payload.get("peer_device_id")
        if not isinstance(peer_device_id, int):
            raise HTTPException(status_code=400, detail="payload must be {peer_device_id: <int>}")
        if peer_device_id == device_id:
            raise HTTPException(status_code=400, detail="can't open a conversation with yourself")
        conversation_id = db.ensure_direct_conversation(device_id, peer_device_id)
        return {"ok": True, "conversation_id": conversation_id}

    @app.delete("/api/server-chat/messages/{message_id}")
    def api_server_chat_delete_message(message_id: int, device_id: int = Depends(_require_device_id)):
        """Deletes one message — restricted to the device that actually
        sent it (like every ordinary chat app's "delete message," not a
        moderation action any participant can take on anyone else's
        text). Use DELETE /api/server-chat/conversations/{id} instead to
        clear a whole conversation's history."""
        row = db.get_server_chat_message(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such message")
        if row["sender_device_id"] != device_id:
            raise HTTPException(status_code=403, detail="you can only delete your own messages")
        conv = db.get_conn().execute(
            "SELECT kind FROM server_chat_conversations WHERE id=?", (row["conversation_id"],)
        ).fetchone()
        if conv is not None and conv["kind"] == "group":
            raise HTTPException(status_code=400, detail="messages in the permanent group room can't be deleted")
        db.delete_server_chat_message(message_id)
        return {"ok": True}

    @app.post("/api/server-chat/send")
    async def api_server_chat_send(payload: dict = Body(...), device_id: int = Depends(_require_device_id)):
        conversation_id = payload.get("conversation_id")
        text = (payload.get("text") or "").strip()
        if not isinstance(conversation_id, int) or not text:
            raise HTTPException(status_code=400, detail="payload must be {conversation_id: <int>, text: <str>}")
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        msg_id = db.create_server_chat_message(conversation_id, device_id, text)
        # Admin control surface plan, Section 3 — the permanent group
        # room doubles as the channel you talk to AgenticBotPlatform in; a no-op
        # for direct (1:1) conversations and when no admin instance is
        # configured (see server_chat_admin.py's own guards).
        from bot import server_chat_admin

        await server_chat_admin.maybe_handle_group_message(conversation_id, device_id, text)
        return {"ok": True, "id": msg_id}

    @app.post("/api/server-chat/approvals/{approval_id}/resolve")
    async def api_server_chat_approval_resolve(
        approval_id: int, payload: dict = Body(...), device_id: int = Depends(_require_device_id),
    ):
        from bot.agent_runtime import approval as agent_approval

        outcome = (payload.get("outcome") or "").strip()
        if outcome not in ("once", "session", "always", "deny"):
            raise HTTPException(status_code=400, detail="outcome must be one of once/session/always/deny")
        actor = f"device:{device_id}"
        ok = agent_approval.resolve(approval_id, outcome, actor=actor)
        if not ok:
            raise HTTPException(status_code=409, detail="already resolved or no such approval")
        return {"ok": True}

    @app.post("/api/server-chat/send-file")
    async def api_server_chat_send_file(
        conversation_id: int = Form(...),
        text: str = Form(""),
        file: UploadFile = File(...),
        device_id: int = Depends(_require_device_id),
    ):
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        try:
            rel_path, orig_name = await attachments.safe_store_stream(file.filename, file, MAX_ATTACHMENT_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        mime = file.content_type or mimetypes.guess_type(orig_name)[0]
        size = (attachments.ATTACHMENTS_DIR / rel_path).stat().st_size
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        msg_id = db.create_server_chat_message(
            conversation_id, device_id, (text or "").strip(),
            attachment_path=rel_path, attachment_name=orig_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id}

    @app.post("/api/server-chat/uploads/init")
    def api_server_chat_uploads_init(payload: dict = Body(...), device_id: int = Depends(_require_device_id)):
        conversation_id = payload.get("conversation_id")
        filename = payload.get("filename") or "file"
        total_size = int(payload.get("total_size") or 0)
        mime = payload.get("mime")
        text = (payload.get("text") or "").strip()
        if not isinstance(conversation_id, int):
            raise HTTPException(status_code=400, detail="payload must include conversation_id")
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        try:
            session = attachments.create_upload_session(
                filename, total_size, mime, MAX_ATTACHMENT_BYTES,
                conversation_id=conversation_id, sender_device_id=device_id, text=text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return session

    @app.put("/api/server-chat/uploads/{session_id}/chunk/{index}")
    async def api_server_chat_uploads_chunk(session_id: str, index: int, request: Request, device_id: int = Depends(_require_device_id)):
        try:
            await attachments.write_chunk(session_id, index, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown or expired upload session") from exc
        return {"ok": True}

    @app.post("/api/server-chat/uploads/{session_id}/complete")
    async def api_server_chat_uploads_complete(session_id: str, device_id: int = Depends(_require_device_id)):
        try:
            assembled = await asyncio.get_running_loop().run_in_executor(
                None, attachments.assemble_upload, session_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown or expired upload session") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conversation_id = assembled["conversation_id"]
        rel_path = assembled["rel_path"]
        display_name = assembled["display_name"]
        mime = assembled["mime"]
        size = assembled["size"]
        text = assembled["text"]
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        msg_id = db.create_server_chat_message(
            conversation_id, assembled["sender_device_id"], text,
            attachment_path=rel_path, attachment_name=display_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id}

    @app.get("/api/server-chat/attachments/{message_id}")
    def api_server_chat_attachment(message_id: int, device_id: int = Depends(_require_device_id)):
        row = db.get_server_chat_message(message_id)
        if row is None or not row["attachment_path"] or not db.is_conversation_participant(row["conversation_id"], device_id):
            raise HTTPException(status_code=404, detail="no such attachment")
        full_path = attachments.ATTACHMENTS_DIR / row["attachment_path"]
        if not full_path.resolve().is_relative_to(attachments.ATTACHMENTS_DIR.resolve()) or not full_path.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing")
        return FileResponse(full_path, media_type=row["attachment_mime"] or "application/octet-stream", filename=row["attachment_name"] or full_path.name)

    @app.get("/api/server-chat/attachments/{message_id}/thumbnail")
    def api_server_chat_attachment_thumbnail(message_id: int, device_id: int = Depends(_require_device_id)):
        row = db.get_server_chat_message(message_id)
        if row is None or not row["thumbnail_path"] or not db.is_conversation_participant(row["conversation_id"], device_id):
            raise HTTPException(status_code=404, detail="no such thumbnail")
        full_path = attachments.THUMBS_DIR / row["thumbnail_path"]
        if not full_path.resolve().is_relative_to(attachments.THUMBS_DIR.resolve()) or not full_path.is_file():
            raise HTTPException(status_code=404, detail="thumbnail file missing")
        return FileResponse(full_path, media_type="image/jpeg")

    @app.get("/api/sessions", dependencies=[Depends(_require_token_or_api_key)])
    def api_sessions(
        instance_id: Optional[int] = None,
        q: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 50,
    ):
        rows = [dict(r) for r in db.list_sessions(instance_id=instance_id, q=q, since=since, until=until, limit=limit)]
        legacy = []
        # Legacy rows have no per-item timestamp/title to filter on, so a
        # search or date-range query — which the real title/last_activity_at
        # columns can satisfy — should exclude this synthetic bucket rather
        # than always showing it regardless of the filter.
        instance_ids = (
            [] if (q or since or until)
            else [instance_id] if instance_id is not None
            else [inst["id"] for inst in bot_instances.list_instances()]
        )
        for iid in instance_ids:
            count = db.count_legacy_items(iid)
            if count:
                legacy.append({
                    "id": f"legacy-{iid}",
                    "instance_id": iid,
                    "chat_id": None,
                    "title": "Before sessions",
                    "started_at": None,
                    "last_activity_at": None,
                    "item_count": count,
                    "legacy": True,
                })
        return rows + legacy

    @app.get("/api/sessions/{session_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_session_detail(session_id: str):
        if session_id.startswith("legacy-"):
            iid = int(session_id.removeprefix("legacy-"))
            items = db.get_legacy_items(iid)
            session = {
                "id": session_id, "instance_id": iid, "chat_id": None,
                "title": "Before sessions", "started_at": None, "last_activity_at": None,
                "item_count": len(items["messages"]) + len(items["jobs"]), "legacy": True,
            }
        else:
            row = db.get_session(int(session_id))
            if row is None:
                raise HTTPException(status_code=404, detail=f"session {session_id} not found")
            session = dict(row)
            items = db.get_session_items(int(session_id))
        return {
            "session": session,
            "messages": [dict(r) for r in items["messages"]],
            "jobs": [dict(r) for r in items["jobs"]],
        }

    @app.get("/api/sessions/{session_id}/export", dependencies=[Depends(_require_token_or_api_key)])
    def api_session_export(session_id: str):
        if session_id.startswith("legacy-"):
            data = db.export_legacy_data(int(session_id.removeprefix("legacy-")))
        else:
            data = db.export_session_data(int(session_id))
            if data is None:
                raise HTTPException(status_code=404, detail=f"session {session_id} not found")
        return _json_download(data, f"session-{session_id}-{_ts_stamp()}.json")

    @app.get("/api/sessions/export", dependencies=[Depends(_require_token_or_api_key)])
    def api_sessions_export_all(instance_id: Optional[int] = None):
        # One combined file rather than one download per session — every
        # real session plus each affected bot's legacy ("Before sessions")
        # bucket, matching exactly what GET /api/sessions itself lists.
        rows = db.list_sessions(instance_id=instance_id, limit=1_000_000)
        bundle = [db.export_session_data(row["id"]) for row in rows]
        instance_ids = [instance_id] if instance_id is not None else [inst["id"] for inst in bot_instances.list_instances()]
        for iid in instance_ids:
            if db.count_legacy_items(iid):
                bundle.append(db.export_legacy_data(iid))
        suffix = f"-bot{instance_id}" if instance_id is not None else ""
        return _json_download({"sessions": bundle}, f"sessions-backup{suffix}-{_ts_stamp()}.json")

    @app.delete("/api/sessions/{session_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_session_delete(session_id: str):
        if session_id.startswith("legacy-"):
            count = db.clear_legacy_items(int(session_id.removeprefix("legacy-")))
            return {"ok": True, "deleted_messages": count}
        ok = db.delete_session(int(session_id))
        if not ok:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found")
        return {"ok": True}
