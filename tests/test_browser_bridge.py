"""The ABP <-> browser-extension bridge: pairing (code and approve flows), key scoping, the WebSocket hub with a fake
extension, RPC deadlines/errors, resume, unpairing, and the server-side sensitive-site policy. Runs against a real
uvicorn server because the bridge accepts only loopback peers with an extension Origin."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets

from bot import browser_bridge as bb, browser_policy, db
from bot.dashboard.server import build_app

TOKEN = "bridge-test-token"
H = {"X-Dashboard-Token": TOKEN}
EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
ORIGIN = f"chrome-extension://{EXT_ID}"


@pytest.fixture
def server(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setattr(bb, "PAIR_MIN_INTERVAL_S", 0.0)
    bb.bridge.connections.clear()
    bb.bridge._orphans.clear()
    bb.pairing = bb.Pairing()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def _pair(base, ext_id=EXT_ID) -> str:
    code = httpx.post(f"{base}/api/browser/pair/code", headers=H).json()["code"]
    r = httpx.post(f"{base}/api/browser/pair/complete", json={"code": code, "browser": "Edge", "version": "0.1"},
                   headers={"Origin": f"chrome-extension://{ext_id}"})
    assert r.status_code == 200, r.text
    return r.json()["key"]


class FakeExtension:
    """Speaks protocol v1 like the real extension: hello, answers requests from a handler table."""

    def __init__(self, base, key, *, origin=ORIGIN, resume=None):
        self.url = base.replace("http", "ws") + "/api/browser/ws"
        self.key, self.origin, self.resume = key, origin, resume
        self.handlers: dict = {}
        self.seen: list[dict] = []
        self.hello: dict = {}
        self.ws = None
        self._task = None

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, origin=self.origin)
        await self.ws.send(json.dumps({"v": 1, "id": "h1", "method": "hello", "params": {
            "protocol": [1], "key": self.key, "ext": {"name": "ABP Bridge", "version": "0.1.0", "browser": "Edge"},
            "capabilities": {"debugger": False}, **({"resume": self.resume} if self.resume else {})}}))
        self.hello = json.loads(await asyncio.wait_for(self.ws.recv(), 5))
        if "result" in self.hello:
            self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
        await self.ws.close()

    async def _loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self.seen.append(msg)
                if "method" in msg and msg.get("id"):
                    asyncio.create_task(self._answer(msg))          # concurrent, like the real service worker
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _answer(self, msg):
        h = self.handlers.get(msg["method"])
        try:
            if h is None:
                await self.ws.send(json.dumps({"v": 1, "id": msg["id"], "error": {"code": "E_METHOD", "message": "no"}}))
                return
            res = h(msg["params"])
            if asyncio.iscoroutine(res):
                res = await res
            await self.ws.send(json.dumps({"v": 1, "id": msg["id"], "result": res}))
        except bb.BridgeError as e:
            await self.ws.send(json.dumps({"v": 1, "id": msg["id"], "error": e.to_error()}))
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------ pairing
def test_hello_is_public_on_loopback_and_pair_endpoints_need_an_extension_origin(server):
    hello = httpx.get(f"{server}/api/browser/hello").json()
    assert hello["abp"] is True and hello["protocol"] == 1 and len(hello["server_id"]) == 32
    code = httpx.post(f"{server}/api/browser/pair/code", headers=H).json()["code"]
    assert httpx.post(f"{server}/api/browser/pair/complete", json={"code": code}).status_code == 403            # no Origin
    assert httpx.post(f"{server}/api/browser/pair/complete", json={"code": code},
                      headers={"Origin": "https://evil.example"}).status_code == 403                           # a web page
    assert httpx.post(f"{server}/api/browser/pair/code").status_code in (401, 503)                              # needs the desktop token


def test_code_pairing_is_single_use_expires_and_locks_out_guessing(server, monkeypatch):
    ok = httpx.post(f"{server}/api/browser/pair/code", headers=H).json()
    code = ok["code"]
    bad = "000000" if code != "000000" else "111111"
    for left in (4, 3, 2, 1):
        r = httpx.post(f"{server}/api/browser/pair/complete", json={"code": bad}, headers={"Origin": ORIGIN})
        assert r.status_code == 401 and r.json()["detail"]["data"]["attempts_left"] == left
    httpx.post(f"{server}/api/browser/pair/complete", json={"code": bad}, headers={"Origin": ORIGIN})            # 5th wrong attempt
    r = httpx.post(f"{server}/api/browser/pair/complete", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 401                                                                                # locked out, even with the right code

    key = _pair(server)                                                                                         # a fresh code works once
    assert len(key) > 20
    code2 = httpx.post(f"{server}/api/browser/pair/code", headers=H).json()["code"]
    monkeypatch.setattr(bb.pairing._code, "expires", time.time() - 1)
    assert httpx.post(f"{server}/api/browser/pair/complete", json={"code": code2}, headers={"Origin": ORIGIN}).status_code == 401


def test_approve_flow_hands_the_key_over_exactly_once_and_deny_gives_nothing(server):
    e = {"Origin": ORIGIN}
    req = httpx.post(f"{server}/api/browser/pair/request", json={"browser": "Chrome", "version": "0.1"}, headers=e).json()
    assert httpx.post(f"{server}/api/browser/pair/collect", json=req, headers=e).json() == {"state": "pending"}
    pending = httpx.get(f"{server}/api/browser/pair/pending", headers=H).json()["pending"]
    assert pending[0]["extension_id"] == EXT_ID
    assert httpx.post(f"{server}/api/browser/pair/{req['request_id']}/approve", headers=H).json()["state"] == "approved"
    got = httpx.post(f"{server}/api/browser/pair/collect", json=req, headers=e).json()
    assert got["state"] == "approved" and got["key"] and got["server_id"]
    assert httpx.post(f"{server}/api/browser/pair/collect", json=req, headers=e).json()["state"] == "collected"   # the key is gone
    assert httpx.post(f"{server}/api/browser/pair/collect", json={**req, "nonce": "wrong"}, headers=e).status_code == 401

    req2 = httpx.post(f"{server}/api/browser/pair/request", json={}, headers=e).json()
    httpx.post(f"{server}/api/browser/pair/{req2['request_id']}/deny", headers=H)
    assert httpx.post(f"{server}/api/browser/pair/collect", json=req2, headers=e).json() == {"state": "denied"}


# ------------------------------------------------------------------------------------------ key scoping
def test_an_extension_key_reaches_nothing_but_the_bridge(server):
    key = _pair(server)
    kh = {"X-Dashboard-Token": key}
    for path in ("/api/bots", "/api/overview", "/api/config", "/api/env", "/api/browser/status", "/api/browser/policy",
                 "/api/docker/info", "/api/peers", "/api/keys", "/api/mobile-keys"):
        r = httpx.get(f"{server}{path}", headers=kh)
        assert r.status_code in (401, 403, 404, 405), f"{path} -> {r.status_code}"
    assert httpx.post(f"{server}/api/browser/rpc", json={"method": "tab.list"}, headers=kh).status_code in (401, 403)
    assert httpx.delete(f"{server}/api/browser/browsers/1", headers=kh).status_code in (401, 403)


# ------------------------------------------------------------------------------------------ the WebSocket hub
def test_handshake_rejects_unpaired_keys_wrong_origins_and_old_protocols(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, "not-a-key") as x:
            assert x.hello["error"]["code"] == "E_AUTH"
        async with FakeExtension(server, key, origin="chrome-extension://someoneelse00000000000000000000") as x:
            assert x.hello["error"]["code"] == "E_AUTH"                    # the key was issued to a different extension
        with pytest.raises(Exception):
            async with FakeExtension(server, key, origin="https://evil.example"):
                pass                                                        # a web page can't even open the socket
        ws = await websockets.connect(server.replace("http", "ws") + "/api/browser/ws", origin=ORIGIN)
        await ws.send(json.dumps({"v": 1, "id": "1", "method": "hello", "params": {"protocol": [99], "key": key}}))
        assert json.loads(await ws.recv())["error"]["code"] == "E_PROTOCOL"
        await ws.close()
        async with FakeExtension(server, key) as x:
            assert x.hello["result"]["server_id"] and x.hello["result"]["policy"]["max_tabs"] >= 1

    run(go())


def test_the_server_can_call_the_extension_and_errors_come_back_typed(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key) as x:
            x.handlers["tab.list"] = lambda p: {"tabs": [{"id": 1, "url": "https://example.com/", "title": "Example"}]}

            def stale(p):
                raise bb.BridgeError("E_STALE_REF", "the page changed", retryable=True, hint="take a new snapshot")
            x.handlers["tab.act"] = stale
            async with httpx.AsyncClient() as c:
                r = await c.post(f"{server}/api/browser/rpc", json={"method": "tab.list"}, headers=H)
                assert r.json()["result"]["tabs"][0]["title"] == "Example"
                r = await c.post(f"{server}/api/browser/rpc", json={"method": "tab.act", "params": {"ref": "@e1"}}, headers=H)
                assert r.status_code == 502 and r.json()["detail"]["code"] == "E_STALE_REF" and r.json()["detail"]["retryable"] is True
                r = await c.post(f"{server}/api/browser/rpc", json={"method": "nope.nothing"}, headers=H)
                assert r.json()["detail"]["code"] == "E_METHOD"

    run(go())


def test_no_browser_connected_and_timeouts_are_reported_not_hung(server):
    key = _pair(server)
    r = httpx.post(f"{server}/api/browser/rpc", json={"method": "tab.list"}, headers=H)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "E_NOT_CONNECTED"

    async def go():
        async with FakeExtension(server, key) as x:
            async def slow(p):
                await asyncio.sleep(5)
            x.handlers["tab.wait"] = slow
            async with httpx.AsyncClient(timeout=15) as c:
                t0 = time.time()
                r = await c.post(f"{server}/api/browser/rpc", json={"method": "tab.wait", "deadline_ms": 300}, headers=H)
                assert r.status_code == 504 and r.json()["detail"]["code"] == "E_TIMEOUT" and time.time() - t0 < 4
            assert any(m.get("method") == "cancel" for m in x.seen)                     # the extension was told to stop

    run(go())


def test_the_server_refuses_sensitive_and_internal_urls_before_the_browser_is_asked(server):
    key = _pair(server)

    async def go():
        async with FakeExtension(server, key) as x:
            x.handlers["tab.navigate"] = lambda p: {"ok": True}
            async with httpx.AsyncClient() as c:
                for url in ("https://www.chase.com/", "chrome://settings", "https://accounts.google.com/signin", "file:///etc/passwd",
                            "https://example.com/checkout/payment"):
                    r = await c.post(f"{server}/api/browser/rpc", json={"method": "tab.navigate", "params": {"url": url}}, headers=H)
                    assert r.status_code == 403 and r.json()["detail"]["code"] == "E_SENSITIVE_SITE", url
                ok = await c.post(f"{server}/api/browser/rpc", json={"method": "tab.navigate", "params": {"url": "https://example.com/"}}, headers=H)
                assert ok.status_code == 200
            assert [m["method"] for m in x.seen if m.get("method") == "tab.navigate"] == ["tab.navigate"]   # only the allowed one arrived

    run(go())


def test_a_newer_connection_supersedes_and_unpairing_disconnects(server):
    key = _pair(server)
    key_id = httpx.get(f"{server}/api/browser/status", headers=H).json()["paired"][0]["key_id"]

    async def go():
        async with FakeExtension(server, key) as first:
            async with FakeExtension(server, key) as second:
                await asyncio.sleep(0.3)
                st = httpx.get(f"{server}/api/browser/status", headers=H).json()
                assert len(st["connections"]) == 1 and st["connections"][0]["session"] == second.hello["result"]["session"]
                with pytest.raises(websockets.ConnectionClosed):
                    await asyncio.wait_for(first.ws.recv(), 3)                           # the old one was closed
                assert httpx.delete(f"{server}/api/browser/browsers/{key_id}", headers=H).status_code == 200
                await asyncio.sleep(0.3)
                assert httpx.get(f"{server}/api/browser/status", headers=H).json()["connections"] == []
        async with FakeExtension(server, key) as again:
            assert again.hello["error"]["code"] == "E_AUTH"                              # the key is revoked

    run(go())


def test_an_unanswered_request_is_resent_after_a_reconnect(server):
    key = _pair(server)

    async def go():
        first = await FakeExtension(server, key).__aenter__()
        got = asyncio.Event()
        first.handlers["tab.list"] = lambda p: (_ for _ in ()).throw(asyncio.CancelledError())   # never answers
        async with httpx.AsyncClient(timeout=15) as c:
            call = asyncio.create_task(c.post(f"{server}/api/browser/rpc", json={"method": "tab.list", "deadline_ms": 8000}, headers=H))
            await asyncio.sleep(0.5)
            session = first.hello["result"]["session"]
            await first.ws.close()                                                       # the service worker was killed mid-request
            await asyncio.sleep(0.3)
            async with FakeExtension(server, key, resume={"session": session}) as second:
                second.handlers["tab.list"] = lambda p: {"tabs": ["after-resume"]}
                r = await asyncio.wait_for(call, 10)
                assert r.json()["result"] == {"tabs": ["after-resume"]}
        got.set()

    run(go())


# ------------------------------------------------------------------------------------------ policy
@pytest.mark.parametrize("url,allowed,sensitive", [
    ("https://example.com/", True, False), ("https://en.wikipedia.org/wiki/Cat", True, False),
    ("http://127.0.0.1:8000/app", True, False),
    ("https://www.chase.com/", True, True), ("https://paypal.com/myaccount", True, True), ("https://vault.bitwarden.com/", True, True),
    ("https://console.aws.amazon.com/ec2", True, True), ("https://accounts.google.com/", True, True),
    ("https://shop.example.com/checkout", True, True), ("https://example.com/login?next=/", True, True),
    ("chrome://settings", False, True), ("edge://flags", False, True), ("chrome-extension://abc/page.html", False, True),
    ("file:///C:/secrets.txt", False, True), ("javascript:alert(1)", False, True), ("data:text/html,hi", False, True),
    ("not a url", False, True),
])
def test_url_classification(url, allowed, sensitive):
    v = browser_policy.classify(url)
    assert (v.allowed, v.sensitive) == (allowed, sensitive), v


def test_user_lists_extend_and_trusted_sites_relax_the_path_rule():
    assert browser_policy.classify("https://intranet.corp/", extra_sensitive=["intranet.corp"]).sensitive
    assert not browser_policy.classify("https://my.app/login", trusted_sites=["my.app"]).sensitive
    assert browser_policy.classify("https://my.chase.com/x", trusted_sites=["chase.com"]).sensitive      # trust never unblocks a bank
