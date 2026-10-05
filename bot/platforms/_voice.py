"""Voice on the chat-platform adapters that are not Telegram (roadmap P7): the
Discord and Slack half of the one speech pipeline.

Nothing here chooses an engine, a limit or a set of words — those live in
bot/voice.py, which Telegram's own handler (bot/handlers.py's on_voice) uses
unchanged. This module is only the plumbing between a platform's audio
attachment and those two functions:

  fetch_audio()      the attachment's bytes, over HTTP with the bot's own
                     credentials and a ceiling, so a huge upload is never read
                     into memory whole
  over_limit()       the same max_seconds ceiling Telegram applies, checked
                     against the size the platform reported before any download
  transcribe_audio() bot/voice.py's transcribe, with the attachment's real name
                     so a command engine gets the right suffix
  speak()            bot/voice.py's synthesize, as (bytes, file name) to upload

A voice attachment is handled as the user's text exactly as a typed message
would be — same allow-list, same command dispatch, same backend. Audio is only
ever sent to an engine the person configured themselves (bot/voice.py raises
before making a call when none is).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from bot import voice

logger = logging.getLogger("bot.platforms.voice")


def over_limit(size: Optional[int]) -> bool:
    """Whether the platform's own idea of the attachment's size is over
    `voice.max_seconds`. Checked before downloading, as Telegram does with
    telegram's file_size, so an over-long recording is never fetched."""
    return int(size or 0) > voice.max_bytes()


async def fetch_audio(client: Any, url: str, headers: dict, *, limit: Optional[int] = None) -> Optional[bytes]:
    """An attachment's bytes, streamed with a size ceiling (a huge upload must
    not be read whole into memory). `limit` defaults to `voice.max_bytes()`, so
    audio is never fetched beyond what the speech engines would accept anyway.
    None if unavailable or too large."""
    if not url:
        return None
    cap = voice.max_bytes() if limit is None else limit
    # Discord's attachment urls are signed and Slack's carry the file's identity in the query, so only
    # the path is ever logged.
    where = url.split("?", 1)[0]
    try:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                logger.warning("attachment %s answered %s", where, resp.status_code)
                return None
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    logger.warning("attachment %s is over %d bytes; skipped", where, cap)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception:  # noqa: BLE001 — one unreadable file must not drop the message
        logger.warning("could not download attachment %s", where, exc_info=True)
        return None


async def transcribe_audio(data: bytes, *, filename: str = "", mimetype: str = "") -> str:
    """`data` -> transcript, through bot/voice.py. Raises voice.VoiceError for
    every problem, which the caller reports in the shared wording."""
    import mimetypes

    name = voice.audio_name(filename, mimetype)
    kind = str(mimetype or "").split(";")[0].strip() or (mimetypes.guess_type(name)[0] or "application/octet-stream")
    return await voice.transcribe(data, kind, filename=name)


async def speak(text: str) -> tuple[bytes, str]:
    """A reply as (audio bytes, file name to upload it under), through
    bot/voice.py's text-to-speech — the same bytes Telegram would speak."""
    audio, _mime, ext = await voice.synthesize(text)
    return audio, f"reply.{ext}"
