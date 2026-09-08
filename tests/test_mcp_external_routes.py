"""bot/dashboard/server.py's /api/mcp-external routes — the dashboard-
facing management surface for external_mcp_servers (bot/agent_runtime/
mcp_client.py). connect() itself is monkeypatched so these never touch
the real `mcp` package (only in the pipeline's bundled venv — see
test_mcp_client.py's own module docstring for the same reasoning),
proving the route/DB wiring rather than a live connection.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.server import build_app


@pytest.fixture(autouse=True)
def _no_real_connect(monkeypatch):
    async def _fake_connect(name):
        return True

    async def _fake_disconnect(name):
        return None

    monkeypatch.setattr("bot.agent_runtime.mcp_client.connect", _fake_connect)
    monkeypatch.setattr("bot.agent_runtime.mcp_client.disconnect", _fake_disconnect)
    monkeypatch.setattr("bot.agent_runtime.mcp_client.connected_servers", lambda: [])


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def test_add_list_enable_disable_delete_round_trip(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post(
        "/api/mcp-external",
        json={"name": "github", "transport": "stdio", "command": "npx", "args": ["-y", "server-github"]},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "connected": True, "authorization_url": None}

    listed = client.get("/api/mcp-external", headers=_headers()).json()["servers"]
    assert len(listed) == 1
    assert listed[0]["name"] == "github"
    assert listed[0]["transport"] == "stdio"
    assert listed[0]["enabled"] is True

    resp = client.post("/api/mcp-external/github/disable", headers=_headers())
    assert resp.status_code == 200
    listed = client.get("/api/mcp-external", headers=_headers()).json()["servers"]
    assert listed[0]["enabled"] is False

    resp = client.post("/api/mcp-external/github/enable", headers=_headers())
    assert resp.status_code == 200
    listed = client.get("/api/mcp-external", headers=_headers()).json()["servers"]
    assert listed[0]["enabled"] is True

    resp = client.delete("/api/mcp-external/github", headers=_headers())
    assert resp.status_code == 200
    assert client.get("/api/mcp-external", headers=_headers()).json()["servers"] == []


def test_add_rejects_a_duplicate_name(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/mcp-external", json={"name": "github", "transport": "stdio", "command": "npx"}, headers=_headers())

    resp = client.post("/api/mcp-external", json={"name": "github", "transport": "stdio", "command": "npx"}, headers=_headers())

    assert resp.status_code == 400


def test_add_rejects_an_unknown_transport(temp_db, monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/api/mcp-external", json={"name": "x", "transport": "carrier-pigeon"}, headers=_headers())

    assert resp.status_code == 400


def test_enable_disable_delete_404_on_unknown_name(temp_db, monkeypatch):
    client = _client(monkeypatch)

    assert client.post("/api/mcp-external/nope/enable", headers=_headers()).status_code == 404
    assert client.post("/api/mcp-external/nope/disable", headers=_headers()).status_code == 404
    assert client.delete("/api/mcp-external/nope", headers=_headers()).status_code == 404


def test_remote_transport_never_exposes_the_auth_token_on_read(temp_db, monkeypatch):
    client = _client(monkeypatch)
    client.post(
        "/api/mcp-external",
        json={"name": "remote-one", "transport": "remote", "url": "https://example.com/mcp", "auth_token": "secret-token"},
        headers=_headers(),
    )

    listed = client.get("/api/mcp-external", headers=_headers()).json()["servers"]

    assert listed[0]["has_auth_token"] is True
    assert "auth_token" not in listed[0]
    assert "secret-token" not in str(listed)
