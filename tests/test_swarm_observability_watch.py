"""Phase 9: bot/swarm/observability.watch_dispatch — the bounded-lifetime
task hermes_gateway_backend fires (best-effort, fire-and-forget) to record
a dispatch's live top-level tool events. Must record real events when the
tap works, and must never raise when it doesn't (an old Hermes gateway
with no SSE route, a connection drop, etc.) — this is optional
observability bolted onto a dispatch, never a required part of it.
"""

from __future__ import annotations

import asyncio

from bot import db
from bot.swarm import observability


def _run(coro):
    return asyncio.run(coro)


class _FakeBackendOk:
    async def stream_tool_events(self, session_id):
        yield {"event": "tool_started", "name": "delegate_task"}
        yield {"event": "tool_completed", "name": "delegate_task"}


class _FakeBackendFails:
    async def stream_tool_events(self, session_id):
        raise ConnectionError("gateway has no SSE route on this version")
        yield  # pragma: no cover - unreachable, makes this an async generator


def test_watch_dispatch_records_events(temp_db):
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")

    _run(observability.watch_dispatch(job_id, _FakeBackendOk(), "sess-1"))

    events = db.list_job_tool_events(job_id)
    assert [e["event_type"] for e in events] == ["tool_started", "tool_completed"]


def test_watch_dispatch_never_raises_on_backend_failure(temp_db):
    job_id = db.create_job(action_type="swarm_dispatch", backend="hermes_gateway", user_id=0, prompt="p")

    # Must not raise — this is the whole point of the try/except inside
    # watch_dispatch, since this coroutine is fired without being awaited
    # by its real caller.
    _run(observability.watch_dispatch(job_id, _FakeBackendFails(), "sess-1"))

    assert db.list_job_tool_events(job_id) == []
