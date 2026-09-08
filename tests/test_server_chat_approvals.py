"""POST /api/server-chat/approvals/{id}/resolve — resolves a Server
Chat approval-request message through the exact same
bot.agent_runtime.approval state machine the Telegram admin bot's
dangerous-tool prompts use (Section 3 of the "Admin control surface"
plan).
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from bot import db
from bot.agent_runtime import approval
from bot.dashboard.server import build_app


def _run(coro):
    return asyncio.run(coro)


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _desktop_headers():
    return {"X-Dashboard-Token": "test-token"}


def test_resolve_route_resolves_a_pending_approval(temp_db, monkeypatch):
    """approval.resolve() only recognizes an approval id that's actually
    being waited on (an in-memory _waiters entry, registered by a real
    request_approval() call) — matching tests/test_plan_approval.py's
    own established pattern for exercising this same state machine."""
    client = _client(monkeypatch)

    async def scenario():
        async def notify(approval_id, tool_name, tool_input):
            pass

        task = asyncio.create_task(
            approval.request_approval(1, "chat1", "session1", "admin_engage_estop", {"reason": "x"}, notify=notify)
        )
        await asyncio.sleep(0.05)
        approval_id = next(iter(approval._waiters))

        resp = client.post(f"/api/server-chat/approvals/{approval_id}/resolve", headers=_desktop_headers(), json={"outcome": "once"})
        assert resp.status_code == 200

        outcome = await task
        assert outcome == "once"

    _run(scenario())


def test_resolve_route_rejects_an_invalid_outcome(temp_db, monkeypatch):
    client = _client(monkeypatch)
    approval_id = db.create_pending_approval(1, "chat1", "session1", "admin_engage_estop", {})

    resp = client.post(f"/api/server-chat/approvals/{approval_id}/resolve", headers=_desktop_headers(), json={"outcome": "maybe"})

    assert resp.status_code == 400


def test_resolve_route_409s_on_an_already_resolved_approval(temp_db, monkeypatch):
    client = _client(monkeypatch)

    async def scenario():
        async def notify(approval_id, tool_name, tool_input):
            pass

        task = asyncio.create_task(
            approval.request_approval(1, "chat1", "session1", "admin_engage_estop", {}, notify=notify)
        )
        await asyncio.sleep(0.05)
        approval_id = next(iter(approval._waiters))

        first = client.post(f"/api/server-chat/approvals/{approval_id}/resolve", headers=_desktop_headers(), json={"outcome": "deny"})
        assert first.status_code == 200
        await task

        second = client.post(f"/api/server-chat/approvals/{approval_id}/resolve", headers=_desktop_headers(), json={"outcome": "once"})
        assert second.status_code == 409

    _run(scenario())
