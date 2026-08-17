"""Discord bot platform — full working integration via discord.py.

Setup (also walked through in the dashboard's Platforms settings):
  1. discord.com/developers/applications -> New Application
  2. Bot tab -> Reset Token (copy it), and turn on "Message Content Intent"
     under Privileged Gateway Intents — without this the bot can't read
     message text, only that a message happened.
  3. OAuth2 -> URL Generator: scope "bot", permissions "Send Messages" +
     "Read Message History" -> open the generated URL, invite it to a
     server you own.
  4. Paste the bot token into DISCORD_BOT_TOKEN.
  5. Turn on Developer Mode (User Settings -> Advanced), then right-click
     your own name anywhere -> Copy User ID -> paste into
     DISCORD_ALLOWED_USER_IDS.

Same shape as bot/handlers.py's Telegram wiring: every allowed message
becomes one bot.router.ask() call, every message either direction is
logged via bot.db.log_message(), so the dashboard's Chat view and Jobs
table don't need to know Discord exists.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from bot import db
from bot.backends.base import BackendError
from bot.router import router

logger = logging.getLogger("bot.platforms.discord")

DISCORD_MAX_LEN = 2000

_client: Optional[Any] = None


def allowed_ids() -> set[int]:
    raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("ignoring non-numeric DISCORD_ALLOWED_USER_IDS entry: %r", part)
    return ids


def is_configured() -> bool:
    return bool(os.environ.get("DISCORD_BOT_TOKEN")) and bool(allowed_ids())


async def _reply(channel: Any, text: str) -> None:
    text = text or "(empty response)"
    db.log_message(platform="discord", chat_id=channel.id, direction="out", source="bot", text=text)
    for i in range(0, len(text), DISCORD_MAX_LEN):
        await channel.send(text[i : i + DISCORD_MAX_LEN])


def _build_client():
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info("discord platform connected as %s", client.user)

    @client.event
    async def on_message(message: "discord.Message"):
        if message.author.bot:
            return
        if message.author.id not in allowed_ids():
            logger.warning("rejected discord message from unauthorized user %s (%s)", message.author.id, message.author)
            db.log_audit(actor=str(message.author.id), action="unauthorized_attempt", detail=f"discord:{message.author}")
            return
        text = message.content or ""
        if not text.strip():
            return
        db.log_message(
            platform="discord",
            chat_id=message.channel.id,
            user_id=message.author.id,
            username=str(message.author),
            direction="in",
            source="discord",
            text=text,
        )
        async with message.channel.typing():
            try:
                result = await router.ask(text, action_type="quick_question", user_id=message.author.id)
                await _reply(message.channel, result.text)
            except BackendError as exc:
                await _reply(message.channel, f"Backend failed: {exc}")

    return client


async def start() -> None:
    """Long-running — connects and processes events until stop() is called
    or the connection drops. Registers this platform's sender with
    bot.outbox so the dashboard's Chat tab can send through it too."""
    global _client
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")

    from bot import outbox

    _client = _build_client()

    async def _send(chat_id: Any, text: str) -> None:
        channel = _client.get_channel(int(chat_id)) or await _client.fetch_channel(int(chat_id))
        await channel.send(text)

    outbox.register("discord", _send)
    try:
        await _client.start(token)
    finally:
        outbox.unregister("discord")


async def stop() -> None:
    if _client is not None:
        await _client.close()
