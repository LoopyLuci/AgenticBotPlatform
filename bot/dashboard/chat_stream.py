"""Live reply streaming for the dashboard/desktop "Chat with Bot" path.

A native-agent turn calls context["stream_notify"](StreamEvent); ChatStreamer turns those into compact
{type: "chat_stream", ...} frames for the authenticated /api/ws sockets. Text deltas are coalesced (at
most one frame per FLUSH_INTERVAL_S) so a long reply does not flood the socket. Frames carry the tool
NAME only - never arguments or results - and nothing here may break the turn.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 0.05
MAX_TOOL_NAME = 64


def _short_tool(name: object) -> str:
    return "".join(ch for ch in str(name or "")[:MAX_TOOL_NAME] if ch.isprintable())


class ChatStreamer:
    def __init__(self, send: Callable[[dict], Awaitable[None]], instance_id: int, chat_id: str, turn_id: str,
                 flush_interval_s: float = FLUSH_INTERVAL_S) -> None:
        self._send = send
        self._base = {"type": "chat_stream", "instance_id": instance_id, "chat_id": chat_id, "turn_id": turn_id}
        self._interval = flush_interval_s
        self._buf: list[str] = []
        self._timer: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def _emit(self, **fields) -> None:
        try:
            await self._send({**self._base, **fields})
        except Exception:  # noqa: BLE001 - a dead socket or broadcaster must never fail the turn
            logger.exception("chat_stream send failed")

    async def _flush_locked(self) -> None:
        if self._buf:
            text, self._buf = "".join(self._buf), []
            await self._emit(kind="text_delta", text=text)

    async def _timed_flush(self) -> None:
        await asyncio.sleep(self._interval)
        async with self._lock:
            self._timer = None
            await self._flush_locked()

    async def _cancel_timer(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def notify(self, event) -> None:
        """The stream_notify callable: takes a transport StreamEvent (kind text/reset/tool_start/tool_end)."""
        try:
            kind = getattr(event, "kind", "")
            text = getattr(event, "text", "") or ""
            async with self._lock:
                if kind == "text":
                    if text:
                        self._buf.append(text)
                        if self._timer is None:
                            self._timer = asyncio.get_running_loop().create_task(self._timed_flush())
                    return
                await self._cancel_timer()
                await self._flush_locked()
                if kind == "reset":
                    await self._emit(kind="reset")
                elif kind in ("tool_start", "tool_end"):
                    await self._emit(kind=kind, tool=_short_tool(text))
        except Exception:  # noqa: BLE001
            logger.exception("chat_stream notify failed")

    async def event(self, kind: str, **fields) -> None:
        """A frame of the caller's own kind (e.g. Local Chat's "loading" / "loaded" while the model loads), in order."""
        try:
            async with self._lock:
                await self._cancel_timer()
                await self._flush_locked()
                await self._emit(kind=kind, **fields)
        except Exception:  # noqa: BLE001
            logger.exception("chat_stream event failed")

    async def finish(self) -> None:
        """Flush whatever is pending and send the closing "done" frame (call in a finally)."""
        try:
            async with self._lock:
                await self._cancel_timer()
                await self._flush_locked()
                await self._emit(kind="done")
        except Exception:  # noqa: BLE001
            logger.exception("chat_stream finish failed")
