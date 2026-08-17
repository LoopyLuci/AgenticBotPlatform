"""Lets the dashboard push a message into any connected platform.

Each platform module (Telegram's wiring in bot/main.py, bot/platforms/*)
registers its own async send function here once it's actually connected —
Telegram right after Application.build(), Discord/Slack once their client
has logged in. The dashboard's chat-send endpoint then just calls
send_message(platform, chat_id, text) without needing to know discord.py
from slack_bolt from python-telegram-bot; that coupling lives in exactly
one place per platform, at registration time.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

_senders: dict[str, Callable[[Any, str], Awaitable[None]]] = {}


def register(platform: str, sender: Callable[[Any, str], Awaitable[None]]) -> None:
    _senders[platform] = sender


def unregister(platform: str) -> None:
    _senders.pop(platform, None)


def is_ready(platform: str) -> bool:
    return platform in _senders


def available_platforms() -> list[str]:
    return sorted(_senders.keys())


async def send_message(platform: str, chat_id: Any, text: str) -> None:
    sender = _senders.get(platform)
    if sender is None:
        raise RuntimeError(
            f"{platform!r} isn't connected right now — configure it and restart the server"
        )
    await sender(chat_id, text)
