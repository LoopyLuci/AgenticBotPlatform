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


def test_a_paired_device_can_list_and_disable_hooks_but_creating_needs_unrestricted(temp_db, monkeypatch):
    """The Android Automation screen manages hooks with a device key, so
    listing/disabling/deleting stay open to any paired device. CREATING (and
    enabling) one runs a shell command as the server user, so it needs the
    `unrestricted` tier — before that, a phone at tier `none` could run code
    on the server (see _require_tier in bot/dashboard/server.py)."""
    from bot import db

    client = _client(monkeypatch)
    _id, low = db.create_api_key("phone", permission_tier="none")
    _id, high = db.create_api_key("trusted-phone", permission_tier="unrestricted")
    low_h, high_h = {"X-Dashboard-Token": low}, {"X-Dashboard-Token": high}
    body = {"event": "PreToolUse", "command": "echo hi"}

    assert client.post("/api/hooks", json=body, headers=low_h).status_code == 403
    hook_id = client.post("/api/hooks", json=body, headers=high_h).json()["id"]

    assert client.get("/api/hooks", headers=low_h).json()["hooks"]
    assert client.post(f"/api/hooks/{hook_id}/disable", headers=low_h).status_code == 200
    assert client.post(f"/api/hooks/{hook_id}/enable", headers=low_h).status_code == 403
    assert client.delete(f"/api/hooks/{hook_id}", headers=low_h).status_code == 200


def test_list_filters_by_event(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/hooks", json={"event": "PreToolUse", "command": "echo 1"}, headers=_headers())
    client.post("/api/hooks", json={"event": "PostToolUse", "command": "echo 2"}, headers=_headers())

    listed = client.get("/api/hooks?event=PreToolUse", headers=_headers()).json()["hooks"]

    assert len(listed) == 1
    assert listed[0]["event"] == "PreToolUse"
