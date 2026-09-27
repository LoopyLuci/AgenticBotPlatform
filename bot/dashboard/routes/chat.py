"""Dashboard routes: chat.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import asyncio
import mimetypes
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from bot import attachments, bot_instances, db, outbox, thumbnails
from bot import commands as bot_commands
from bot.backends.base import BackendError
from bot.router import router


def register(app: FastAPI, *, json_download: Callable) -> None:
    from bot.dashboard.server import (
        MAX_ATTACHMENT_BYTES,
        PLATFORM_RELAY_LIMIT_BYTES,
        _app_chat_sessions,
        _caller_thread_identity,
        _require_token_or_api_key,
        _ts_stamp,
    )
    _json_download = json_download

    # Real conversation content, not just metadata — token-gated for reads
    # too, same reasoning as the .env editor above.

    @app.get("/api/chat/recipients", dependencies=[Depends(_require_token_or_api_key)])
    def api_chat_recipients():
        connected = set(outbox.available_instances())
        return {
            "instances": [
                {
                    "id": inst["id"],
                    "name": inst["name"],
                    "platform": inst["platform"],
                    "allowed_ids": sorted(inst["allowed_user_ids"], key=str),
                    "connected": inst["id"] in connected,
                }
                for inst in bot_instances.list_instances()
            ]
        }

    @app.get("/api/chat/messages", dependencies=[Depends(_require_token_or_api_key)])
    def api_chat_messages(
        limit: int = 100,
        platform: Optional[str] = None,
        chat_id: Optional[str] = None,
        after_id: Optional[int] = None,
        instance_id: Optional[int] = None,
    ):
        rows = db.list_messages(
            limit=limit, platform=platform, chat_id=chat_id, after_id=after_id, instance_id=instance_id
        )
        return [dict(r) for r in rows]

    @app.get("/api/chat/messages/export", dependencies=[Depends(_require_token_or_api_key)])
    def api_chat_messages_export(instance_id: int, chat_id: Optional[str] = None, platform: Optional[str] = None):
        # No chat_id: the whole bot's merged history, matching what the
        # Chat tab itself displays (one timeline per instance, every
        # chat_id combined) — see refreshChat() in dashboard.html.
        if chat_id is None:
            data = db.export_instance_messages_data(instance_id)
        else:
            data = db.export_chat_messages_data(instance_id, chat_id, platform=platform)
        suffix = f"-{chat_id}" if chat_id else ""
        return _json_download(
            {"instance_id": instance_id, "chat_id": chat_id, "messages": data},
            f"chat-{instance_id}{suffix}-{_ts_stamp()}.json",
        )

    @app.delete("/api/chat/messages", dependencies=[Depends(_require_token_or_api_key)])
    def api_chat_messages_delete(instance_id: int = Body(...), chat_id: Optional[str] = Body(None), platform: Optional[str] = Body(None)):
        if chat_id is None:
            count = db.delete_instance_messages(instance_id)
        else:
            count = db.delete_chat_messages(instance_id, chat_id, platform=platform)
        return {"ok": True, "deleted": count}

    @app.delete("/api/chat/messages/{message_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_chat_message_delete_one(message_id: int):
        if db.get_message(message_id) is None:
            raise HTTPException(status_code=404, detail="no such message")
        db.delete_message(message_id)
        return {"ok": True}

    @app.post("/api/chat/send", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_send(payload: dict = Body(...)):
        instance_id = payload.get("instance_id")
        chat_id = payload.get("chat_id")
        text = (payload.get("text") or "").strip()
        if not instance_id or not chat_id or not text:
            raise HTTPException(status_code=400, detail="payload must be {instance_id: int, chat_id: str|int, text: str}")
        instance = bot_instances.get_instance(int(instance_id))
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            await outbox.send_message(int(instance_id), chat_id, text)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"send failed: {exc}") from exc
        db.log_message(
            platform=instance["platform"], chat_id=chat_id, direction="out", source="dashboard",
            text=text, instance_id=int(instance_id),
        )
        return {"ok": True}

    # ------------------------------------------------- "Chat with Bot" mode -
    # The Chat tab's other mode: /api/chat/send (above) is "Send from
    # Server" — the dashboard/app pushes a message OUT, through outbox.py +
    # a live platform SDK, appearing to a real Telegram/Discord/Slack user as
    # if it came from the bot. This is the reverse direction: a real message
    # FROM the dashboard operator or a real paired device TO the bot, using
    # the exact same CmdContext/dispatch_command/router.ask() pipeline every
    # Telegram/Discord/Slack handler uses (see e.g. discord_platform.py's
    # on_message) — genuinely processed, genuinely replied to. Nothing here
    # is simulated: the sender's identity comes from real request auth (see
    # _caller_thread_identity), not a client-declared value, and is logged
    # as platform="app" — the Agentic Bot Platform App's own real channel — rather
    # than disguised as whichever platform the target instance happens to
    # also use. Never touches outbox.py or any platform SDK.
    @app.post("/api/chat/send-to-bot", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_send_to_bot(
        payload: dict = Body(...),
        identity: tuple[str, str, str] = Depends(_caller_thread_identity),
    ):
        instance_id = payload.get("instance_id")
        text = (payload.get("text") or "").strip()
        if not instance_id or not text:
            raise HTTPException(status_code=400, detail="payload must be {instance_id: int, text: str}")
        instance = bot_instances.get_instance(int(instance_id))
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        source, chat_id, username = identity
        db.log_message(
            platform="app", chat_id=chat_id, user_id=chat_id, username=username,
            direction="in", source=source, text=text, instance_id=int(instance_id),
        )
        session = _app_chat_sessions.setdefault((int(instance_id), chat_id), {})
        cmd_ctx = bot_commands.CmdContext(
            instance_id=int(instance_id), instance_name=instance["name"], user_id=chat_id,
            chat_id=chat_id, actor=f"{source}:{chat_id}", session=session,
        )
        try:
            cmd_reply = await bot_commands.dispatch_command(text, cmd_ctx)
            if cmd_reply is not None:
                reply_text = cmd_reply
            else:
                result = await router.ask(
                    text, action_type=session.get("action_type", "quick_question"), user_id=chat_id,
                    context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
                    instance_id=int(instance_id), chat_id=chat_id,
                )
                reply_text = result.text
        except BackendError as exc:
            reply_text = f"Backend failed: {exc}"
        db.log_message(
            platform="app", chat_id=chat_id, direction="out", source="bot",
            text=reply_text, instance_id=int(instance_id),
        )
        return {"ok": True, "reply": reply_text}

    @app.post("/api/chat/send-file", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_send_file(
        instance_id: int = Form(...),
        chat_id: str = Form(...),
        text: str = Form(""),
        file: UploadFile = File(...),
    ):
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        # Kept as the simple one-shot path for small files — the chunked
        # /api/uploads/* flow below is what desktop/Android use for
        # anything sizeable, but there's no reason to force a 3-request
        # dance for a 200KB image. Still capped at the platform relay limit
        # since this path always relays immediately.
        try:
            rel_path, orig_name = await attachments.safe_store_stream(file.filename, file, PLATFORM_RELAY_LIMIT_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        caption = (text or "").strip()
        try:
            await outbox.send_file(
                instance_id, chat_id, str(attachments.ATTACHMENTS_DIR / rel_path), orig_name, caption or None
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"send failed: {exc}") from exc
        mime = file.content_type or mimetypes.guess_type(orig_name)[0]
        size = (attachments.ATTACHMENTS_DIR / rel_path).stat().st_size
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        msg_id = db.log_message(
            platform=instance["platform"], chat_id=chat_id, direction="out", source="dashboard",
            text=caption, instance_id=instance_id,
            attachment_path=rel_path, attachment_name=orig_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id}

    @app.post("/api/uploads/init", dependencies=[Depends(_require_token_or_api_key)])
    def api_uploads_init(payload: dict = Body(...)):
        """Step 1 of the chunked-upload protocol — declares intent (which
        chat, how big, what filename) and gets back a session id plus the
        chunk size to use. See bot/attachments.py's create_upload_session
        docstring for why sessions live in memory rather than the DB."""
        instance_id = payload.get("instance_id")
        chat_id = payload.get("chat_id")
        filename = payload.get("filename") or "file"
        total_size = int(payload.get("total_size") or 0)
        mime = payload.get("mime")
        text = (payload.get("text") or "").strip()
        if not instance_id or not chat_id:
            raise HTTPException(status_code=400, detail="payload must include instance_id and chat_id")
        if bot_instances.get_instance(int(instance_id)) is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            session = attachments.create_upload_session(
                filename, total_size, mime, MAX_ATTACHMENT_BYTES,
                instance_id=int(instance_id), chat_id=str(chat_id), text=text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return session

    @app.put("/api/uploads/{session_id}/chunk/{index}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_uploads_chunk(session_id: str, index: int, request: Request):
        """Step 2, called once per chunk (any order, retriable) — the raw
        request body is streamed straight to disk, see attachments.write_chunk."""
        try:
            await attachments.write_chunk(session_id, index, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown or expired upload session") from exc
        return {"ok": True}

    @app.post("/api/uploads/{session_id}/complete", dependencies=[Depends(_require_token_or_api_key)])
    async def api_uploads_complete(session_id: str):
        """Step 3 — assembles the chunks (off the event loop, can be
        gigabytes), relays through the bot if it's within the platform's
        own size limit, and always stores it server-side either way so it's
        pullable from any paired device regardless of relay outcome."""
        try:
            assembled = await asyncio.get_running_loop().run_in_executor(
                None, attachments.assemble_upload, session_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown or expired upload session") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        instance_id = assembled["instance_id"]
        chat_id = assembled["chat_id"]
        rel_path = assembled["rel_path"]
        display_name = assembled["display_name"]
        mime = assembled["mime"]
        size = assembled["size"]
        text = assembled["text"]
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            (attachments.ATTACHMENTS_DIR / rel_path).unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        relayed = size <= PLATFORM_RELAY_LIMIT_BYTES
        if relayed:
            try:
                await outbox.send_file(
                    instance_id, chat_id, str(attachments.ATTACHMENTS_DIR / rel_path), display_name, text or None
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"send failed: {exc}") from exc
        msg_id = db.log_message(
            platform=instance["platform"], chat_id=chat_id, direction="out", source="dashboard",
            text=text, instance_id=instance_id,
            attachment_path=rel_path, attachment_name=display_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id, "relayed": relayed}

    @app.get("/api/chat/attachments/{message_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_chat_attachment(message_id: int):
        row = db.get_message(message_id)
        if row is None or not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="no attachment on this message")
        full_path = attachments.ATTACHMENTS_DIR / row["attachment_path"]
        if not full_path.resolve().is_relative_to(attachments.ATTACHMENTS_DIR.resolve()) or not full_path.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing")
        return FileResponse(
            full_path,
            media_type=row["attachment_mime"] or "application/octet-stream",
            filename=row["attachment_name"] or full_path.name,
        )

    @app.get("/api/chat/attachments/{message_id}/thumbnail", dependencies=[Depends(_require_token_or_api_key)])
    def api_chat_attachment_thumbnail(message_id: int):
        row = db.get_message(message_id)
        if row is None or not row["thumbnail_path"]:
            raise HTTPException(status_code=404, detail="no thumbnail for this attachment")
        full_path = attachments.THUMBS_DIR / row["thumbnail_path"]
        if not full_path.resolve().is_relative_to(attachments.THUMBS_DIR.resolve()) or not full_path.is_file():
            raise HTTPException(status_code=404, detail="thumbnail file missing")
        return FileResponse(full_path, media_type="image/jpeg")
