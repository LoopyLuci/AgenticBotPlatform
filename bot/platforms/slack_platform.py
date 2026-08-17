"""Slack bot platform — full working integration via slack_bolt's Socket
Mode, which needs no public URL/webhook (ideal for a local-first app).

Setup (also walked through in the dashboard's Platforms settings):
  1. api.slack.com/apps -> Create New App -> From scratch
  2. Socket Mode -> enable it -> Generate Token and Scopes, add
     connections:write -> that token is SLACK_APP_TOKEN (starts xapp-).
  3. OAuth & Permissions -> Bot Token Scopes: add chat:write, im:history,
     im:read (and channels:history if you want it in channels, not just
     DMs) -> Install to Workspace -> copy the Bot User OAuth Token ->
     that's SLACK_BOT_TOKEN (starts xoxb-).
  4. Event Subscriptions -> Subscribe to bot events -> add message.im
     (and message.channels for channel messages).
  5. Your Slack member ID: click your profile picture -> "..." More ->
     Copy member ID -> paste into SLACK_ALLOWED_USER_IDS.

Slack's own IDs (users, channels) are strings, not numbers, unlike
Telegram/Discord — messages.user_id stores that string as-is, but
job tracking (bot.db.jobs.user_id) is int-typed from Telegram's original
design, so Slack-originated jobs are logged under a placeholder id of 0;
the real Slack user is still on the message row itself.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from bot import db
from bot.backends.base import BackendError
from bot.router import router

logger = logging.getLogger("bot.platforms.slack")

_handler: Optional[Any] = None
_app: Optional[Any] = None


def allowed_ids() -> set[str]:
    raw = os.environ.get("SLACK_ALLOWED_USER_IDS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_configured() -> bool:
    return (
        bool(os.environ.get("SLACK_BOT_TOKEN"))
        and bool(os.environ.get("SLACK_APP_TOKEN"))
        and bool(allowed_ids())
    )


async def _reply(say: Any, channel: str, text: str) -> None:
    text = text or "(empty response)"
    db.log_message(platform="slack", chat_id=channel, direction="out", source="bot", text=text)
    await say(text)


def _build_app():
    from slack_bolt.app.async_app import AsyncApp

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("message")
    async def handle_message(event: dict, say: Any):
        if event.get("bot_id") or event.get("subtype"):
            return
        user = event.get("user")
        if not user or user not in allowed_ids():
            if user:
                logger.warning("rejected slack message from unauthorized user %s", user)
                db.log_audit(actor=user, action="unauthorized_attempt", detail="slack")
            return
        text = event.get("text") or ""
        if not text.strip():
            return
        channel = event.get("channel")
        db.log_message(
            platform="slack", chat_id=channel, user_id=user, direction="in", source="slack", text=text
        )
        try:
            result = await router.ask(text, action_type="quick_question", user_id=0)
            await _reply(say, channel, result.text)
        except BackendError as exc:
            await _reply(say, channel, f"Backend failed: {exc}")

    return app


async def start() -> None:
    """Long-running — connects over Socket Mode and processes events until
    stop() is called or the connection drops. Registers this platform's
    sender with bot.outbox so the dashboard's Chat tab can send through it."""
    global _handler, _app
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from bot import outbox

    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN is not set")

    _app = _build_app()

    async def _send(chat_id: Any, text: str) -> None:
        await _app.client.chat_postMessage(channel=chat_id, text=text)

    outbox.register("slack", _send)
    _handler = AsyncSocketModeHandler(_app, app_token)
    try:
        await _handler.start_async()
    finally:
        outbox.unregister("slack")


async def stop() -> None:
    if _handler is not None:
        await _handler.close_async()
