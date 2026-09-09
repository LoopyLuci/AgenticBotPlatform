"""bot/dashboard/server.py's /api/hooks routes — mirrors
test_mcp_external_routes.py's own shape for the equivalent
/api/mcp-external routes.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def test_add_list_enable_disable_delete_round_trip(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post(
        "/api/hooks", json={"event": "PreToolUse", "matcher": "run_shell", "command": "echo hi"}, headers=_headers(),
    )
    assert resp.status_code == 200
    hook_id = resp.json()["id"]

    listed = client.get("/api/hooks", headers=_headers()).json()["hooks"]
    assert len(listed) == 1
    assert listed[0]["event"] == "PreToolUse"
    assert listed[0]["enabled"] is True

    resp = client.post(f"/api/hooks/{hook_id}/disable", headers=_headers())
    assert resp.status_code == 200
    listed = client.get("/api/hooks", headers=_headers()).json()["hooks"]
    assert listed[0]["enabled"] is False

    resp = client.post(f"/api/hooks/{hook_id}/enable", headers=_headers())
    assert resp.status_code == 200
    listed = client.get("/api/hooks", headers=_headers()).json()["hooks"]
    assert listed[0]["enabled"] is True

    resp = client.delete(f"/api/hooks/{hook_id}", headers=_headers())
    assert resp.status_code == 200
    assert client.get("/api/hooks", headers=_headers()).json()["hooks"] == []


def test_add_rejects_an_unknown_event(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/api/hooks", json={"event": "NotAnEvent", "command": "echo hi"}, headers=_headers())

    assert resp.status_code == 400


def test_add_rejects_an_empty_command(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/api/hooks", json={"event": "PreToolUse", "command": ""}, headers=_headers())

    assert resp.status_code == 400


def test_enable_disable_delete_404_on_unknown_id(temp_db, monkeypatch):
    client = _client(monkeypatch)

    assert client.post("/api/hooks/999/enable", headers=_headers()).status_code == 404
    assert client.post("/api/hooks/999/disable", headers=_headers()).status_code == 404
    assert client.delete("/api/hooks/999", headers=_headers()).status_code == 404


def test_reachable_by_a_paired_device_key_not_just_the_desktop_token(temp_db, monkeypatch):
    """Widened from the original desktop-only _require_token when the
    Android app's own Automation screen was built — same tier as
    /api/bots and /api/config/set (see _identify_caller's docstring)."""
    from bot import db

    client = _client(monkeypatch)
    _key_id, plaintext = db.create_api_key("phone", permission_tier="none")

    resp = client.post(
        "/api/hooks", json={"event": "PreToolUse", "command": "echo hi"}, headers={"X-Dashboard-Token": plaintext},
    )

    assert resp.status_code == 200
    assert client.get("/api/hooks", headers={"X-Dashboard-Token": plaintext}).json()["hooks"]


def test_list_filters_by_event(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/hooks", json={"event": "PreToolUse", "command": "echo 1"}, headers=_headers())
    client.post("/api/hooks", json={"event": "PostToolUse", "command": "echo 2"}, headers=_headers())

    listed = client.get("/api/hooks?event=PreToolUse", headers=_headers()).json()["hooks"]

    assert len(listed) == 1
    assert listed[0]["event"] == "PreToolUse"
