"""/api/agent/permissions, /api/instances/{id}/permissions, /api/mcp/pins, /api/agent/taint."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bot import bot_instances
from bot.agent_runtime import mcp_client, permissions, taint
from bot.dashboard.server import build_app

TOKEN = "test-token"
H = {"X-Dashboard-Token": TOKEN}


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    taint.forget_all()
    return TestClient(build_app())


@pytest.fixture
def instance(temp_db):
    return bot_instances.create_instance(
        name="p", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])


@pytest.mark.parametrize("method,path", [
    ("get", "/api/agent/permissions"), ("get", "/api/instances/1/permissions"), ("put", "/api/instances/1/permissions"),
    ("get", "/api/mcp/pins"), ("post", "/api/mcp/pins/approve"), ("get", "/api/agent/taint?session=x"),
    ("post", "/api/agent/taint/clear"), ("post", "/api/agent/permissions/validate"),
])
def test_every_route_requires_authentication(client, method, path):
    assert getattr(client, method)(path).status_code in (401, 422) and getattr(client, method)(path).status_code != 200
    assert getattr(client, method)(path, headers={"X-Dashboard-Token": "wrong"}).status_code == 401


def test_host_permissions_and_rule_validation(client, monkeypatch):
    monkeypatch.setattr(permissions, "_config", lambda: {"mode": "plan", "locked": True, "rules": [
        {"decision": "deny", "tool": "run_shell", "match": "rm *"}, {"decision": "bad", "tool": "x"}]})
    body = client.get("/api/agent/permissions", headers=H).json()
    assert body["mode"] == "plan" and body["locked"] is True and len(body["rules"]) == 1 and len(body["problems"]) == 1
    ok = client.post("/api/agent/permissions/validate", headers=H, json={"rules": [{"decision": "allow", "tool": "grep"}]})
    assert ok.json() == {"ok": True, "problems": []}
    bad = client.post("/api/agent/permissions/validate", headers=H, json={"rules": [{"decision": "nah", "tool": "x"}]}).json()
    assert bad["ok"] is False and "rule 1" in bad["problems"][0]


def test_instance_permissions_round_trip(client, instance):
    r = client.put(f"/api/instances/{instance}/permissions", headers=H, json={
        "mode": "accept_edits", "rules": [{"decision": "allow", "tool": "run_shell", "match": "git status*"}]})
    assert r.status_code == 200 and r.json()["instance"]["mode"] == "accept_edits"
    got = client.get(f"/api/instances/{instance}/permissions", headers=H).json()
    assert got["effective"]["mode"] == "accept_edits" and got["effective"]["rules"][0]["match"] == "git status*"
    assert got["locked"] is False and "plan" in got["modes"]


def test_instance_permission_errors(client, instance, monkeypatch):
    assert client.get("/api/instances/9999/permissions", headers=H).status_code == 404
    assert client.put("/api/instances/9999/permissions", headers=H, json={"mode": "plan"}).status_code == 404
    assert client.put(f"/api/instances/{instance}/permissions", headers=H, json={"mode": "yolo"}).status_code == 400
    assert client.put(f"/api/instances/{instance}/permissions", headers=H, json={"rules": [{"decision": "x", "tool": "y"}]}).status_code == 400
    monkeypatch.setattr(permissions, "_config", lambda: {"locked": True})
    locked = client.put(f"/api/instances/{instance}/permissions", headers=H, json={"mode": "plan"})
    assert locked.status_code == 409 and locked.json()["detail"] == "permissions_locked"


def test_pins_can_be_listed_and_approved(client, monkeypatch):
    tool = {"name": "lookup", "description": "d", "input_schema": {}}
    monkeypatch.setattr(mcp_client, "_connections", {"acme": SimpleNamespace(tools=[tool])})
    mcp_client._rebuild_tool_index()
    monkeypatch.setattr(mcp_client, "_connections", {"acme": SimpleNamespace(tools=[{**tool, "description": "changed"}])})
    mcp_client._rebuild_tool_index()
    rows = client.get("/api/mcp/pins", headers=H).json()["tools"]
    assert rows == [{"server": "acme", "tool": "lookup", "status": "changed", "description": "changed"}]
    assert client.post("/api/mcp/pins/approve", headers=H, json={"server": "acme", "tool": "lookup"}).status_code == 200
    assert client.get("/api/mcp/pins", headers=H).json()["tools"][0]["status"] == "ok"
    assert client.post("/api/mcp/pins/approve", headers=H, json={"server": "acme", "tool": "nope"}).status_code == 404
    mcp_client._tool_index.clear()
    mcp_client._blocked.clear()


def test_taint_can_be_read_and_cleared_by_a_person(client):
    taint.mark("s1", "web_fetch")
    assert client.get("/api/agent/taint?session=s1", headers=H).json() == {"session": "s1", "tainted": True, "sources": ["web_fetch"]}
    assert client.get("/api/agent/taint?session=other", headers=H).json()["tainted"] is False
    assert client.post("/api/agent/taint/clear", headers=H, json={"session": "s1"}).json()["cleared"] is True
    assert client.get("/api/agent/taint?session=s1", headers=H).json()["tainted"] is False
    assert client.post("/api/agent/taint/clear", headers=H, json={"session": "s1"}).json()["cleared"] is False
