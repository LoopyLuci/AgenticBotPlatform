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

An ABP_SANDBOX_INSTANCE (bot/lease.py) never sends anything: its config is a
COPY of the real one, so "its" bot token is the real bot's, and a sandbox that
replies in a real chat is indistinguishable from the real thing until it
disagrees with it.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

_senders: dict[int, Callable[[Any, str], Awaitable[None]]] = {}
# A second, optional registration for platforms that support sub-chat
# addressing (Telegram forum topics) — see bot/main.py's registration.
# Falls back to the plain sender (dropping thread_id) when a platform
# hasn't registered one, so this is purely additive.
_threaded_senders: dict[int, Callable[[Any, str, Any], Awaitable[None]]] = {}


def register(instance_id: int, sender: Callable[[Any, str], Awaitable[None]]) -> None:
    _senders[instance_id] = sender


def register_threaded(instance_id: int, sender: Callable[[Any, str, Any], Awaitable[None]]) -> None:
    _threaded_senders[instance_id] = sender


def unregister(instance_id: int) -> None:
    _senders.pop(instance_id, None)
    _threaded_senders.pop(instance_id, None)


def is_ready(instance_id: int) -> bool:
    return instance_id in _senders


def available_instances() -> list[int]:
    return sorted(_senders.keys())


async def send_message(instance_id: int, chat_id: Any, text: str, thread_id: Optional[Any] = None) -> None:
    _refuse_in_sandbox()
    if thread_id is not None:
        threaded = _threaded_senders.get(instance_id)
        if threaded is not None:
            await threaded(chat_id, text, thread_id)
            return
    sender = _senders.get(instance_id)
    if sender is None:
        raise RuntimeError(
            f"bot instance {instance_id} isn't connected right now — check it's enabled and running"
        )
    await sender(chat_id, text)


def _refuse_in_sandbox() -> None:
    """The last line of defence for ABP_SANDBOX_INSTANCE: a sandbox is a COPY
    of the real .env, so its bot_instances rows carry the REAL tokens. Nothing
    stops a plugin, an agent tool or a future caller from reaching this module
    directly, and one send from a sandbox is a real message in a real chat, so
    this check lives at the one place every send goes through."""
    from bot import lease

    reason = lease.sandbox_blocked_reason()
    if reason:
        raise RuntimeError(
            f"refusing to send: {reason}. A sandboxed instance never messages anyone — "
            "use the real instance (abp_cli instance list) for that."
        )


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
    _refuse_in_sandbox()
    sender = _file_senders.get(instance_id)
    if sender is None:
        raise RuntimeError(
            f"bot instance {instance_id} isn't connected right now — check it's enabled and running"
        )
    await sender(chat_id, file_path, filename, caption)
