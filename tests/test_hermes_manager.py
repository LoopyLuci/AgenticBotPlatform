"""ABP's Hermes Manager module: finding the bridge from its discovery file, calling operations and window operations,
the agent tools' read/change split, the install locator, and the page (identical in both UIs). A stand-in bridge
answers over httpx."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bot.hermes_manager import client, harness
from bot.hermes_manager.client import ManagerError

ROOT = Path(__file__).resolve().parents[1]
API_OPS = [{"id": "gateway.status", "group": "gateway", "summary": "Gateway status", "params": {"type": "object"},
            "mutating": False},
           {"id": "gateway.lifecycle", "group": "gateway", "summary": "Start/stop", "params": {"type": "object"},
            "mutating": True}]
GUI_OPS = [{"id": "gui.sections", "summary": "Sections", "params": {}},
           {"id": "gui.click", "summary": "Click", "params": {"target": {}}, "required": ["target"], "mutating": True}]


class FakeBridge:
    def __init__(self):
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/ping":
            return httpx.Response(200, json={"ok": True, "pid": 777, "version": "0.1.0"})
        if request.headers.get("Authorization") != "Bearer unused":
            return httpx.Response(401, json={"detail": "unauthorized"})
        body = json.loads(request.content or b"null")
        self.calls.append((request.method, path, body))
        if path == "/api/v1/operations":
            return httpx.Response(200, json=API_OPS)
        if path == "/api/v1/gui/operations":
            return httpx.Response(200, json=GUI_OPS)
        if path == "/api/v1/call/gateway.status":
            return httpx.Response(200, json={"running": True})
        if path == "/api/v1/call/gateway.lifecycle":
            return httpx.Response(200, json={"done": body})
        if path == "/api/v1/gui/sections":
            return httpx.Response(409, json={"detail": "the Hermes Manager window is not open"})
        return httpx.Response(404, json={"detail": "no such route"})


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    fake = FakeBridge()
    (tmp_path / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "unused", "pid": 777,
                                                       "owner": "abp"}))
    monkeypatch.setenv("HM_HOME", str(tmp_path))
    transport = httpx.MockTransport(fake.handle)
    real = httpx.Client

    def get(url, **kw):
        with real(transport=transport) as c:
            return c.get(url)

    def request(method, url, **kw):
        with real(transport=transport) as c:
            return c.request(method, url, json=kw.get("json"), headers=kw.get("headers"))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "request", request)
    client._ops.update(at=0.0, url="", api=[], gui=[])
    return fake


def test_finds_the_bridge_and_its_owner(bridge):
    b = client.find()
    assert b and b.pid == 777 and b.owner == "abp" and b.token == "unused"


def test_operations_merge_api_and_window(bridge):
    ids = [o["id"] for o in client.operations()]
    assert ids == ["gateway.status", "gateway.lifecycle", "gui.sections", "gui.click"]
    assert client.operation("gui.click")["mutating"] and client.operation("gui.click")["params"]["required"] == ["target"]


def test_calls_go_to_the_right_routes(bridge):
    assert client.call("gateway.status") == {"running": True}
    assert client.call("gateway.lifecycle", {"action": "restart"}) == {"done": {"action": "restart"}}
    with pytest.raises(ManagerError, match="not open") as e:
        client.call("gui.sections")
    assert e.value.status == 409


def test_read_tool_refuses_changes(bridge):
    import bot.hermes_manager.tools  # noqa: F401
    from bot.agent_runtime import toolspec
    out = asyncio.run(toolspec.dispatch("hm_read", {"operation": "gateway.lifecycle", "args": {"action": "stop"}}))
    assert "changes something" in out
    assert not [c for c in bridge.calls if c[1].endswith("gateway.lifecycle")]
    assert "running" in asyncio.run(toolspec.dispatch("hm_read", {"operation": "gateway.status"}))


def test_tools_split_reading_from_changing():
    import bot.hermes_manager.tools  # noqa: F401
    from bot.agent_runtime import toolspec
    for name in ("hm_status", "hm_operations", "hm_read", "hm_gui_look"):
        assert not toolspec.registered_dangerous(name), name
    for name in ("hm_call", "hm_gui_act", "hm_setup"):
        assert toolspec.registered_dangerous(name), name


def test_stopping_refuses_the_windows_own_bridge(bridge, tmp_path):
    (tmp_path / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "unused", "pid": 777,
                                                       "owner": "app"}))
    with pytest.raises(ManagerError, match="belongs to the Hermes Manager window"):
        harness.stop_bridge()


def test_install_dir_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_HERMES_MANAGER_DIR", str(tmp_path / "hm"))
    assert harness.install_dir() == tmp_path / "hm"


def test_update_refuses_to_touch_uncommitted_work(monkeypatch):
    monkeypatch.setattr(harness, "install_info", lambda fetch=False: {"changed_files": 2, "behind": 1})
    with pytest.raises(ManagerError, match="uncommitted"):
        harness._update(lambda _l: None)


def test_panel_is_identical_in_both_uis_and_wired():
    a = (ROOT / "bot/dashboard/static/hermesmgr-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/hermesmgr-panel.js").read_text(encoding="utf-8")
    assert a == b
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="hermes-manager"' in html and 'id="hmp-root"' in html and "hermesmgr-panel.js" in html
