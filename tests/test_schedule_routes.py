"""bot/dashboard/server.py's /api/bots/{id}/schedules* routes — new HTTP
surface over bot/scheduler.py's existing recurring-prompt store, which
previously had no route at all (only reachable via /cron in chat).
Exercised against the real FastAPI app via TestClient, matching the
precedent in tests/test_hotreload_route.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import bot_instances
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _create_instance():
    return bot_instances.create_instance(
        name="sched-bot", platform="telegram", backend="cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


def test_list_schedules_empty_by_default(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_instance()
    resp = client.get(f"/api/bots/{instance_id}/schedules", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_list_pause_resume_delete_schedule(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_instance()

    create_resp = client.post(
        f"/api/bots/{instance_id}/schedules",
        headers=_headers(),
        json={"chat_id": "42", "kind": "cron", "prompt": "say hi", "interval": "10m"},
    )
    assert create_resp.status_code == 200
    sched_id = create_resp.json()["id"]

    list_resp = client.get(f"/api/bots/{instance_id}/schedules", headers=_headers())
    rows = list_resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == sched_id
    assert rows[0]["prompt"] == "say hi"
    assert rows[0]["interval_s"] == 600
    assert rows[0]["enabled"]

    pause_resp = client.post(f"/api/bots/{instance_id}/schedules/{sched_id}/pause", headers=_headers())
    assert pause_resp.status_code == 200
    assert not client.get(f"/api/bots/{instance_id}/schedules", headers=_headers()).json()[0]["enabled"]

    resume_resp = client.post(f"/api/bots/{instance_id}/schedules/{sched_id}/resume", headers=_headers())
    assert resume_resp.status_code == 200
    assert client.get(f"/api/bots/{instance_id}/schedules", headers=_headers()).json()[0]["enabled"]

    delete_resp = client.delete(f"/api/bots/{instance_id}/schedules/{sched_id}", headers=_headers())
    assert delete_resp.status_code == 200
    assert client.get(f"/api/bots/{instance_id}/schedules", headers=_headers()).json() == []


def test_create_schedule_bad_interval_400s(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_instance()
    resp = client.post(
        f"/api/bots/{instance_id}/schedules",
        headers=_headers(),
        json={"chat_id": "42", "kind": "cron", "prompt": "say hi", "interval": "not-a-duration"},
    )
    assert resp.status_code == 400


def test_schedules_route_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    instance_id = _create_instance()
    resp = client.get(f"/api/bots/{instance_id}/schedules")
    assert resp.status_code == 401
