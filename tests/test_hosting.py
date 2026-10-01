"""bot/hosting: accounts (secrets sealed, never listed), DNS record-set reconciliation, sites and their validation,
go-live plans, the edge as a real process serving static files / a proxied app / a WebSocket / redirects over http and
https (SNI), ACME's JWS and its refusals, Caddy blocks, the API and the agents' tools.

Provider APIs (Cloudflare, DigitalOcean, deSEC, Netlify...) cannot be reached without a person's account, so their
request shapes are checked against small stateful stand-ins (httpx.MockTransport) that behave like those APIs."""
from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import threading
import time
from pathlib import Path

import httpx
import pytest

from bot.hosting.store import HostingError


@pytest.fixture
def hosting(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_HOSTING_DIR", str(tmp_path / "hosting"))
    monkeypatch.setenv("ABP_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("ABP_VAULT_KEY", raising=False)
    return tmp_path / "hosting"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- accounts ------------------------------------------------------------------------------------------------------

def test_accounts_seal_secrets_and_never_list_them(hosting):
    from bot.hosting import accounts
    a = accounts.add("cloudflare", "cf main", {"api_token": "unused", "account_id": "abc"})
    assert a["secrets_set"] == ["api_token"] and a["settings"] == {"account_id": "abc"} and "dns" in a["caps"]
    raw = (hosting / "accounts.json").read_text(encoding="utf-8")
    assert "unused" not in raw                                   # sealed on disk
    acc = accounts.get("cf main")                                # by name too
    assert accounts.secret(acc, "api_token") == "unused" and accounts.setting(acc, "account_id") == "abc"
    assert "unused" not in json.dumps(accounts.listing())
    with pytest.raises(HostingError, match="already named"):
        accounts.add("cloudflare", "CF MAIN", {"api_token": "unused"})
    with pytest.raises(HostingError, match="needs api_token"):
        accounts.add("cloudflare", "other", {})
    with pytest.raises(HostingError, match="no field"):
        accounts.add("ftp", "x", {"host": "h", "user": "u", "password": "unused", "bogus": 1})
    accounts.edit(a["id"], {"account_id": "", "api_token": ""})  # optional cleared, empty secret keeps the old one
    acc = accounts.get(a["id"])
    assert accounts.setting(acc, "account_id") == "" and accounts.secret(acc, "api_token") == "unused"
    assert accounts.listing("vps") == [] and len(accounts.listing("tunnel")) == 1
    assert accounts.remove(a["id"]) and accounts.listing() == []


# ---- DNS -----------------------------------------------------------------------------------------------------------

def test_names_are_made_full_and_relative_and_records_checked():
    from bot.hosting import dns
    assert dns.fqdn("www", "Example.com.") == "www.example.com"
    assert dns.fqdn("@", "example.com") == dns.fqdn("example.com.", "example.com") == "example.com"
    assert dns.fqdn("a.b.example.com", "example.com") == "a.b.example.com"
    assert dns.relative("www.example.com", "example.com") == "www" and dns.relative("example.com", "example.com") == ""
    assert dns.check_record("www.example.com", "a", ["1.2.3.4"]) == "A"
    for bad in (("x.example.com", "A", ["nope"]), ("x.example.com", "CNAME", ["a", "b"]), ("bad name", "A", ["1.2.3.4"]),
                ("x.example.com", "PTR", ["x"]), ("x.example.com", "A", [])):
        with pytest.raises(HostingError):
            dns.check_record(*bad)


class FakeCloudflare:
    """Cloudflare's v4 zones and dns_records endpoints, in memory, paged like the real API."""

    def __init__(self):
        self.zones = [{"id": "z1", "name": "example.com", "status": "active"}]
        self.records: dict[str, dict] = {}
        self.n = 0
        self.calls: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer unused"
        path = req.url.path.removeprefix("/client/v4")
        self.calls.append(f"{req.method} {path}")
        page = int(req.url.params.get("page", 1))
        if path == "/user/tokens/verify":
            return httpx.Response(200, json={"result": {"status": "active"}})
        if path == "/zones":
            return httpx.Response(200, json={"result": self.zones, "result_info": {"total_pages": 1}})
        m = re.fullmatch(r"/zones/z1/dns_records(?:/(\w+))?", path)
        assert m, path
        rid = m.group(1)
        if req.method == "GET":
            recs = list(self.records.values())
            per = 2                                      # small pages, so paging is exercised
            return httpx.Response(200, json={"result": recs[(page - 1) * per: page * per],
                                             "result_info": {"total_pages": max(1, -(-len(recs) // per))}})
        if req.method == "POST":
            self.n += 1
            body = json.loads(req.content)
            self.records[f"r{self.n}"] = {"id": f"r{self.n}", **body}
            return httpx.Response(200, json={"result": self.records[f"r{self.n}"]})
        if req.method == "PUT":
            self.records[rid] = {"id": rid, **json.loads(req.content)}
            return httpx.Response(200, json={"result": self.records[rid]})
        if req.method == "DELETE":
            self.records.pop(rid)
            return httpx.Response(200, json={"result": {"id": rid}})
        raise AssertionError(req)


def test_cloudflare_record_sets_reconcile_to_exactly_the_values(hosting):
    from bot.hosting import accounts, dns
    acc = accounts.add("cloudflare", "cf", {"api_token": "unused"})
    fake = FakeCloudflare()
    p = dns.provider(acc["id"], transport=httpx.MockTransport(fake))
    assert "token active" in p.verify() and p.zone_for("a.b.example.com") == "example.com"
    with pytest.raises(HostingError, match="not in any zone"):
        p.zone_for("example.org")
    p.set("example.com", "www", "A", ["1.1.1.1", "2.2.2.2"], ttl=300)
    p.set("example.com", "@", "TXT", ["v=spf1 -all"])
    sets = {(s["name"], s["type"]): s for s in p.records("example.com")}
    assert sorted(sets[("www.example.com", "A")]["values"]) == ["1.1.1.1", "2.2.2.2"]
    assert sets[("example.com", "TXT")]["values"] == ["v=spf1 -all"]
    # one value kept, one replaced in place (an update, not delete + create)
    fake.calls.clear()
    p.set("example.com", "www", "A", ["1.1.1.1", "3.3.3.3"])
    assert [c for c in fake.calls if not c.startswith("GET")] == ["PUT /zones/z1/dns_records/r2"]
    # down to one value: the extra record removed
    p.set("example.com", "www.example.com", "A", ["3.3.3.3"])
    assert [s["values"] for s in p.records("example.com") if s["type"] == "A"] == [["3.3.3.3"]]
    # a CNAME replaces the name's address records (they cannot coexist)
    p.set("example.com", "www", "CNAME", ["abc.cfargotunnel.com"], ttl=1, proxied=True)
    www = [s for s in p.records("example.com") if s["name"] == "www.example.com"]
    assert [(s["type"], s["values"], s.get("proxied")) for s in www] == [("CNAME", ["abc.cfargotunnel.com"], True)]
    assert p.delete("example.com", "www", "CNAME") == 1 and p.delete("example.com", "www", "CNAME") == 0


def test_digitalocean_and_desec_speak_their_own_record_formats(hosting):
    from bot.hosting import accounts, dns
    seen = []

    def do(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "GET":
            return httpx.Response(200, json={"domain_records": [
                {"id": 7, "type": "CNAME", "name": "blog", "data": "host", "ttl": 1800},
                {"id": 8, "type": "MX", "name": "@", "data": "mail.example.com.", "priority": 5, "ttl": 1800}], "links": {}})
        return httpx.Response(201 if req.method == "POST" else 204, json={} if req.method == "POST" else None)

    p = dns.provider(accounts.add("digitalocean", "do", {"token": "unused"})["id"], transport=httpx.MockTransport(do))
    recs = {(s["name"], s["type"]): s["values"] for s in p.records("example.com")}
    assert recs == {("blog.example.com", "CNAME"): ["host.example.com"], ("example.com", "MX"): ["5 mail.example.com"]}
    p.set("example.com", "www", "CNAME", ["target.example.net"])
    post = [s for s in seen if s[0] == "POST"][0]
    assert post[1] == "/v2/domains/example.com/records" and post[2]["name"] == "www" and post[2]["data"] == "target.example.net."

    sent = []

    def desec(req: httpx.Request) -> httpx.Response:
        sent.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(200, json=[])
    q = dns.provider(accounts.add("desec", "desec", {"token": "unused"})["id"], transport=httpx.MockTransport(desec))
    q.set("example.com", "@", "TXT", ["hello world"], ttl=60)
    q.delete("example.com", "example.com", "TXT")
    assert sent[0] == ("PUT", "/api/v1/domains/example.com/rrsets/", [{"subname": "", "type": "TXT", "ttl": 3600, "records": ['"hello world"']}])
    assert sent[1][:2] == ("DELETE", "/api/v1/domains/example.com/rrsets/@/TXT/")


# ---- sites and plans -------------------------------------------------------------------------------------------------

def test_sites_are_validated_and_plans_follow_the_mode(hosting, tmp_path):
    from bot.hosting import accounts, service
    www = tmp_path / "www"
    www.mkdir()
    s = service.create({"name": "My Blog", "kind": "static", "root": str(www), "domains": "Blog.Example.com, https://www.blog.example.com/"})
    assert s["id"] == "my-blog" and s["domains"] == ["blog.example.com", "www.blog.example.com"]
    assert service.create({"name": "My Blog", "kind": "redirect", "redirect_to": "https://x.org"})["id"] == "my-blog-2"
    for bad, msg in (({"kind": "static", "root": str(tmp_path / "missing")}, "not a folder"),
                     ({"kind": "proxy", "upstream": "localhost:3000"}, "app's address"),
                     ({"kind": "static", "root": str(www), "domains": "blog.example.com"}, "already belongs"),
                     ({"kind": "static", "root": str(www), "domains": "not a domain!"}, "not a domain"),
                     ({"kind": "ftp"}, "kind is one of")):
        with pytest.raises(HostingError, match=msg):
            service.create(bad)
    service.edit("my-blog", {"password": "pw", "spa": True})
    raw = service.get("my-blog")
    assert raw["auth"]["password_hash"].startswith("pbkdf2$") and service.public(raw)["auth"] == {"user": "admin"}
    steps = lambda *a: [x["step"] for x in service.plan("my-blog", *a)]  # noqa: E731
    assert steps("port-forward") == ["edge", "dns", "upnp", "certificate", "check"]
    assert steps("direct") == ["edge", "dns", "certificate", "check"]
    assert steps("tailscale-funnel") == ["edge", "funnel", "check"]
    with pytest.raises(HostingError, match="Cloudflare account"):
        service.plan("my-blog", "cloudflare-tunnel")
    cf = accounts.add("cloudflare", "cf", {"api_token": "unused", "account_id": "a"})
    assert steps("cloudflare-tunnel", cf["id"]) == ["edge", "tunnel", "tunnel-run", "check"]
    nl = accounts.add("netlify", "nl", {"token": "unused"})
    assert steps("provider", nl["id"]) == ["publish", "provider-domain", "check"]
    service.edit("my-blog", {"build": {"command": "", "cwd": str(www), "output": "."}})
    assert steps("server", "x")[0] == "build"
    assert service.build("my-blog") == www.resolve()
    assert service.remove("my-blog") and "my-blog" not in service.sites()


# ---- the edge, for real ------------------------------------------------------------------------------------------------

def test_the_edge_serves_static_proxy_websocket_and_redirect_over_http_and_https(hosting, tmp_path):
    import uvicorn
    import websockets.sync.client as wsc
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    from bot.hosting import service

    site = tmp_path / "site"
    (site / "assets").mkdir(parents=True)
    (site / "index.html").write_text("<h1>home</h1>" + "x" * 2000)
    (site / "about.html").write_text("<h1>about</h1>")
    (site / "assets" / "app.0123abcd.js").write_text("1")

    async def hello(req):
        return JSONResponse({"path": req.url.path, "xff": req.headers.get("x-forwarded-for"), "body": (await req.body()).decode()})

    async def echo(ws):
        await ws.accept()
        await ws.send_text("echo:" + await ws.receive_text())
        await ws.close()
    app_port, http_port, https_port = _free_port(), _free_port(), _free_port()
    up = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/{p:path}", hello, methods=["GET", "POST"]), WebSocketRoute("/ws", echo)]),
                                       port=app_port, log_level="warning"))
    threading.Thread(target=up.run, daemon=True).start()
    service.set_settings({"http_port": http_port, "https_port": https_port, "bind": "127.0.0.1", "autostart_edge": False})
    service.create({"name": "Blog", "kind": "static", "root": str(site), "domains": "blog.localhost", "spa": True})
    service.create({"name": "App", "kind": "proxy", "upstream": f"http://127.0.0.1:{app_port}", "domains": "app.localhost",
                    "user": "me", "password": "pw1"})
    service.create({"name": "Old", "kind": "redirect", "redirect_to": "https://blog.localhost", "domains": "old.localhost"})
    assert service.edge_start()["running"]
    try:
        base = f"http://127.0.0.1:{http_port}"
        for _ in range(50):
            try:
                if httpx.get(f"{base}/.well-known/abp-edge", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        c = httpx.Client(timeout=10)
        H = lambda h: {"host": h}  # noqa: E731
        r = c.get(f"{base}/", headers=H("blog.localhost"))
        assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and r.headers["cache-control"] == "no-cache"
        assert c.get(f"{base}/about", headers=H("blog.localhost")).text == "<h1>about</h1>"
        assert c.get(f"{base}/deep/route", headers=H("blog.localhost")).text.startswith("<h1>home")      # SPA fallback
        assert "immutable" in c.get(f"{base}/assets/app.0123abcd.js", headers=H("blog.localhost")).headers["cache-control"]
        assert c.get(f"{base}/", headers={**H("blog.localhost"), "range": "bytes=0-3"}).status_code == 206
        with socket.create_connection(("127.0.0.1", http_port)) as raw:
            raw.sendall(b"GET /assets/../../../../etc/passwd HTTP/1.1\r\nHost: blog.localhost\r\nConnection: close\r\n\r\n")
            assert raw.recv(200).split(b"\r\n")[0] == b"HTTP/1.1 404 Not Found"
        assert c.get(f"{base}/x", headers=H("app.localhost")).status_code == 401
        r = c.post(f"{base}/x?q=1", headers=H("app.localhost"), auth=("me", "pw1"), content=b"hi")
        assert r.json() == {"path": "/x", "xff": "127.0.0.1", "body": "hi"}
        r = c.get(f"{base}/p?a=b", headers=H("old.localhost"))
        assert r.status_code == 301 and r.headers["location"] == "https://blog.localhost/p?a=b"
        assert c.get(f"{base}/", headers=H("nobody.example")).status_code == 421
        auth = "Basic " + base64.b64encode(b"me:pw1").decode()
        with wsc.connect("ws://app.localhost/ws", sock=socket.create_connection(("127.0.0.1", http_port)),
                         additional_headers={"Authorization": auth}) as w:
            w.send("hi")
            assert w.recv() == "echo:hi"
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with ctx.wrap_socket(socket.create_connection(("127.0.0.1", https_port)), server_hostname="blog.localhost") as t:
            from cryptography import x509
            assert x509.load_der_x509_certificate(t.getpeercert(binary_form=True)).subject.rfc4514_string() == "CN=blog.localhost"
            t.sendall(b"GET /about HTTP/1.1\r\nHost: blog.localhost\r\nConnection: close\r\n\r\n")
            assert t.recv(100).startswith(b"HTTP/1.1 200")
        service.edit("blog", {"domains": ["blog.localhost", "www.blog.localhost"]})      # no restart needed
        time.sleep(0.2)
        assert c.get(f"{base}/about", headers=H("www.blog.localhost")).status_code == 200
        assert "www.blog.localhost" in service.edge_status()["health"]["hosts"]
    finally:
        assert service.edge_stop()
        up.should_exit = True
    assert not service.edge_status()["running"]


# ---- ACME, Caddy ---------------------------------------------------------------------------------------------------

def _acme_dir(req: httpx.Request) -> httpx.Response:
    if req.method == "HEAD":
        return httpx.Response(200, headers={"Replay-Nonce": "n1"})
    return httpx.Response(200, json={"newNonce": "https://ca.test/nonce", "newAccount": "https://ca.test/acct",
                                     "newOrder": "https://ca.test/order", "meta": {"termsOfService": "https://ca.test/tos"}})


def test_acme_signs_verifiable_jws_and_refuses_without_agreement(hosting):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    from bot.hosting import acme
    c = acme.Client("https://ca.test/dir", ec.generate_private_key(ec.SECP256R1()), transport=httpx.MockTransport(_acme_dir))
    jws = c.sign({"alg": "ES256", "nonce": c._nonce(), "url": "u", "jwk": c.jwk()}, {"a": 1})
    sig = acme._b64u_dec(jws["signature"])
    c.key.public_key().verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                              f"{jws['protected']}.{jws['payload']}".encode(), ec.ECDSA(hashes.SHA256()))
    assert len(c.thumbprint()) == 43
    with pytest.raises(HostingError, match="agree to the certificate authority's terms"):
        c.account(agree_tos=False)
    with pytest.raises(HostingError, match="wildcard name needs the dns-01"):
        acme.issue(["*.example.com"], method="http-01")
    assert acme.certificates() == []


def test_caddy_blocks_for_each_kind():
    from bot.hosting import caddy
    st = caddy.block({"id": "a", "kind": "static", "root": "C:\\www\\a", "spa": True, "domains": ["a.com", "www.a.com"]})
    assert st.startswith("a.com, www.a.com {") and 'root * "C:/www/a"' in st and "/index.html" in st and "file_server" in st
    px = caddy.block({"id": "b", "kind": "proxy", "upstream": "http://127.0.0.1:3000", "domains": ["b.lan"]})
    assert "reverse_proxy http://127.0.0.1:3000" in px and "tls internal" in px
    rd = caddy.block({"id": "c", "kind": "redirect", "redirect_to": "https://x.org/", "domains": ["c.com"], "https": "off"})
    assert rd.startswith("http://c.com {") and "redir https://x.org{uri} permanent" in rd
    with pytest.raises(HostingError):
        caddy.block({"id": "d", "kind": "static", "root": "x", "domains": []})


# ---- API, tools, pages ---------------------------------------------------------------------------------------------

def test_the_api_end_to_end(hosting, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import hosting_api
    app = FastAPI()
    hosting_api.register(app, lambda: None)
    c = TestClient(app)
    provs = c.get("/api/hosting/providers").json()
    assert {"cloudflare", "netlify", "ssh", "ftp", "hetzner", "route53"} <= set(provs)
    assert any(f["secret"] for f in provs["cloudflare"]["fields"])
    r = c.post("/api/hosting/accounts", json={"provider": "ssh", "name": "box", "values": {"host": "192.0.2.10", "user": "me"}, "verify": False})
    assert r.status_code == 200 and r.json()["caps"] == ["server", "deploy"]
    www = tmp_path / "w"
    www.mkdir()
    assert c.post("/api/hosting/sites", json={"name": "S", "kind": "static", "root": str(www), "domains": ["s.example.com"]}).status_code == 200
    assert c.post("/api/hosting/sites", json={"name": "S", "kind": "nope"}).status_code == 400
    assert [s["step"] for s in c.get("/api/hosting/sites/s/plan", params={"mode": "direct"}).json()][:2] == ["edge", "dns"]
    assert c.patch("/api/hosting/sites/s", json={"spa": True}).json()["spa"] is True
    run = c.post("/api/hosting/certs", json={"names": "*.example.com", "method": "http-01"}).json()
    for _ in range(50):
        got = c.get(f"/api/hosting/runs/{run['run']}").json()
        if got["done"]:
            break
        time.sleep(0.1)
    assert got["done"] and "dns-01" in got["error"]
    assert c.get("/api/hosting/runs/nope").status_code == 404
    o = c.get("/api/hosting").json()
    assert [s["id"] for s in o["sites"]] == ["s"] and o["edge"]["running"] is False
    assert c.put("/api/hosting/settings", json={"engine": "nginx"}).status_code == 400
    assert c.post("/api/hosting/servers/box", json={"name": "x", "region": "r", "size": "s"}).status_code == 400   # not a cloud
    assert c.delete("/api/hosting/sites/s").json() == {"removed": True}


def test_the_agents_tools_are_registered_with_approval_for_changes():
    from bot.agent_runtime import toolspec
    from bot.hosting import tools  # noqa: F401
    for name in ("hosting_site", "hosting_go_live", "hosting_publish", "hosting_dns_change", "hosting_server_control"):
        assert toolspec.spec_for(name).origin == "registered" and toolspec.registered_dangerous(name), name
    for name in ("hosting_status", "hosting_network", "hosting_plan", "hosting_check", "hosting_dns", "hosting_server"):
        assert toolspec.spec_for(name).origin == "registered" and not toolspec.registered_dangerous(name), name


ROOT = Path(__file__).resolve().parent.parent


def test_the_page_is_the_same_file_in_both_uis_and_both_pages_have_it():
    a = (ROOT / "bot/dashboard/static/hosting-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/hosting-panel.js").read_text(encoding="utf-8")
    assert a == b, "desktop-app/ui/hosting-panel.js differs from bot/dashboard/static/hosting-panel.js: copy it over"
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="hosting"' in html and 'id="hsp-root"' in html and "hosting-panel.js" in html and 'href="#hosting"' in html


def test_the_cli_parses_every_host_command():
    from abp_cli.__main__ import _parser
    p = _parser()
    for argv in (["host", "status"], ["host", "site", "add", "--name", "x", "--root", "C:/w", "--domains", "a.com"],
                 ["host", "live", "x", "--mode", "cloudflare-tunnel", "--account", "cf"], ["host", "dns", "set", "cf", "a.com", "www", "A", "1.2.3.4"],
                 ["host", "vps", "create", "do", "--name", "w", "--region", "fra1", "--size", "s-1vcpu-1gb"], ["host", "router", "add", "443"]):
        assert p.parse_args(argv).host_cmd == argv[1]
