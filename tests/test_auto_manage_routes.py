"""bot/dashboard/server.py's /api/auto-manage/{instance_id} routes."""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def _make_manager_instance():
    return bot_instances.create_instance(
        name="manager", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1], persona="manager",
    )


def test_get_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/auto-manage/1")
    assert resp.status_code == 401


def test_get_unknown_instance_is_404(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/auto-manage/999999", headers=_auth())
    assert resp.status_code == 404


def test_enable_then_get(temp_db, monkeypatch):
    client = _client(monkeypatch)
    iid = _make_manager_instance()
    resp = client.post(
        "/api/auto-manage/%d" % iid,
        json={"enabled": True, "chat_id": 7, "trigger": "both", "interval": "1h"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    resp2 = client.get(f"/api/auto-manage/{iid}", headers=_auth())
    assert resp2.json()["chat_id"] == 7
    assert resp2.json()["trigger"] == "both"


def test_disable(temp_db, monkeypatch):
    client = _client(monkeypatch)
    iid = _make_manager_instance()
    client.post(f"/api/auto-manage/{iid}", json={"enabled": True, "chat_id": 7}, headers=_auth())

    resp = client.post(f"/api/auto-manage/{iid}", json={"enabled": False}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_invalid_trigger_is_400(temp_db, monkeypatch):
    client = _client(monkeypatch)
    iid = _make_manager_instance()
    resp = client.post(
        "/api/auto-manage/%d" % iid, json={"enabled": True, "chat_id": 7, "trigger": "bogus"}, headers=_auth()
    )
    assert resp.status_code == 400
