"""Discord bot platform — full working integration via discord.py.

One DiscordPlatformInstance per enabled bot_instances row with
platform="discord" — bot/platform_supervisor.py owns the instance_id ->
asyncio.Task mapping and constructs one of these per instance, so two
Discord bots (e.g. a Claude one and a Hermes one) can run at once, each
with its own token/allowlist/backend routing, fully separate chat and job
history.

Setup (also walked through in the dashboard's Bots tab):
  1. discord.com/developers/applications -> New Application
  2. Bot tab -> Reset Token (copy it), and turn on "Message Content Intent"
     under Privileged Gateway Intents — without this the bot can't read
     message text, only that a message happened.
  3. OAuth2 -> URL Generator: scope "bot", permissions "Send Messages" +
     "Read Message History" -> open the generated URL, invite it to a
     server you own.
  4. Paste the bot token into the Bots tab's "Bot token" field.
  5. Turn on Developer Mode (User Settings -> Advanced), then right-click
     your own name anywhere -> Copy User ID -> paste into "Allowed user
     ID(s)".

Same shape as bot/handlers.py's Telegram wiring: every allowed message
becomes one bot.router.ask() call, every message either direction is
logged via bot.db.log_message(), so the dashboard's Chat view and Jobs
table don't need to know Discord exists.

A voice note or audio file on an attachment is this channel's on_voice: it
is downloaded with this bot's own token, capped, transcribed and then handled
exactly as if it had been typed — same allow-list (checked above), same slash
commands, same backend. The reply says what was heard and, with
`voice.reply_with_voice: true`, is also sent back as an audio attachment. It
is the same speech pipeline Telegram uses (bot/voice.py, off the same
`voice:` block), not a second one; audio only ever reaches a speech-to-text
engine the person configured themselves.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Optional

from bot import attachments, db, push, voice
from bot.backends.base import BackendError
from bot.commands import CmdContext, dispatch_command
from bot.platforms import _voice
from bot.router import router
from bot import tasks as bg

logger = logging.getLogger("bot.platforms.discord")

DISCORD_MAX_LEN = 2000


class DiscordPlatformInstance:
    def __init__(self, instance_id: int, name: str, bot_token: str, allowed_ids: set[int]):
        self.instance_id = instance_id
        self.name = name
        self.token = bot_token
        self.allowed_ids = allowed_ids
        self._client: Optional[Any] = None
        # Per-channel scratch state (project_cwd, action_type) for /project —
        # mirrors Telegram's context.user_data, keyed by channel since Discord
        # has no built-in per-chat session dict the way python-telegram-bot does.
        self._sessions: dict[Any, dict] = {}

    async def _reply(self, channel: Any, text: str) -> None:
        text = text or "(empty response)"
        db.log_message(
            platform="discord", chat_id=channel.id, direction="out", source="bot",
            text=text, instance_id=self.instance_id,
        )
        for i in range(0, len(text), DISCORD_MAX_LEN):
            await channel.send(text[i : i + DISCORD_MAX_LEN])

    async def _ask(self, message: Any, text: str) -> Optional[str]:
        """One message through slash-command dispatch and then the router. Returns the backend's answer
        when there was one — so a voice turn can speak it back, see _speak — and None when the reply was
        a command's own or an error report, which are the whole answer already sent."""
        db.log_message(
            platform="discord",
            chat_id=message.channel.id,
            user_id=message.author.id,
            username=str(message.author),
            direction="in",
            source="discord",
            text=text,
            instance_id=self.instance_id,
        )
        bg.spawn(push.notify_new_message(self.name, text))

        session = self._sessions.setdefault(message.channel.id, {})
        cmd_ctx = CmdContext(
            instance_id=self.instance_id, instance_name=self.name,
            user_id=message.author.id, chat_id=message.channel.id,
            actor=str(message.author.id), session=session,
            enforce_access=True, scope="dm" if getattr(message, "guild", None) is None else "group",
        )
        cmd_reply = await dispatch_command(text, cmd_ctx)
        if cmd_reply is not None:
            await self._reply(message.channel, cmd_reply)
            return None

        async with message.channel.typing():
            try:
                result = await router.ask(
                    text, action_type=session.get("action_type", "quick_question"), user_id=message.author.id,
                    context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
                    instance_id=self.instance_id, chat_id=message.channel.id,
                )
                await self._reply(message.channel, result.text)
                return result.text
            except BackendError as exc:
                await self._reply(message.channel, f"Backend failed: {exc}")
            except Exception:  # noqa: BLE001 — never leave the user without a reply
                logger.exception("unexpected error answering a message")
                await self._reply(message.channel, "Something went wrong on my side — it has been logged. Please try again.")
        return None

    async def _speak(self, channel: Any, reply: str) -> None:
        """The answer as an audio attachment, for someone who spoke to the bot (bot/voice.py — the same
        text-to-speech Telegram speaks). The text reply has already gone out, so a failure here is logged
        and nothing more."""
        import discord

        try:
            audio, filename = await _voice.speak(reply)
            await channel.send(file=discord.File(io.BytesIO(audio), filename=filename))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not send a spoken reply: %s", exc)

    async def _on_voice(self, message: Any, att: Any) -> None:
        """A voice note or audio file attached to a message: fetched from Discord's CDN with this bot's
        own token, capped by voice.max_seconds, transcribed with the configured engine and then handled
        as the user's text. Mirrors bot/handlers.py's on_voice, word for word."""
        import httpx

        if not voice.stt_enabled():
            await self._reply(message.channel, voice.NO_STT)
            return
        if _voice.over_limit(att.size):
            await self._reply(message.channel, voice.TOO_LONG)
            return
        async with httpx.AsyncClient(timeout=60) as client:
            data = await _voice.fetch_audio(client, att.url, {"Authorization": f"Bot {self.token}"})
        if not data:
            await self._reply(message.channel, voice.NO_DOWNLOAD)
            return
        try:
            heard = await _voice.transcribe_audio(data, filename=att.filename, mimetype=att.content_type)
        except voice.VoiceError as exc:
            await self._reply(message.channel, voice.not_transcribed(exc))
            return
        db.log_audit(
            actor=str(message.author.id), action="voice_message",
            detail=f"{len(data)} bytes, {len(heard)} characters heard",
        )
        await self._reply(message.channel, voice.heard(heard))
        reply = await self._ask(message, heard)
        if reply and voice.reply_with_voice():
            await self._speak(message.channel, reply)

    def _build_client(self):
        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            logger.info("discord instance %r connected as %s", self.name, client.user)

        @client.event
        async def on_message(message: "discord.Message"):
            if message.author.bot:
                return
            if message.author.id not in self.allowed_ids:
                logger.warning(
                    "rejected discord message from unauthorized user %s (%s) on instance %r",
                    message.author.id, message.author, self.name,
                )
                db.log_audit(
                    actor=str(message.author.id), action="unauthorized_attempt",
                    detail=f"discord:{message.author} (instance {self.instance_id})",
                )
                return
            text = message.content or ""
            if not text.strip() and not message.attachments:
                return
            # An audio attachment is a voice message, so it takes the transcribed path below rather
            # than the generic one: Telegram ignores the caption on a voice note and so does this, and
            # only the first audio attachment on a message is transcribed.
            audio = next((a for a in message.attachments if voice.is_audio(a.filename, a.content_type)), None)
            if audio is not None:
                await self._on_voice(message, audio)
                return
            for att in message.attachments:
                data = await att.read()
                rel_path, orig_name = attachments.safe_store(att.filename, data)
                db.log_message(
                    platform="discord", chat_id=message.channel.id, user_id=message.author.id,
                    username=str(message.author), direction="in", source="discord",
                    text="", instance_id=self.instance_id,
                    attachment_path=rel_path, attachment_name=orig_name, attachment_mime=att.content_type,
                )
                bg.spawn(push.notify_new_message(self.name, f"📎 {orig_name}"))
            if not text.strip():
                return
            await self._ask(message, text)

        return client

    async def start(self) -> None:
        """Long-running — connects and processes events until stop() is
        called or the connection drops. Registers this instance's sender
        with bot.outbox so the dashboard's Chat tab can send through it."""
        import discord

        from bot import outbox

        self._client = self._build_client()

        async def _send(chat_id: Any, text: str) -> None:
            channel = self._client.get_channel(int(chat_id)) or await self._client.fetch_channel(int(chat_id))
            await channel.send(text)

        async def _send_file(chat_id: Any, file_path: str, filename: str, caption: Optional[str]) -> None:
            channel = self._client.get_channel(int(chat_id)) or await self._client.fetch_channel(int(chat_id))
            await channel.send(content=caption or None, file=discord.File(file_path, filename=filename))

        outbox.register(self.instance_id, _send)
        outbox.register_file_sender(self.instance_id, _send_file)
        try:
            await self._client.start(self.token)
        finally:
            outbox.unregister(self.instance_id)
            outbox.unregister_file_sender(self.instance_id)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
