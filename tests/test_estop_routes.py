"""bot/dashboard/server.py's /api/estop routes."""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def test_get_status_default_disengaged(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/estop", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["engaged"] is False


def test_engage_then_get(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/estop", json={"engaged": True, "reason": "testing"}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["engaged"] is True

    resp2 = client.get("/api/estop", headers=_auth())
    assert resp2.json()["reason"] == "testing"


def test_disengage(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/estop", json={"engaged": True}, headers=_auth())

    resp = client.post("/api/estop", json={"engaged": False}, headers=_auth())
    assert resp.json()["engaged"] is False


def test_post_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/estop", json={"engaged": True})
    assert resp.status_code == 401
