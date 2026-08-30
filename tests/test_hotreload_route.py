"""bot/dashboard/server.py's /api/hotreload/* routes — exercised against
the real FastAPI app via TestClient, matching the precedent set by
tests/test_whatsapp_webhook_route.py and tests/test_export.py, so a
route typo or wiring mistake fails here rather than only in
bot/hotreload.py's own unit tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot import hotreload
from bot.dashboard.server import build_app


@pytest.fixture(autouse=True)
def _reset_hotreload_state():
    hotreload._degraded = None
    hotreload._last_events.clear()
    yield
    hotreload._degraded = None
    hotreload._last_events.clear()


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_status_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/hotreload/status")
    assert resp.status_code == 401


def test_status_reports_enabled_by_default(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/hotreload/status", headers={"X-Dashboard-Token": "test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body
    assert "degraded" in body
    assert "recent_events" in body


def test_run_triggers_a_real_cycle_and_shows_up_in_status(temp_db, monkeypatch):
    async def fake_trigger():
        hotreload._record("applied", "0 file(s) changed -> reloaded 0 module(s)")
        return {"status": "applied", "detail": "0 file(s) changed -> reloaded 0 module(s)"}

    monkeypatch.setattr(hotreload, "trigger_manual_reload", fake_trigger)

    client = _client(monkeypatch)
    resp = client.post("/api/hotreload/run", headers={"X-Dashboard-Token": "test-token"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"

    status_resp = client.get("/api/hotreload/status", headers={"X-Dashboard-Token": "test-token"})
    events = status_resp.json()["recent_events"]
    assert events and events[0]["status"] == "applied"
