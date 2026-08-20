"""Slack bot platform — full working integration via slack_bolt's Socket
Mode, which needs no public URL/webhook (ideal for a local-first app).

One SlackPlatformInstance per enabled bot_instances row with
platform="slack" — bot/platform_supervisor.py owns the instance_id ->
asyncio.Task mapping and constructs one of these per instance, so multiple
Slack bots (e.g. a Claude one and a Hermes one) can run at once, each with
its own tokens/allowlist/backend routing, fully separate chat and job
history.

Setup (also walked through in the dashboard's Bots tab):
  1. api.slack.com/apps -> Create New App -> From scratch
  2. Socket Mode -> enable it -> Generate Token and Scopes, add
     connections:write -> that token is the App token (starts xapp-).
  3. OAuth & Permissions -> Bot Token Scopes: add chat:write, im:history,
     im:read (and channels:history if you want it in channels, not just
     DMs) -> Install to Workspace -> copy the Bot User OAuth Token ->
     that's the Bot token (starts xoxb-).
  4. Event Subscriptions -> Subscribe to bot events -> add message.im
     (and message.channels for channel messages).
  5. Your Slack member ID: click your profile picture -> "..." More ->
     Copy member ID -> paste into "Allowed user ID(s)".

Slack's own IDs (users, channels) are strings, not numbers, unlike
Telegram/Discord — messages.user_id stores that string as-is, but
job tracking (bot.db.jobs.user_id) is int-typed from Telegram's original
design, so Slack-originated jobs are logged under a placeholder id of 0;
the real Slack user is still on the message row itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from bot import attachments, db, push
from bot.backends.base import BackendError
from bot.commands import CmdContext, dispatch_command
from bot.router import router

logger = logging.getLogger("bot.platforms.slack")


class SlackPlatformInstance:
    def __init__(self, instance_id: int, name: str, bot_token: str, app_token: str, allowed_ids: set[str]):
        self.instance_id = instance_id
        self.name = name
        self.bot_token = bot_token
        self.app_token = app_token
        self.allowed_ids = allowed_ids
        self._handler: Optional[Any] = None
        self._app: Optional[Any] = None
        # Per-channel scratch state (project_cwd, action_type) for /project —
        # see discord_platform.py's identical use of this pattern.
        self._sessions: dict[Any, dict] = {}

    async def _reply(self, say: Any, channel: str, text: str) -> None:
        text = text or "(empty response)"
        db.log_message(
            platform="slack", chat_id=channel, direction="out", source="bot",
            text=text, instance_id=self.instance_id,
        )
        await say(text)

    def _build_app(self):
        from slack_bolt.app.async_app import AsyncApp

        app = AsyncApp(token=self.bot_token)

        @app.event("message")
        async def handle_message(event: dict, say: Any):
            subtype = event.get("subtype")
            if event.get("bot_id") or (subtype and subtype != "file_share"):
                return
            user = event.get("user")
            if not user or user not in self.allowed_ids:
                if user:
                    logger.warning(
                        "rejected slack message from unauthorized user %s on instance %r", user, self.name
                    )
                    db.log_audit(actor=user, action="unauthorized_attempt", detail=f"slack (instance {self.instance_id})")
                return
            text = event.get("text") or ""
            files = event.get("files") or []
            if not text.strip() and not files:
                return
            channel = event.get("channel")
            if files:
                import httpx
                async with httpx.AsyncClient() as client:
                    for f in files:
                        resp = await client.get(f["url_private_download"], headers={"Authorization": f"Bearer {self.bot_token}"})
                        if resp.status_code == 200:
                            rel_path, orig_name = attachments.safe_store(f.get("name", "file"), resp.content)
                            db.log_message(
                                platform="slack", chat_id=channel, user_id=user, direction="in", source="slack",
                                text="", instance_id=self.instance_id,
                                attachment_path=rel_path, attachment_name=orig_name, attachment_mime=f.get("mimetype"),
                            )
                            asyncio.create_task(push.notify_new_message(self.name, f"📎 {orig_name}"))
            if not text.strip():
                return
            db.log_message(
                platform="slack", chat_id=channel, user_id=user, direction="in", source="slack",
                text=text, instance_id=self.instance_id,
            )
            asyncio.create_task(push.notify_new_message(self.name, text))

            session = self._sessions.setdefault(channel, {})
            cmd_ctx = CmdContext(
                # jobs.user_id is int-typed (a Telegram-era column) — Slack's
                # real string user id goes on the message row already, and
                # into `actor` for audit/config-change logging; job creation
                # keeps the same 0 placeholder the plain relay path already used.
                instance_id=self.instance_id, instance_name=self.name,
                user_id=0, chat_id=channel, actor=user, session=session,
            )
            cmd_reply = await dispatch_command(text, cmd_ctx)
            if cmd_reply is not None:
                await self._reply(say, channel, cmd_reply)
                return

            try:
                result = await router.ask(
                    text, action_type=session.get("action_type", "quick_question"), user_id=0,
                    context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
                    instance_id=self.instance_id, chat_id=channel,
                )
                await self._reply(say, channel, result.text)
            except BackendError as exc:
                await self._reply(say, channel, f"Backend failed: {exc}")

        return app

    async def start(self) -> None:
        """Long-running — connects over Socket Mode and processes events
        until stop() is called or the connection drops. Registers this
        instance's sender with bot.outbox so the dashboard's Chat tab can
        send through it."""
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        from bot import outbox

        self._app = self._build_app()

        async def _send(chat_id: Any, text: str) -> None:
            await self._app.client.chat_postMessage(channel=chat_id, text=text)

        async def _send_file(chat_id: Any, file_path: str, filename: str, caption: Optional[str]) -> None:
            await self._app.client.files_upload_v2(channel=chat_id, file=file_path, filename=filename, initial_comment=caption or None)

        outbox.register(self.instance_id, _send)
        outbox.register_file_sender(self.instance_id, _send_file)
        self._handler = AsyncSocketModeHandler(self._app, self.app_token)
        try:
            await self._handler.start_async()
        finally:
            outbox.unregister(self.instance_id)
            outbox.unregister_file_sender(self.instance_id)

    async def stop(self) -> None:
        if self._handler is not None:
            await self._handler.close_async()
