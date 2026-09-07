"""bot/dashboard/server.py's POST /api/plugins/create route — the
dashboard/MCP-facing counterpart to the create_plugin agent tool, tested
against the real FastAPI app via TestClient.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot import plugins
from bot.dashboard.server import build_app


@pytest.fixture(autouse=True)
def _reset_registry(tmp_path, monkeypatch):
    plugins._tools.clear()
    plugins._commands.clear()
    plugins._command_aliases.clear()
    plugins._loaded.clear()
    monkeypatch.setattr("bot.envfile.PROJECT_ROOT", tmp_path)
    yield
    plugins._tools.clear()
    plugins._commands.clear()
    plugins._command_aliases.clear()
    plugins._loaded.clear()


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


PLUGIN_CODE = '''
async def _handler(tool_input, *, workspace, instance_id):
    return "ok"

def setup(api):
    api.register_tool("route_authored_tool", "d", {"type": "object", "properties": {}}, _handler)
'''


def test_requires_token(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    resp = client.post("/api/plugins/create", json={"name": "authored", "code": PLUGIN_CODE})
    assert resp.status_code == 401


def test_create_writes_installs_and_activates(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch)
    resp = client.post(
        "/api/plugins/create",
        json={"name": "authored", "code": PLUGIN_CODE, "description": "test"},
        headers={"X-Dashboard-Token": "test-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "authored"
    assert body["enabled"] is True
    assert "route_authored_tool" in body["tools"]
    assert (tmp_path / "data" / "plugins" / "authored" / "authored.py").is_file()


def test_missing_name_or_code_is_400(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/plugins/create", json={"name": "", "code": "x"}, headers={"X-Dashboard-Token": "test-token"})
    assert resp.status_code == 400
