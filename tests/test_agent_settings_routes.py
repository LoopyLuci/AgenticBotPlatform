"""bot/dashboard/server.py's /api/agent-settings routes — exercised
against the real FastAPI app via TestClient, matching the precedent set
by tests/test_hotreload_route.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot import bot_instances
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def _make_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1],
    )


def test_get_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/agent-settings")
    assert resp.status_code == 401


def test_get_returns_defaults(temp_db, monkeypatch):
    client = _client(monkeypatch)
    iid = _make_instance()
    resp = client.get("/api/agent-settings", params={"instance_id": iid}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert "max_concurrent_children" in body
    assert body["worker_effort"] is None


def test_reachable_by_a_paired_device_key_not_just_the_desktop_token(temp_db, monkeypatch):
    """Widened from the original desktop-only _require_token when the
    Android app's own Automation screen was built — same tier as
    /api/bots and /api/config/set (see _identify_caller's docstring)."""
    from bot import db

    client = _client(monkeypatch)
    _key_id, plaintext = db.create_api_key("phone", permission_tier="none")

    resp = client.get("/api/agent-settings", headers={"X-Dashboard-Token": plaintext})

    assert resp.status_code == 200


def test_set_then_get_round_trips(temp_db, monkeypatch):
    client = _client(monkeypatch)
    iid = _make_instance()
    resp = client.post(
        "/api/agent-settings",
        json={"instance_id": iid, "max_concurrent_children": 4, "worker_effort": "high"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["max_concurrent_children"] == 4

    resp2 = client.get("/api/agent-settings", params={"instance_id": iid}, headers=_auth())
    assert resp2.json()["worker_effort"] == "high"


def test_set_is_admin_instance_via_dashboard_route(temp_db, monkeypatch):
    """is_admin_instance is settable through this operator-authenticated
    dashboard route (generic FIELDS passthrough) — the self-escalation
    guard lives on the agent-runtime admin_set_agent_settings tool, not
    here, since this route is never reachable from a bot's own tool loop."""
    client = _client(monkeypatch)
    iid = _make_instance()
    resp = client.post("/api/agent-settings", json={"instance_id": iid, "is_admin_instance": True}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["is_admin_instance"] == True  # noqa: E712 (sqlite may return 1, not bool)


def test_set_unknown_field_via_extra_payload_key_is_ignored(temp_db, monkeypatch):
    """Only known FIELDS pass through — an unrelated payload key must not
    reach agent_settings.set_settings() and trigger its ValueError."""
    client = _client(monkeypatch)
    iid = _make_instance()
    resp = client.post(
        "/api/agent-settings", json={"instance_id": iid, "unrelated_key": "x"}, headers=_auth()
    )
    assert resp.status_code == 200
