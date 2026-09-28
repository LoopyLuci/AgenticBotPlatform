"""ABP's VM-Harness module: finding the hub from its control file, calling it, the agent tools' read/change split, the
install locator, and the page (identical in both UIs). A small aiohttp-free stand-in plays the hub over httpx."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bot.vm_harness import client, harness
from bot.vm_harness.client import HarnessError

ROOT = Path(__file__).resolve().parents[1]
OPS = [{"id": "vm.list", "group": "vm", "summary": "Every VM", "params": {}, "mutating": False, "destructive": False},
       {"id": "vm.start", "group": "vm", "summary": "Start", "params": {}, "mutating": True, "destructive": False},
       {"id": "gui.panels", "group": "gui", "summary": "Panels", "params": {}, "mutating": False, "destructive": False}]


class FakeHub:
    def __init__(self):
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "service": "vm-harness", "pid": 4242, "version": "0.3.0",
                                             "gui": False})
        if request.headers.get("Authorization") != "Bearer unused":
            return httpx.Response(401, json={"ok": False, "error": "missing or wrong token", "code": "unauthorized"})
        if request.url.path == "/v1/operations":
            return httpx.Response(200, json={"ok": True, "operations": OPS})
        if request.url.path.startswith("/v1/call/"):
            op = request.url.path.rsplit("/", 1)[1]
            args = json.loads(request.content or b"{}")
            self.calls.append((op, args))
            if op == "vm.start" and args.get("name") == "ghost":
                return httpx.Response(404, json={"ok": False, "error": "no VM named 'ghost'", "code": "not_found"})
            return httpx.Response(200, json={"ok": True, "result": [{"name": "alpha", "backend": "qemu", "state": "running"}]
                                             if op == "vm.list" else {"done": op}})
        return httpx.Response(404, json={"ok": False, "error": "no route"})


@pytest.fixture
def hub(tmp_path, monkeypatch):
    fake = FakeHub()
    home = tmp_path / "vmh"
    home.mkdir()
    (home / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:1", "token": "unused", "pid": 4242}))
    monkeypatch.setenv("VMH_HOME", str(home))
    monkeypatch.setattr(client, "_cfg", lambda: {})
    transport = httpx.MockTransport(fake.handle)
    real = httpx.Client

    def get(url, **kw):
        with real(transport=transport) as c:
            return c.get(url, **{k: v for k, v in kw.items() if k != "timeout"})

    def request(method, url, **kw):
        with real(transport=transport) as c:
            return c.request(method, url, **{k: v for k, v in kw.items() if k != "timeout"})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "request", request)
    client._ops_cache.update(at=0.0, url="", ops=[])
    return fake


def test_finds_the_hub_from_its_control_file(hub):
    found = client.find()
    assert found and found.url == "http://127.0.0.1:1" and found.token == "unused" and not found.remote


def test_a_stale_control_file_is_ignored(hub, tmp_path, monkeypatch):
    (Path(client.vmh_home()) / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:1", "token": "unused",
                                                                       "pid": 1}))
    assert client.find() is None       # the pid that answers is not the one that wrote the file


def test_calls_and_errors(hub):
    assert client.call("vm.list")[0]["name"] == "alpha"
    with pytest.raises(HarnessError) as e:
        client.call("vm.start", {"name": "ghost"})
    assert e.value.code == "not_found" and e.value.status == 404
    assert client.operation("vm.start")["mutating"] is True


def test_read_tool_refuses_operations_that_change_things(hub):
    import bot.vm_harness.tools  # noqa: F401  (registers)
    from bot.agent_runtime import toolspec
    out = asyncio.run(toolspec.dispatch("vmh_read", {"operation": "vm.start", "args": {"name": "alpha"}}))
    assert "changes something" in out and not [c for c in hub.calls if c[0] == "vm.start"]
    assert "alpha" in asyncio.run(toolspec.dispatch("vmh_read", {"operation": "vm.list"}))


def test_tools_split_reading_from_changing():
    import bot.vm_harness.tools  # noqa: F401
    from bot.agent_runtime import toolspec
    specs = {n: toolspec._registered[n][1] for n in ("vmh_status", "vmh_vms", "vmh_operations", "vmh_read", "vmh_gui_look",
                                                "vmh_call", "vmh_gui_act", "vmh_setup")}
    for name in ("vmh_status", "vmh_vms", "vmh_operations", "vmh_read", "vmh_gui_look"):
        assert specs[name].read_only, name
    for name in ("vmh_call", "vmh_gui_act", "vmh_setup"):
        assert toolspec.registered_dangerous(name), name


def test_install_dir_prefers_env_then_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_VM_HARNESS_DIR", str(tmp_path / "x"))
    assert harness.install_dir() == tmp_path / "x"
    monkeypatch.delenv("ABP_VM_HARNESS_DIR")
    monkeypatch.setattr(client, "_cfg", lambda: {"path": str(tmp_path / "y")})
    assert harness.install_dir() == tmp_path / "y"


def test_update_refuses_to_touch_uncommitted_work(monkeypatch):
    monkeypatch.setattr(harness, "install_info", lambda fetch=False: {"changed_files": 3, "behind": 2})
    with pytest.raises(HarnessError, match="uncommitted"):
        harness._update(lambda _l: None)


def test_panel_is_identical_in_both_uis_and_wired():
    a = (ROOT / "bot/dashboard/static/vmharness-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/vmharness-panel.js").read_text(encoding="utf-8")
    assert a == b
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="vm-harness"' in html and 'id="vh-root"' in html and "vmharness-panel.js" in html
        assert 'href="#vm-harness"' in html
