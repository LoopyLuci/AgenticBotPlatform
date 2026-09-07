"""bot/dashboard/server.py's /api/skills* routes — exercised against the
real FastAPI app via TestClient, matching the precedent set by
tests/test_hotreload_route.py. Closes the standing gap: skills previously
had zero dashboard/HTTP exposure, only the /skills slash command and the
agent's own in-loop tools.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def test_list_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/skills")
    assert resp.status_code == 401


def test_create_then_list_a_global_skill(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post(
        "/api/skills",
        json={"name": "onboarding_notes", "description": "How to onboard", "content": "Step one...", "global_": True},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"name": "onboarding_notes", "description": "How to onboard", "global": True}

    listed = client.get("/api/skills", headers=_auth()).json()["skills"]
    assert any(s["name"] == "onboarding_notes" for s in listed)


def test_create_invalid_name_is_400(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/skills", json={"name": "bad name!", "content": "x"}, headers=_auth())
    assert resp.status_code == 400


def test_delete_a_skill(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/skills", json={"name": "temp_skill", "content": "x", "global_": True}, headers=_auth())

    resp = client.delete("/api/skills/temp_skill", headers=_auth())
    assert resp.status_code == 200

    resp2 = client.delete("/api/skills/temp_skill", headers=_auth())
    assert resp2.status_code == 404
