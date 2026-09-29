"""Power (keeping this machine awake, waking others) and remote control of a linked server's modules (peers.proxy,
control_auth, the machine argument of the agent's tools)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bot import db, peers, power
from bot.config import config
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _set(monkeypatch, section: str, value) -> None:
    monkeypatch.setitem(config._data, section, value)


# ---- keeping awake ----------------------------------------------------------------------------------------------------
def test_holds_are_reasons_until_they_expire_or_are_released(monkeypatch):
    k = power.KeepAwake()
    monkeypatch.setattr(k, "start", lambda: None)   # no OS request from a test
    k.busy_probe = lambda: []
    _set(monkeypatch, "power", {"keep_awake": "off"})
    assert k.reasons() == []
    k.hold("copy", "copying a disk", 5, by="Server")
    assert k.reasons() == ["copying a disk (asked by Server)"]
    k.holds["copy"]["until"] = 0            # expired
    assert k.reasons() == []
    k.hold("a", "one", 0)                   # 0 = until released
    k.hold("b", "two", 0)
    assert len(k.reasons()) == 2
    k.release("a")
    assert k.reasons() == ["two"]
    k.release()
    assert k.reasons() == []


def test_modes(monkeypatch):
    k = power.KeepAwake()
    k.busy_probe = lambda: ["1 job(s) running"]
    _set(monkeypatch, "power", {"keep_awake": "always"})
    assert k.reasons() == ["set to always stay awake"]
    _set(monkeypatch, "power", {"keep_awake": "off"})
    assert k.reasons() == []
    _set(monkeypatch, "power", {"keep_awake": "while_busy", "idle_minutes": 10})
    assert k.reasons() == ["1 job(s) running"]
    k.busy_probe = lambda: []
    k.note_activity("an agent turn")
    assert k.reasons() == ["an agent turn 0 min ago"]
    k.last_activity["an agent turn"] -= 11 * 60   # older than idle_minutes
    assert k.reasons() == []
    _set(monkeypatch, "power", {"keep_awake": "nonsense"})
    assert power.settings()["keep_awake"] == "while_busy"


def test_save_settings_validates_and_writes_config():
    with pytest.raises(ValueError):
        power.save_settings(keep_awake="sometimes")
    with pytest.raises(ValueError):
        power.save_settings(bogus=1)
    out = power.save_settings(keep_awake="always", idle_minutes=3)
    assert out["keep_awake"] == "always" and out["idle_minutes"] == 3
    assert config.current["power"]["keep_awake"] == "always"


# ---- waking -----------------------------------------------------------------------------------------------------------
def test_magic_packet(monkeypatch):
    sent = []

    class Sock:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def setsockopt(self, *a): pass
        def sendto(self, data, addr): sent.append((data, addr))

    monkeypatch.setattr(power.socket, "socket", Sock)
    monkeypatch.setattr(power.time, "sleep", lambda s: None)
    out = power.wake("7c-10-c9-44-07-95", "192.168.0.255", repeat=1)
    assert out["sent"] == 4 and out["to"] == ["192.168.0.255", "255.255.255.255"]
    data = sent[0][0]
    assert data[:6] == b"\xff" * 6 and data[6:12] == bytes.fromhex("7c10c9440795") and len(data) == 102
    with pytest.raises(ValueError):
        power.wake("not-a-mac")


def test_wake_target_needs_a_learned_machine(monkeypatch):
    _set(monkeypatch, "power", {"wake": {"Server": {"mac": "AA:BB:CC:DD:EE:FF", "broadcast": "10.0.0.255"}}})
    monkeypatch.setattr(power, "wake", lambda mac, bcast, **k: {"mac": mac, "to": [bcast]})
    assert power.wake_target("Server") == {"target": "Server", "mac": "AA:BB:CC:DD:EE:FF", "to": ["10.0.0.255"]}
    with pytest.raises(ValueError):
        power.wake_target("Nobody")


# ---- a linked server controlling this one -----------------------------------------------------------------------------
def _peer_headers():
    _, plaintext = db.create_api_key("peer: other-machine", kind="peer_server")
    return {"X-Dashboard-Token": plaintext}


def test_peers_reach_module_areas_only_when_allowed(temp_db, monkeypatch):
    client = _client(monkeypatch)
    headers = _peer_headers()
    _set(monkeypatch, "peers", {"remote_control": []})
    r = client.get("/api/power/status", headers=headers)
    assert r.status_code == 403 and "peers.remote_control" in r.json()["detail"]
    assert client.post("/api/vm-harness/call", headers=headers, json={"operation": "vm.list"}).status_code == 403

    _set(monkeypatch, "peers", {"remote_control": ["power", "not-an-area"]})
    assert peers.allowed_control_areas() == {"power"}
    r = client.get("/api/power/status", headers=headers)
    assert r.status_code == 200 and "keep_awake" in r.json()
    assert any("a linked server using power" in x for x in power.keeper.reasons()) or \
        power.settings()["keep_awake"] != "while_busy"
    assert client.get("/api/vm-harness/jobs", headers=headers).status_code == 403   # still not allowed
    power.keeper.release()


def test_dashboard_always_and_phone_reads_only(temp_db, monkeypatch):
    client = _client(monkeypatch)
    dash = {"X-Dashboard-Token": "test-token"}
    assert client.get("/api/power/status", headers=dash).status_code == 200
    _, phone = db.create_api_key("test-phone", kind="device")
    assert client.get("/api/power/status", headers={"X-Dashboard-Token": phone}).status_code == 200
    assert client.post("/api/power/hold", headers={"X-Dashboard-Token": phone}, json={"minutes": 1}).status_code == 403
    assert client.get("/api/peers/control", headers=dash).json()["areas"] == ["hermes-manager", "modules", "power", "transferdaemon", "vm-harness"]


# ---- proxy (this machine calling a linked one) -------------------------------------------------------------------------
def _linked(name="Server", base="http://server.test:8765"):
    key_id, _ = db.create_api_key(f"peer: {name}", kind="peer_server")
    db.create_peer_server(name, base, "unused", key_id)
    return peers.find_peer(name)


def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(peers.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items()
                                                                                    if k != "transport"}))


def test_proxy_forwards_and_reports_refusals(temp_db, monkeypatch):
    row = _linked()
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, req.headers.get("X-Dashboard-Token"), req.content))
        if req.url.path == "/api/vm-harness/status":
            return httpx.Response(200, json={"hub": "running"})
        return httpx.Response(403, json={"detail": "this server does not let linked servers control power"})

    _mock_http(monkeypatch, handler)
    assert asyncio.run(peers.proxy(row, "GET", "/api/vm-harness/status")) == {"hub": "running"}
    assert seen[0][:3] == ("GET", "/api/vm-harness/status", "unused")
    with pytest.raises(peers.PeerError, match="does not let linked servers control power"):
        asyncio.run(peers.proxy(row, "POST", "/api/power/hold", {"minutes": 1}))
    for bad in ("/api/config/set", "/api/vm-harness/../config", "/api/bots"):
        with pytest.raises(peers.PeerError):
            asyncio.run(peers.proxy(row, "GET", bad))
    with pytest.raises(peers.PeerError, match="no linked server"):
        peers.find_peer("Nowhere")
    assert peers.find_peer("server")["name"] == "Server"   # by name, any case


def test_proxy_wakes_a_sleeping_server_first(temp_db, monkeypatch):
    row = _linked()
    state = {"awake": False}

    def handler(req):
        if not state["awake"]:
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(200, json={"ok": True})

    async def wake_and_wait(peer_row, wait_s=120.0):
        state["awake"] = True
        return True

    _mock_http(monkeypatch, handler)
    monkeypatch.setattr(power, "wake_and_wait", wake_and_wait)
    _set(monkeypatch, "power", {"auto_wake": False})
    with pytest.raises(peers.PeerError, match="not reachable"):
        asyncio.run(peers.proxy(row, "GET", "/api/power/status"))
    _set(monkeypatch, "power", {"auto_wake": True})
    assert asyncio.run(peers.proxy(row, "GET", "/api/power/status")) == {"ok": True}


def test_dashboard_proxy_route(temp_db, monkeypatch):
    client = _client(monkeypatch)
    _linked()
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={"path": req.url.path, "body": json.loads(req.content or b"null")}))
    r = client.post("/api/peers/Server/proxy", headers={"X-Dashboard-Token": "test-token"},
                    json={"method": "POST", "path": "/api/vm-harness/call", "body": {"operation": "vm.list"}})
    assert r.status_code == 200
    assert r.json()["result"] == {"path": "/api/vm-harness/call", "body": {"operation": "vm.list"}}
    assert client.post("/api/peers/Server/proxy", headers={"X-Dashboard-Token": "test-token"},
                       json={"path": "/api/bots"}).status_code == 502
    _, phone = db.create_api_key("test-phone", kind="device")
    assert client.post("/api/peers/Server/proxy", headers={"X-Dashboard-Token": phone},
                       json={"path": "/api/power/status"}).status_code in (401, 403)


def test_agent_tools_take_a_machine(temp_db, monkeypatch):
    from bot.agent_runtime import toolspec
    import bot.agent_runtime.tools  # noqa: F401  (registers everything)

    _linked()
    calls = []

    def handler(req):
        calls.append((req.method, str(req.url.path), json.loads(req.content or b"null")))
        if req.url.path.endswith("/operations"):
            return httpx.Response(200, json=[{"id": "vm.status", "group": "vm", "summary": "status", "mutating": False,
                                              "destructive": False},
                                             {"id": "vm.start", "group": "vm", "summary": "start", "mutating": True,
                                              "destructive": False}])
        if req.url.path.endswith("/vms"):
            return httpx.Response(200, json=[{"name": "omarchy", "backend": "qemu", "state": "stopped", "config": {}}])
        return httpx.Response(200, json={"result": {"state": "running"}})

    _mock_http(monkeypatch, handler)

    def run(name, inp):
        return asyncio.run(toolspec.dispatch(name, inp))

    assert json.loads(run("vmh_vms", {"machine": "Server"})) == [{"name": "omarchy", "backend": "qemu", "state": "stopped"}]
    assert json.loads(run("vmh_read", {"machine": "Server", "operation": "vm.status", "args": {"name": "omarchy"}})) == \
        {"state": "running"}
    assert "use vmh_call" in run("vmh_read", {"machine": "Server", "operation": "vm.start"})
    run("vmh_call", {"machine": "Server", "operation": "vm.start", "args": {"name": "omarchy", "backend": "qemu"}})
    assert calls[-1] == ("POST", "/api/vm-harness/call",
                         {"operation": "vm.start", "args": {"name": "omarchy", "backend": "qemu"}, "timeout_s": 900.0})
    run("vmh_gui_act", {"machine": "Server", "action": "open", "panel": "vms"})
    assert calls[-1][2] == {"operation": "gui.open", "args": {"panel": "vms"}, "timeout_s": 120}
    run("vmh_setup", {"machine": "Server", "action": "start"})
    assert calls[-1][:2] == ("POST", "/api/vm-harness/hub/start")
    run("hm_call", {"machine": "Server", "operation": "gateway.start"})
    assert calls[-1][:2] == ("POST", "/api/hermes-manager/call")
    run("power_keep_awake", {"machine": "Server", "minutes": 30, "reason": "using its VM"})
    assert calls[-1][:2] == ("POST", "/api/power/hold") and calls[-1][2]["minutes"] == 30
    assert "no linked server" in run("vmh_status", {"machine": "Elsewhere"})


def test_owner_chooses_what_linked_servers_control(temp_db, monkeypatch):
    client = _client(monkeypatch)
    dash = {"X-Dashboard-Token": "test-token"}
    r = client.put("/api/peers/control", headers=dash, json={"allowed": ["vm-harness", "power"]})
    assert r.status_code == 200 and r.json()["allowed"] == ["power", "vm-harness"]
    assert peers.allowed_control_areas() == {"power", "vm-harness"}
    assert client.put("/api/peers/control", headers=dash, json={"allowed": ["everything"]}).status_code == 400
    # a linked server can never widen what it may do
    assert client.put("/api/peers/control", headers=_peer_headers(), json={"allowed": ["hermes-manager"]}).status_code in (401, 403)


def test_power_page_is_the_same_in_both_uis():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    a = (root / "bot/dashboard/static/power-panel.js").read_text(encoding="utf-8")
    assert a == (root / "desktop-app/ui/power-panel.js").read_text(encoding="utf-8")
    for html in ((root / "bot/dashboard/static/dashboard.html").read_text(encoding="utf-8"),
                 (root / "desktop-app/ui/index.html").read_text(encoding="utf-8")):
        assert 'id="power"' in html and 'id="pw-root"' in html and "power-panel.js" in html
