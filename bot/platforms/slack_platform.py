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

An audio file shared in a conversation (a voice message, an .m4a) is this
channel's on_voice: it is downloaded from url_private with this bot's own
token, capped, transcribed and then handled exactly as if it had been typed —
same allow-list (checked below), same slash commands, same backend. The reply
says what was heard and, with `voice.reply_with_voice: true`, is also uploaded
as a file. It is the same speech pipeline Telegram uses (bot/voice.py, off the
same `voice:` block), not a second one; audio only ever reaches a
speech-to-text engine the person configured themselves.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any, Optional

from bot import attachments, db, push, voice
from bot.backends.base import BackendError
from bot.commands import CmdContext, dispatch_command
from bot.platforms import _voice
from bot.router import router
from bot import tasks as bg

logger = logging.getLogger("bot.platforms.slack")


MAX_FILE_BYTES = 25 * 1024 * 1024  # same ceiling as the dashboard's platform relay


async def _download_capped(client: Any, url: str, token: str) -> Optional[bytes]:
    """A shared file's bytes, streamed with a size ceiling (a huge upload must
    not be read whole into memory); None if unavailable or too large."""
    return await _voice.fetch_audio(client, url, {"Authorization": f"Bearer {token}"}, limit=MAX_FILE_BYTES)


def _audio_name(f: dict) -> str:
    """A Slack file object's name, with its bare filetype ("m4a") as the suffix when the name has none."""
    name = str(f.get("name") or "")
    if not Path(name).suffix and f.get("filetype"):
        name = f"{name}.{f['filetype']}"
    return name


def _audio_file(files: list[dict]) -> Optional[dict]:
    """The first shared file that is speech. A Slack file object carries a mimetype, a name and a
    filetype, and any one of them can be missing — a voice message often arrives with no mimetype at
    all — so all three are offered to the shared detector."""
    return next((f for f in files if voice.is_audio(_audio_name(f), str(f.get("mimetype") or ""))), None)


def _private_url(f: dict) -> str:
    """Slack's own download link, fetched with the bot token. url_private_download is preferred but
    only answers once the file has been fetched through files.info at least once, so the plain
    private link is the fallback rather than a silent no-answer."""
    return str(f.get("url_private_download") or f.get("url_private") or "")


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

    async def _ask(self, event: dict, say: Any, channel: str, user: str, text: str) -> Optional[str]:
        """One message through slash-command dispatch and then the router. Returns the backend's answer
        when there was one — so a voice turn can speak it back, see _speak — and None when the reply was
        a command's own or an error report, which are the whole answer already sent."""
        db.log_message(
            platform="slack", chat_id=channel, user_id=user, direction="in", source="slack",
            text=text, instance_id=self.instance_id,
        )
        bg.spawn(push.notify_new_message(self.name, text))

        session = self._sessions.setdefault(channel, {})
        cmd_ctx = CmdContext(
            # jobs.user_id is int-typed (a Telegram-era column) — Slack's
            # real string user id goes on the message row already, and
            # into `actor` for audit/config-change logging; job creation
            # keeps the same 0 placeholder the plain relay path already used.
            instance_id=self.instance_id, instance_name=self.name,
            user_id=0, chat_id=channel, actor=user, session=session,
            enforce_access=True, scope="dm" if event.get("channel_type") == "im" else "group",
        )
        cmd_reply = await dispatch_command(text, cmd_ctx)
        if cmd_reply is not None:
            await self._reply(say, channel, cmd_reply)
            return None

        try:
            result = await router.ask(
                text, action_type=session.get("action_type", "quick_question"), user_id=0,
                context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
                instance_id=self.instance_id, chat_id=channel,
            )
            await self._reply(say, channel, result.text)
            return result.text
        except BackendError as exc:
            await self._reply(say, channel, f"Backend failed: {exc}")
        except Exception:  # noqa: BLE001 — never leave the user without a reply
            logger.exception("unexpected error answering a message")
            await self._reply(say, channel, "Something went wrong on my side — it has been logged. Please try again.")
        return None

    async def _speak(self, client: Any, channel: str, reply: str) -> None:
        """The answer uploaded as a file, for someone who spoke to the bot (bot/voice.py — the same
        text-to-speech Telegram speaks). The text reply has already gone out, so a failure here is
        logged and nothing more."""
        try:
            audio, filename = await _voice.speak(reply)
            await client.files_upload_v2(channel=channel, file=io.BytesIO(audio), filename=filename)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not send a spoken reply: %s", exc)

    async def _on_voice(self, event: dict, say: Any, client: Any, channel: str, user: str, f: dict) -> None:
        """A voice message or audio file shared in the conversation: fetched from Slack with this bot's
        own token, capped by voice.max_seconds, transcribed with the configured engine and then handled
        as the user's text. Mirrors bot/handlers.py's on_voice, word for word."""
        import httpx

        if not voice.stt_enabled():
            await self._reply(say, channel, voice.NO_STT)
            return
        if _voice.over_limit(f.get("size")):
            await self._reply(say, channel, voice.TOO_LONG)
            return
        name = _audio_name(f)
        async with httpx.AsyncClient(timeout=60) as http:
            data = await _voice.fetch_audio(http, _private_url(f), {"Authorization": f"Bearer {self.bot_token}"})
        if not data:
            await self._reply(say, channel, voice.NO_DOWNLOAD)
            return
        try:
            heard = await _voice.transcribe_audio(data, filename=name, mimetype=str(f.get("mimetype") or ""))
        except voice.VoiceError as exc:
            await self._reply(say, channel, voice.not_transcribed(exc))
            return
        db.log_audit(actor=user, action="voice_message", detail=f"{len(data)} bytes, {len(heard)} characters heard")
        await self._reply(say, channel, voice.heard(heard))
        reply = await self._ask(event, say, channel, user, heard)
        if reply and voice.reply_with_voice():
            await self._speak(client or self._app.client, channel, reply)

    def _build_app(self):
        from slack_bolt.app.async_app import AsyncApp

        app = AsyncApp(token=self.bot_token)

        @app.event("message")
        async def handle_message(event: dict, say: Any, client: Any = None):
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
            # An audio file is a voice message, so it takes the transcribed path rather than the
            # generic attachment one below: Telegram ignores the caption on a voice note and so does
            # this, and only the first audio file on a message is transcribed.
            audio = _audio_file(files)
            if audio is not None:
                await self._on_voice(event, say, client, channel, user, audio)
                return
            if files:
                import httpx
                async with httpx.AsyncClient(timeout=60) as client:
                    for f in files:
                        data = await _download_capped(client, _private_url(f), self.bot_token)
                        if data is not None:
                            rel_path, orig_name = attachments.safe_store(f.get("name", "file"), data)
                            db.log_message(
                                platform="slack", chat_id=channel, user_id=user, direction="in", source="slack",
                                text="", instance_id=self.instance_id,
                                attachment_path=rel_path, attachment_name=orig_name, attachment_mime=f.get("mimetype"),
                            )
                            bg.spawn(push.notify_new_message(self.name, f"📎 {orig_name}"))
            if not text.strip():
                return
            await self._ask(event, say, channel, user, text)

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
