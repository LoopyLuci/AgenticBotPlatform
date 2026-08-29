"""Matrix bot platform — full working integration via matrix-nio.

One MatrixPlatformInstance per enabled bot_instances row with
platform="matrix" — bot/platform_supervisor.py owns the instance_id ->
asyncio.Task mapping and constructs one of these per instance, the same
shape as Discord/Slack/Telegram.

Setup (also walked through in the dashboard's Bots tab):
  1. Register a dedicated account for the bot on your homeserver (its own
     Matrix user, not your personal one) — e.g. via Element's sign-up
     flow, or `POST /_matrix/client/v3/register` if your homeserver
     allows it.
  2. Get that account's access token: Element -> Settings -> Help & About
     -> Advanced -> Access Token, or `POST /_matrix/client/v3/login`
     with `{"type": "m.login.password", "identifier": {"type":
     "m.id.user", "user": "<localpart>"}, "password": "..."}`.
  3. Paste the homeserver URL (e.g. https://matrix.org), the bot's full
     user ID (e.g. @mybot:matrix.org), and the access token into the
     Bots tab.
  4. Invite the bot's account to any room you want it in — it accepts
     every invite automatically.

Same shape as bot/platforms/discord_platform.py: every allowed message
becomes one bot.router.ask() call, every message either direction is
logged via bot.db.log_message(), so the dashboard's Chat view and Jobs
table don't need to know Matrix exists.

Scope, stated plainly: this talks to unencrypted rooms only. End-to-end
encrypted rooms need an Olm/Megolm store (matrix-nio's optional
`[e2e]` extra, which pulls in libolm) and per-device key verification —
real infrastructure this codebase doesn't have and a separate decision
to add, not a stub standing in for it. Inviting this bot into an
encrypted room will not work; use an unencrypted room instead.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Optional

from bot import attachments, db, push
from bot.backends.base import BackendError
from bot.commands import CmdContext, dispatch_command
from bot.router import router

logger = logging.getLogger("bot.platforms.matrix")

MATRIX_MAX_LEN = 32000  # Matrix events have no hard body-length limit like Telegram/Discord; this is a sane cap


class MatrixPlatformInstance:
    def __init__(
        self,
        instance_id: int,
        name: str,
        homeserver: str,
        user_id: str,
        access_token: str,
        device_id: str,
        allowed_ids: set[str],
    ):
        self.instance_id = instance_id
        self.name = name
        self.homeserver = homeserver
        self.user_id = user_id
        self.access_token = access_token
        self.device_id = device_id
        self.allowed_ids = allowed_ids
        self._client: Optional[Any] = None
        # Ignores backlog delivered by the first /sync after connecting —
        # a fresh access-token login has no "since" cursor of its own, so
        # without this every restart would re-process whatever messages
        # were sitting in each joined room's recent timeline.
        self._started_at_ms = 0
        # Per-room scratch state (project_cwd, action_type) for /project —
        # mirrors Discord's per-channel dict.
        self._sessions: dict[Any, dict] = {}

    async def _reply(self, room_id: str, text: str) -> None:
        text = text or "(empty response)"
        db.log_message(
            platform="matrix", chat_id=room_id, direction="out", source="bot",
            text=text, instance_id=self.instance_id,
        )
        for i in range(0, len(text), MATRIX_MAX_LEN):
            await self._client.room_send(
                room_id=room_id, message_type="m.room.message",
                content={"msgtype": "m.text", "body": text[i : i + MATRIX_MAX_LEN]},
            )

    async def _handle_media(self, room, event, kind: str) -> bool:
        """Downloads an incoming image/file/video/audio event's content and
        logs it the same way Discord's attachment handling does. Returns
        False (caller should ignore the event) if it's not a real
        media message (no mxc:// url — Matrix allows an empty placeholder
        body in some clients)."""
        from nio import DownloadError

        url = getattr(event, "url", "") or ""
        if not url.startswith("mxc://"):
            return False
        resp = await self._client.download(mxc=url, filename=event.body or kind)
        if isinstance(resp, DownloadError):
            logger.warning("matrix instance %r failed to download %s: %s", self.name, url, resp)
            return False
        rel_path, orig_name = attachments.safe_store(resp.filename or event.body or kind, resp.body)
        db.log_message(
            platform="matrix", chat_id=room.room_id, user_id=event.sender, username=event.sender,
            direction="in", source="matrix", text="", instance_id=self.instance_id,
            attachment_path=rel_path, attachment_name=orig_name, attachment_mime=resp.content_type,
        )
        asyncio.create_task(push.notify_new_message(self.name, f"📎 {orig_name}"))
        return True

    async def _on_invite(self, room, event) -> None:
        if event.membership != "invite" or event.state_key != self.user_id:
            return
        from nio import JoinError

        resp = await self._client.join(room.room_id)
        if isinstance(resp, JoinError):
            logger.warning("matrix instance %r failed to join %s: %s", self.name, room.room_id, resp)
        else:
            logger.info("matrix instance %r joined %s (invited by %s)", self.name, room.room_id, event.sender)

    async def _on_message(self, room, event) -> None:
        if event.sender == self.user_id:
            return
        if event.server_timestamp < self._started_at_ms:
            return  # backlog from before this instance connected
        sender = event.sender
        if sender not in self.allowed_ids:
            logger.warning("rejected matrix message from unauthorized user %s on instance %r", sender, self.name)
            db.log_audit(
                actor=sender, action="unauthorized_attempt", detail=f"matrix:{sender} (instance {self.instance_id})"
            )
            return
        text = event.body or ""
        if not text.strip():
            return
        db.log_message(
            platform="matrix", chat_id=room.room_id, user_id=sender, username=sender,
            direction="in", source="matrix", text=text, instance_id=self.instance_id,
        )
        asyncio.create_task(push.notify_new_message(self.name, text))

        session = self._sessions.setdefault(room.room_id, {})
        cmd_ctx = CmdContext(
            instance_id=self.instance_id, instance_name=self.name,
            user_id=sender, chat_id=room.room_id, actor=sender, session=session,
        )
        cmd_reply = await dispatch_command(text, cmd_ctx)
        if cmd_reply is not None:
            await self._reply(room.room_id, cmd_reply)
            return

        try:
            result = await router.ask(
                text, action_type=session.get("action_type", "quick_question"), user_id=sender,
                context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
                instance_id=self.instance_id, chat_id=room.room_id,
            )
            await self._reply(room.room_id, result.text)
        except BackendError as exc:
            await self._reply(room.room_id, f"Backend failed: {exc}")

    async def _on_media(self, room, event) -> None:
        if event.sender == self.user_id:
            return
        if event.server_timestamp < self._started_at_ms:
            return
        if event.sender not in self.allowed_ids:
            return
        await self._handle_media(room, event, "attachment")

    def _build_client(self):
        from nio import (
            AsyncClient,
            AsyncClientConfig,
            InviteMemberEvent,
            RoomMessageAudio,
            RoomMessageFile,
            RoomMessageImage,
            RoomMessageText,
            RoomMessageVideo,
        )

        config = AsyncClientConfig(store_sync_tokens=True)
        client = AsyncClient(self.homeserver, self.user_id, device_id=self.device_id or None, config=config)
        client.access_token = self.access_token
        client.user_id = self.user_id
        self._started_at_ms = int(time.time() * 1000)

        client.add_event_callback(self._on_message, RoomMessageText)
        client.add_event_callback(self._on_invite, InviteMemberEvent)
        client.add_event_callback(
            self._on_media, (RoomMessageImage, RoomMessageFile, RoomMessageAudio, RoomMessageVideo)
        )
        return client

    async def _upload_and_send(self, room_id: str, file_path: str, filename: str, caption: Optional[str]) -> None:
        from nio import UploadResponse

        data = Path(file_path).read_bytes()
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        resp, _ = await self._client.upload(data, content_type=mime, filename=filename, filesize=len(data))
        if not isinstance(resp, UploadResponse):
            raise BackendError(f"matrix upload failed: {resp}")
        msgtype = (
            "m.image" if mime.startswith("image/")
            else "m.video" if mime.startswith("video/")
            else "m.audio" if mime.startswith("audio/")
            else "m.file"
        )
        content = {
            "msgtype": msgtype, "body": filename, "url": resp.content_uri,
            "info": {"mimetype": mime, "size": len(data)},
        }
        await self._client.room_send(room_id=room_id, message_type="m.room.message", content=content)
        if caption:
            await self._reply(room_id, caption)

    async def start(self) -> None:
        """Long-running — connects and syncs until stop() is called or the
        connection drops. Registers this instance's sender with bot.outbox
        so the dashboard's Chat tab can send through it."""
        from bot import outbox

        self._client = self._build_client()

        async def _send(chat_id: Any, text: str) -> None:
            await self._reply(str(chat_id), text)

        async def _send_file(chat_id: Any, file_path: str, filename: str, caption: Optional[str]) -> None:
            await self._upload_and_send(str(chat_id), file_path, filename, caption)

        outbox.register(self.instance_id, _send)
        outbox.register_file_sender(self.instance_id, _send_file)
        try:
            logger.info("matrix instance %r connecting to %s as %s", self.name, self.homeserver, self.user_id)
            await self._client.sync_forever(timeout=30000, full_state=False)
        finally:
            outbox.unregister(self.instance_id)
            outbox.unregister_file_sender(self.instance_id)
            await self._client.close()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
