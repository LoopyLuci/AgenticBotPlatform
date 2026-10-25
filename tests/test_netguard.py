"""Sentinel in ABP (bot/netguard.py, bot/dashboard/netguard_api.py, netguard-panel.js).

The engine is the Sentinel module's sentinel_core. Tests that need it use the checkout next to ABP (or
$ABP_MODULE_SENTINEL_DIR) and skip when there is none; nothing here touches the host's firewall or DNS (the firewall
backend is Sentinel's in-memory one, and system DNS runs only as a dry run with fake adapters)."""
from __future__ import annotations

import socket
import struct
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKEN = {"X-Dashboard-Token": "test-token"}


def test_panel_is_identical_and_on_both_pages():
    a = (ROOT / "bot/dashboard/static/netguard-panel.js").read_text(encoding="utf-8")
    assert a == (ROOT / "desktop-app/ui/netguard-panel.js").read_text(encoding="utf-8")
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert '<section id="netguard" data-unit="netguard">' in html and 'id="ng-root"' in html
        assert "netguard-panel.js" in html and 'href="#netguard"' in html


def test_every_setting_has_a_switch_or_control_on_the_page():
    """The owner's rule: every setting gets a GUI control (both ways)."""
    from bot import netguard as ng
    js = (ROOT / "bot/dashboard/static/netguard-panel.js").read_text(encoding="utf-8")
    for sec, keys in ng.DEFAULTS.items():
        for key in keys:
            if (sec, key) in ng._BOOLS:
                assert f"sw('{sec}', '{key}'" in js, f"{sec}.{key} has no switch"
    for control in ("ng-listen", "ng-up", "ng-resp", "data-list", "ng-block", "ng-allow", "data-rule-on", "ng-r-add",
                    "data-rule-del"):
        assert control in js, control


@pytest.fixture
def ng(monkeypatch, tmp_path):
    from bot import netguard
    state: dict = {}

    def set_values(changes, actor="dashboard"):
        for path, v in changes.items():
            node = state
            for k in path[1:-1]:
                node = node.setdefault(k, {})
            node[path[-1]] = v

    from bot.config import config
    monkeypatch.setattr(config, "set_values", set_values)
    monkeypatch.setattr(netguard, "_raw", lambda: state)
    monkeypatch.setattr(netguard, "data_dir", lambda: tmp_path)
    yield netguard
    netguard.service.stop()


def _core_or_skip(ng):
    try:
        return ng.core()
    except ng.Unavailable:
        pytest.skip("no Sentinel checkout here")


@pytest.fixture
def client(temp_db, monkeypatch, ng):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app
    return TestClient(build_app())


def test_settings_need_the_token_and_are_validated(client, ng):
    assert client.put("/api/netguard/settings", json={"adblock": {"enabled": True}}).status_code in (401, 403)
    for bad in ({}, {"nope": {}}, {"adblock": {"enabled": "yes"}}, {"adblock": {"wat": 1}},
                {"adblock": {"listen": "not-an-ip:53"}}, {"adblock": {"upstream": "1.1.1.1:99999"}},
                {"adblock": {"response": "teapot"}}, {"firewall": {"dry_run": 0}}):
        assert client.put("/api/netguard/settings", headers=TOKEN, json=bad).status_code == 400, bad
    r = client.put("/api/netguard/settings", headers=TOKEN, json={"firewall": {"dry_run": False}, "system_dns": {"enabled": False}})
    assert r.status_code == 200
    assert ng.settings()["firewall"]["dry_run"] is False and ng.settings()["firewall"]["enabled"] is False


def test_defaults_are_all_off_and_dry_run():
    from bot import netguard as ng
    assert ng.DEFAULTS["adblock"]["enabled"] is False and ng.DEFAULTS["firewall"]["enabled"] is False
    assert ng.DEFAULTS["firewall"]["dry_run"] is True and ng.DEFAULTS["system_dns"]["dry_run"] is True
    assert ng.hostport("[::1]:5353") == ("::1", 5353) and ng.hostport("127.0.0.1") == ("127.0.0.1", 53)


