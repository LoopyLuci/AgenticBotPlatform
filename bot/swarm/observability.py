"""Live top-level tool-event tap for a dispatch_swarm_goal job — the
"real-time" half of Phase 9's hybrid swarm-observability design.

What this can and can't see, stated plainly: it consumes the Hermes
gateway's SSE `/api/sessions/{id}/chat/stream` endpoint
(HermesGatewayBackend.stream_tool_events), which the installed Hermes
Agent source only feeds from its own top-level tool_started/
tool_completed hooks — a single (event, whole-batch) pair per
delegate_task call. The gateway's own per-child progress callback
(`_on_tool_progress` in its gateway/platforms/api_server.py) is a
confirmed no-op stub that never reaches this stream, so per-child detail
is NOT available here — see bot/swarm/child_parser.py for how that half
of the picture is recovered instead (parsed from the dispatch's own
final reply, post-hoc).

This module exists purely to answer "is the dispatch still doing
something" in real time while it runs. It is bolted onto
HermesGatewayBackend.ask() as a best-effort, fire-and-forget task
(see that class's _maybe_start_observability) and must never affect
whether a dispatch itself succeeds.
"""

from __future__ import annotations

import logging
from typing import Any

from bot import db

logger = logging.getLogger("bot.swarm.observability")

# Safety cap so a stream that never closes (a stuck gateway, a network
# proxy holding the connection open) can't leak a task forever — bounded
# independently of whatever timeout the actual dispatch call is using.
_MAX_WATCH_SECONDS = 1800


async def watch_dispatch(job_id: int, backend: Any, session_id: str) -> None:
    """Consumes backend.stream_tool_events(session_id) for at most
    _MAX_WATCH_SECONDS, recording each delegate_task tool_started/
    tool_completed event against job_id. Any failure (old Hermes version
    with no SSE route, connection refused, stream error) is logged once
    at info level and this simply returns — it is never awaited by the
    caller, so there is nothing for a failure here to propagate into."""
    import asyncio

    try:
        async with asyncio.timeout(_MAX_WATCH_SECONDS):
            async for event in backend.stream_tool_events(session_id):
                event_type = event.get("event") or event.get("type") or "tool_event"
                tool_name = event.get("name", "delegate_task")
                db.log_job_tool_event(job_id, event_type, tool_name, event)
    except TimeoutError:
        logger.info("swarm observability tap for job %s hit its %ss safety cap", job_id, _MAX_WATCH_SECONDS)
    except Exception:
        # Confirmed during design: the installed Hermes gateway's own
        # per-child progress hook is a no-op stub and the SSE route may
        # not exist at all on older gateway versions — either shows up
        # here as a connection/parse failure. This is optional
        # observability, not a required part of the dispatch, so it's
        # logged and dropped, never re-raised.
        logger.info("swarm observability tap for job %s ended: unavailable or failed", job_id, exc_info=True)
