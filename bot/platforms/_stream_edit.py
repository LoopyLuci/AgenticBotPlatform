"""Live reply streaming for chat channels that can edit a message they sent (Telegram, Discord, Slack).

A native-agent turn calls context["stream_notify"](StreamEvent) (see bot/backends/native_backend.py and
bot/agent_runtime/transports/base.py); StreamEditor turns those into one placeholder message ("...") that is
edited with the text so far, at most once per `interval_s` so the platform's edit rate limit is respected.
A tool shows as a trailing "tool name..." line - the NAME only, never arguments or results - and a reset
(failover) clears the text. When the turn ends, finish(final_text) replaces the placeholder with the reply
and returns True, so the caller skips its normal send (no duplicate message). Anything that goes wrong - an
edit error, or text longer than the platform's message limit - stops the streaming quietly: finish() then
removes the placeholder and returns False, and the normal final send (which already splits long replies)
takes over. Nothing in here may break the turn.

A backend that does not stream never produces an event, so no placeholder is ever posted for it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from bot.config import config

logger = logging.getLogger(__name__)

PLACEHOLDER = "…"
DEFAULT_INTERVAL_S = 1.2
MAX_TOOL_NAME = 64


def _cfg() -> object:
    try:
        return config.current.get("streaming")
    except Exception:  # noqa: BLE001
        return None


def streaming_enabled(platform: str) -> bool:
    """The `streaming:` block of config/backends.yaml, one boolean per platform; on unless set false."""
    block = _cfg()
    if isinstance(block, bool):
        return block
    if isinstance(block, dict):
        return bool(block.get(platform, True))
    return True


def _short_tool(name: object) -> str:
    return "".join(ch for ch in str(name or "")[:MAX_TOOL_NAME] if ch.isprintable())


class StreamEditor:
    def __init__(
        self,
        send_placeholder: Callable[[], Awaitable[Any]],
        edit: Callable[[str], Awaitable[Any]],
        *,
        limit: int,
        delete: Optional[Callable[[], Awaitable[Any]]] = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._send_placeholder = send_placeholder
        self._edit = edit
        self._delete = delete
        self._limit = limit
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._text = ""
        self._tools: list[str] = []
        self._shown = ""  # what the message currently says (never edit to the same text twice)
        self._last_edit = float("-inf")
        self._timer: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.started = False  # a placeholder message exists
        self._stopped = False  # editing gave up (error or too long); the normal send takes over
        self._closed = False  # finish() ran; later events (a queued turn) are ignored

    # ------------------------------------------------------------------ rendering
    def _render(self) -> str:
        text = self._text.strip()
        if self._tools:
            line = f"🔧 {', '.join(self._tools)}…"
            return f"{text}\n\n{line}" if text else line
        return text or PLACEHOLDER

    async def _do_edit(self, text: str) -> bool:
        """One edit; False (and streaming stopped) when it failed or the text no longer fits."""
        if self._stopped:
            return False
        if len(text) > self._limit:
            self._stopped = True
            return False
        if text == self._shown:
            return True
        try:
            await self._edit(text)
        except Exception as exc:  # noqa: BLE001 - rate limit, deleted message, no permission: just stop streaming
            logger.info("channel streaming stopped: %s", exc)
            self._stopped = True
            return False
        self._shown = text
        self._last_edit = self._clock()
        return True

    # --------------------------------------------------------------------- timing
    async def _timed_edit(self, delay: float) -> None:
        await self._sleep(delay)
        async with self._lock:
            self._timer = None
            if not self._closed:
                await self._do_edit(self._render())

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _flush_or_schedule(self) -> None:
        """Edit now if the interval has passed, else make sure one edit is queued for when it has."""
        if self._stopped or self._closed:
            return
        wait = self._interval - (self._clock() - self._last_edit)
        if wait <= 0:
            self._cancel_timer()
            await self._do_edit(self._render())
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().create_task(self._timed_edit(wait))

    # ------------------------------------------------------------------- the feed
    async def notify(self, event) -> None:
        """The stream_notify callable: takes a transport StreamEvent (kind text/reset/tool_start/tool_end)."""
        try:
            kind = getattr(event, "kind", "")
            text = getattr(event, "text", "") or ""
            async with self._lock:
                if self._closed or self._stopped:
                    return
                if kind == "text":
                    if not text:
                        return
                    self._text += text
                elif kind == "reset":
                    self._text = ""
                    self._tools = []
                elif kind == "tool_start":
                    self._tools.append(_short_tool(text))
                elif kind == "tool_end":
                    name = _short_tool(text)
                    if name in self._tools:
                        self._tools.remove(name)
                else:
                    return
                if not self.started:
                    if kind in ("reset", "tool_end"):
                        return  # nothing shown yet, nothing to clear
                    try:
                        await self._send_placeholder()
                    except Exception as exc:  # noqa: BLE001
                        logger.info("channel streaming not started: %s", exc)
                        self._stopped = True
                        return
                    self.started = True
                    self._shown = PLACEHOLDER
                    self._last_edit = self._clock()
                # A tool starting or ending is shown at once when the interval allows, like text.
                await self._flush_or_schedule()
        except Exception:  # noqa: BLE001
            logger.exception("channel streaming notify failed")

    async def _remove_placeholder(self) -> None:
        if self._delete is None:
            return
        try:
            await self._delete()
        except Exception as exc:  # noqa: BLE001
            logger.info("could not remove the streaming placeholder: %s", exc)

    async def finish(self, final_text: str) -> bool:
        """The turn is over. True: the placeholder now holds `final_text` and nothing more should be sent.
        False: the caller sends `final_text` the normal way (the placeholder, if any, has been removed)."""
        try:
            async with self._lock:
                self._cancel_timer()
                self._closed = True
                if not self.started:
                    return False
                if not self._stopped and await self._do_edit(final_text or "(empty response)"):
                    return True
                await self._remove_placeholder()
                return False
        except Exception:  # noqa: BLE001
            logger.exception("channel streaming finish failed")
            return False

    async def abort(self) -> None:
        """The turn raised: drop the placeholder (the caller's own error reply is sent normally)."""
        try:
            async with self._lock:
                self._cancel_timer()
                self._closed = True
                if self.started:
                    await self._remove_placeholder()
        except Exception:  # noqa: BLE001
            logger.exception("channel streaming abort failed")
