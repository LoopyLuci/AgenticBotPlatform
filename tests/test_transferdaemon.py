"""ABP's TransferDaemon module: finding the daemon's control hub from its discovery file, calling operations, sending
to a contact by name, the agent tools' read/change split, the install locator and binaries, and the page (identical
in both UIs). A stand-in hub answers over httpx; a live test against a real daemon runs when one is built."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bot.transferdaemon import client, harness
from bot.transferdaemon.client import DaemonError

ROOT = Path(__file__).resolve().parents[1]
OPS = [
    {"id": "contacts.get_contacts", "group": "contacts", "summary": "Every contact", "mutating": False,
     "destructive": False, "streaming": False, "input": {"type": "object", "properties": {}}, "output": {}},
    {"id": "messages.send_text", "group": "messages", "summary": "Send a text", "mutating": True, "destructive": False,
     "streaming": False, "input": {"type": "object", "properties": {"contact_id": {"type": "string"}}}, "output": {}},
    {"id": "gui.state", "group": "gui", "summary": "The window", "mutating": False, "destructive": False,
     "streaming": False, "input": {"type": "object"}, "output": {}},
]
CONTACTS = [{"id": "ab" * 32, "name": "Friend", "online": True}, {"id": "cd" * 32, "name": "Other", "online": False}]


class FakeHub:
    def __init__(self):
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "pid": 4242, "version": "0.1.0"})
        if request.headers.get("Authorization") != "Bearer unused":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "missing or wrong token"}})
        body = json.loads(request.content or b"null")
        self.calls.append((request.method, path, body))
        if path == "/v1/operations":
            return httpx.Response(200, json=OPS)
        if path == "/v1/call/contacts.get_contacts":
            return httpx.Response(200, json={"result": {"contacts": CONTACTS}})
        if path == "/v1/call/messages.send_text":
            return httpx.Response(200, json={"result": {"id": "d-1", "status": "pending", "text": body["text"]}})
        if path == "/v1/call/gui.state":
            return httpx.Response(409, json={"error": {"code": "not_attached", "message": "the window is not open"}})
        if path == "/v1/audit":
            return httpx.Response(200, json=[{"operation": "messages.send_text", "ok": True}])
        return httpx.Response(404, json={"error": {"code": "unknown_operation", "message": "no such operation"}})


@pytest.fixture
def hub(tmp_path, monkeypatch):
    fake = FakeHub()
    data = tmp_path / "transferdaemon"
    data.mkdir()
    (data / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "unused", "pid": 4242}))
    monkeypatch.setenv("TRANSFERD_DATA_DIR", str(tmp_path))
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
    client._ops.update(at=0.0, url="", ops=[])
    return fake


def test_finds_the_hub(hub):
    h = client.find()
    assert h and h.pid == 4242 and h.token == "unused" and h.version == "0.1.0"


def test_a_stale_discovery_file_is_ignored(hub, tmp_path):
    (tmp_path / "transferdaemon" / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "unused", "pid": 1}))
    assert client.find() is None


def test_calls_and_errors(hub):
    assert client.call("contacts.get_contacts")["contacts"][0]["name"] == "Friend"
    with pytest.raises(DaemonError, match="not open") as e:
        client.call("gui.state")
    assert e.value.code == "not_attached" and e.value.status == 409
    assert [o["id"] for o in client.operations()] == [o["id"] for o in OPS]
    assert client.audit(5)[0]["operation"] == "messages.send_text"


def test_send_by_name_and_the_read_split(hub):
    import bot.transferdaemon.tools  # noqa: F401
    from bot.agent_runtime import toolspec
    out = asyncio.run(toolspec.dispatch("td_send", {"to": "friend", "text": "hi"}))
    assert '"status": "pending"' in out
    assert hub.calls[-1] == ("POST", "/v1/call/messages.send_text", {"contact_id": "ab" * 32, "text": "hi"})
    assert "no single contact" in asyncio.run(toolspec.dispatch("td_send", {"to": "nobody", "text": "x"}))
    assert "changes something" in asyncio.run(toolspec.dispatch("td_read", {"operation": "messages.send_text"}))
    assert "Friend" in asyncio.run(toolspec.dispatch("td_read", {"operation": "contacts.get_contacts"}))


def test_tools_split_reading_from_changing():
    import bot.transferdaemon.tools  # noqa: F401
    from bot.agent_runtime import toolspec
    for name in ("td_status", "td_operations", "td_read", "td_gui_look"):
        assert not toolspec.registered_dangerous(name), name
    for name in ("td_call", "td_send", "td_gui_act", "td_tui", "td_setup"):
        assert toolspec.registered_dangerous(name), name


def test_install_dir_and_binaries(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_TRANSFERDAEMON_DIR", str(tmp_path / "td"))
    assert harness.install_dir() == tmp_path / "td"
    rel = tmp_path / "td" / "transferdaemon" / "target" / "release"
    rel.mkdir(parents=True)
    (rel / f"transferd-cli{harness.EXE}").write_text("")
    assert harness.bin_dir() == rel
    assert harness.binary("transferd").name.startswith("transferd")


def test_update_refuses_to_touch_uncommitted_work(monkeypatch):
    monkeypatch.setattr(harness, "install_info", lambda fetch=False: {"changed_files": 3, "behind": 1})
    with pytest.raises(DaemonError, match="uncommitted"):
        harness._update(lambda _l: None)


def test_start_needs_a_build(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_TRANSFERDAEMON_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("TRANSFERD_DATA_DIR", str(tmp_path))
    with pytest.raises(DaemonError, match="not built"):
        harness.start_daemon(wait_s=1)


def test_peers_may_control_it_when_allowed():
    from bot import peers
    assert peers.control_area_of("/api/transferdaemon/call") == "transferdaemon"


def test_panel_is_identical_in_both_uis_and_wired():
    a = (ROOT / "bot/dashboard/static/transferd-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/transferd-panel.js").read_text(encoding="utf-8")
    assert a == b
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="transferdaemon"' in html and 'id="tdp-root"' in html and "transferd-panel.js" in html


def _live_build() -> Path | None:
    d = ROOT.parent / "TransferDaemon" / "transferdaemon" / "target"
    for profile in ("debug", "release"):
        exe = d / profile / f"transferd{harness.EXE}"
        if exe.is_file() and (d / profile / f"transferd-cli{harness.EXE}").is_file():
            return d / profile
    return None


@pytest.mark.skipif(_live_build() is None, reason="no TransferDaemon build next to ABP")
def test_live_daemon_through_the_module(tmp_path, monkeypatch):
    """A real daemon (its own data folder and ports), started, used and stopped through ABP's module."""
    import socket

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    grpc, control = free_port(), free_port()
    monkeypatch.setenv("TRANSFERD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRANSFERD_ADDR", f"127.0.0.1:{grpc}")
    monkeypatch.setenv("TRANSFERD_CONTROL_ADDR", f"127.0.0.1:{control}")
    monkeypatch.setattr(harness, "bin_dir", _live_build)
    client._ops.update(at=0.0, url="", ops=[])
    try:
        started = harness.start_daemon(wait_s=120)
        assert started["running"]
        assert len(client.operations()) > 70
        client.call("account.create_identity", {"display_name": "ABP live test"})
        client.call("contacts.add_contact", {"public_key": "ef" * 32, "name": "Nobody", "address": "127.0.0.1:9"})
        sent = client.call("messages.send_text", {"contact_id": "ef" * 32, "text": "held in the outbox"})
        assert sent["status"] == "pending"
        s = harness.summary()
        assert s["running"] and s["contacts"] == 1 and s["identity"]["display_name"] == "ABP live test"
        with pytest.raises(DaemonError) as e:
            client.call("contacts.add_contact", {"public_key": "ef" * 32, "name": "Nobody", "address": "not an address"})
        assert e.value.status == 400
    finally:
        harness.stop_daemon()
    assert client.find() is None