class _Upstream:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            ans = struct.pack(">HHHIH", 0xC00C, 1, 1, 60, 4) + bytes([10, 9, 8, 7])
            end = data.index(b"\x00", 12) + 5
            self.sock.sendto(data[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + data[12:end] + ans, addr)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_adblock_switch_starts_the_filter_and_it_blocks(client, ng):
    core = _core_or_skip(ng)
    up = _Upstream()
    port = _free_udp_port()
    (ng.data_dir() / "lists").mkdir(exist_ok=True)
    (ng.data_dir() / "lists" / "stevenblack.txt").write_text("0.0.0.0 ads.example.com\n", encoding="utf-8")
    body = {"adblock": {"enabled": True, "listen": f"127.0.0.1:{port}", "upstream": "127.0.0.1:%d" % up.sock.getsockname()[1],
                        "lists": {"stevenblack": True}, "block": ["Mine.Test"], "allow": []}}
    st = client.put("/api/netguard/settings", headers=TOKEN, json=body).json()
    assert st["dns"]["running"] and st["dns"]["domains"] == 2, st["dns"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        for name in ("x.ads.example.com", "mine.test", "example.org"):
            s.sendto(core.dns.build_query(name), ("127.0.0.1", port))
            s.recvfrom(4096)
    q = client.get("/api/netguard/queries", headers=TOKEN).json()["queries"]
    assert [(x["name"], x["blocked"]) for x in q] == [("example.org", False), ("mine.test", True), ("x.ads.example.com", True)]
    assert client.post("/api/netguard/check", headers=TOKEN, json={"name": "a.mine.test"}).json()["blocked"] is True
    # the allow list wins, and the running filter picks it up without a restart
    client.put("/api/netguard/settings", headers=TOKEN, json={"adblock": {"allow": ["mine.test"]}})
    assert client.post("/api/netguard/check", headers=TOKEN, json={"name": "mine.test"}).json()["blocked"] is False
    st = client.put("/api/netguard/settings", headers=TOKEN, json={"adblock": {"enabled": False}}).json()
    assert st["dns"]["running"] is False
    kinds = [e["kind"] for e in client.get("/api/netguard/journal", headers=TOKEN).json()["entries"]]
    assert "dns" in kinds and "settings" in kinds
    up.sock.close()


def test_a_port_in_use_is_reported_not_raised(client, ng):
    _core_or_skip(ng)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        st = client.put("/api/netguard/settings", headers=TOKEN,
                        json={"adblock": {"enabled": True, "listen": f"127.0.0.1:{port}"}}).json()
    assert st["dns"]["running"] is False and "could not start" in st["dns"]["error"]


def test_firewall_rules_plan_dry_run_apply_and_roll_back(client, ng, monkeypatch):
    core = _core_or_skip(ng)
    be = core.firewall.MemoryBackend()
    monkeypatch.setattr(ng, "_backend", lambda: be)
    rule = {"name": "No telemetry", "direction": "out", "action": "block", "remote_addresses": ["10.1.2.3"]}
    assert client.post("/api/netguard/firewall/rules", json=rule).status_code in (401, 403)
    assert client.post("/api/netguard/firewall/rules", headers=TOKEN, json={**rule, "name": "x'; evil"}).status_code == 400
    assert client.post("/api/netguard/firewall/rules", headers=TOKEN, json=rule).status_code == 200
    fw = client.get("/api/netguard/firewall", headers=TOKEN).json()
    assert fw["changes"] == [] and fw["enabled"] is False          # the firewall switch is still off
    client.put("/api/netguard/settings", headers=TOKEN, json={"firewall": {"enabled": True}})
    fw = client.get("/api/netguard/firewall", headers=TOKEN).json()
    assert [c["op"] for c in fw["changes"]] == ["add"] and "New-NetFirewallRule" in fw["script"]
    dry = client.post("/api/netguard/firewall/apply", headers=TOKEN).json()
    assert dry["dry_run"] is True and be.rules == {}                # dry run: nothing touched
    client.put("/api/netguard/settings", headers=TOKEN, json={"firewall": {"dry_run": False}})
    live = client.post("/api/netguard/firewall/apply", headers=TOKEN).json()
    assert live["ok"] and set(be.rules) == {"Sentinel - No telemetry"}
    client.put("/api/netguard/firewall/rules/No%20telemetry", headers=TOKEN, json={"enabled": False})
    client.post("/api/netguard/firewall/apply", headers=TOKEN)
    assert be.rules == {}
    assert client.post("/api/netguard/firewall/rollback", headers=TOKEN).json()["ok"]
    assert set(be.rules) == {"Sentinel - No telemetry"}
    assert client.delete("/api/netguard/firewall/rules/No%20telemetry", headers=TOKEN).status_code == 200
    assert client.delete("/api/netguard/firewall/rules/No%20telemetry", headers=TOKEN).status_code == 404
    assert any(e["kind"] == "firewall" and e.get("commands") for e in ng.journal_tail())


def test_system_dns_needs_port_53_and_runs_only_as_a_dry_run_by_default(client, ng, monkeypatch):
    core = _core_or_skip(ng)
    monkeypatch.setattr(ng.os, "name", "nt")
    monkeypatch.setattr(core.systemdns, "adapters", lambda: [{"index": 4, "name": "Ethernet", "servers": ["192.168.1.1"], "dhcp": True}])
    ran = []
    monkeypatch.setattr(core.winps, "run_script", lambda body, **kw: ran.append(body) or {"ok": True})
    client.put("/api/netguard/settings", headers=TOKEN, json={"system_dns": {"enabled": True}})
    assert "port 53" in client.get("/api/netguard/system-dns", headers=TOKEN).json()["error"]
    client.put("/api/netguard/settings", headers=TOKEN, json={"adblock": {"listen": "127.0.0.1:53"}})
    view = client.get("/api/netguard/system-dns", headers=TOKEN).json()
    assert view["plan"]["commands"] == ["Set-DnsClientServerAddress -InterfaceIndex 4 -ServerAddresses '127.0.0.1'"]
    assert client.post("/api/netguard/system-dns/apply", headers=TOKEN).json()["dry_run"] is True and ran == []
    client.put("/api/netguard/settings", headers=TOKEN, json={"system_dns": {"dry_run": False}})
    assert client.post("/api/netguard/system-dns/apply", headers=TOKEN).json()["ok"] and len(ran) == 1
    assert ng._read_json("systemdns-saved.json")[0]["index"] == 4
    client.put("/api/netguard/settings", headers=TOKEN, json={"system_dns": {"enabled": False}})
    assert client.post("/api/netguard/system-dns/apply", headers=TOKEN).json()["action"] == "restore"
    assert "-ResetServerAddresses" in ran[-1] and ng._read_json("systemdns-saved.json") is None


def test_status_without_the_module_says_how_to_install(client, ng, monkeypatch):
    monkeypatch.setattr(ng, "core_dir", lambda: None)
    monkeypatch.setattr(ng, "_core_cache", {})
    st = client.get("/api/netguard/status", headers=TOKEN).json()
    assert st["core"]["available"] is False and "Modules page" in st["core"]["reason"]
    assert client.get("/api/netguard/firewall", headers=TOKEN).json()["error"]


def test_sentinel_is_a_builtin_module():
    from bot.modules import registry
    m = registry.find("sentinel")
    assert m is not None and m.repo.endswith("/Sentinel.git") and "sentinel_core/__init__.py" in m.marker


def test_the_default_listen_port_is_not_mdns():
    """Windows' own mDNS holds UDP 5353, so a filter defaulting to it never starts (found live 2026-10-25)."""
    from bot import netguard as ng
    assert ng.hostport(ng.DEFAULTS["adblock"]["listen"])[1] not in (5353, 53)
