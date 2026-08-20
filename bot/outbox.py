"""Lets the dashboard push a message into any connected bot instance.

Each running bot instance (bot/platform_supervisor.py starts one per
enabled row in bot_instances) registers its own async send function here
once it's actually connected — Telegram right after Application.build(),
Discord/Slack once their client has logged in. The dashboard's chat-send
endpoint then just calls send_message(instance_id, chat_id, text) without
needing to know discord.py from slack_bolt from python-telegram-bot; that
coupling lives in exactly one place per platform, at registration time.

Keyed by instance_id (not platform name) so two bots on the same platform
— a Claude bot and a Hermes bot both on Telegram, say — each get their own
slot instead of the second registration silently overwriting the first.
Instance ids are already globally unique across platforms (one
bot_instances table, one autoincrement PK), so no composite key is needed.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

_senders: dict[int, Callable[[Any, str], Awaitable[None]]] = {}


def register(instance_id: int, sender: Callable[[Any, str], Awaitable[None]]) -> None:
    _senders[instance_id] = sender


def unregister(instance_id: int) -> None:
    _senders.pop(instance_id, None)


def is_ready(instance_id: int) -> bool:
    return instance_id in _senders


def available_instances() -> list[int]:
    return sorted(_senders.keys())


async def send_message(instance_id: int, chat_id: Any, text: str) -> None:
    sender = _senders.get(instance_id)
    if sender is None:
        raise RuntimeError(
            f"bot instance {instance_id} isn't connected right now — check it's enabled and running"
        )
    await sender(chat_id, text)


# A separate registry (not a wider signature on _senders) since the two
# operations — chunked text vs. building a platform File/attachment
# object — are never invoked together and most sends are still text-only.
_file_senders: dict[int, Callable[[Any, str, str, Optional[str]], Awaitable[None]]] = {}


def register_file_sender(instance_id: int, sender: Callable[[Any, str, str, Optional[str]], Awaitable[None]]) -> None:
    _file_senders[instance_id] = sender


def unregister_file_sender(instance_id: int) -> None:
    _file_senders.pop(instance_id, None)


def file_send_is_ready(instance_id: int) -> bool:
    return instance_id in _file_senders


async def send_file(instance_id: int, chat_id: Any, file_path: str, filename: str, caption: Optional[str] = None) -> None:
    sender = _file_senders.get(instance_id)
    if sender is None:
        raise RuntimeError(
            f"bot instance {instance_id} isn't connected right now — check it's enabled and running"
        )
    await sender(chat_id, file_path, filename, caption)
