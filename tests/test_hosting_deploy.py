"""Publishing sites to real servers (bot/hosting/deploy.py): an SSH server (asyncssh, running each command in a POSIX
shell, as a VPS does) reached by OpenSSH's own `ssh` with a key, and an FTP server (pyftpdlib) in temporary folders.
Releases, the `current` switch, keeping five releases, rollback, incremental FTP uploads and deleting extras."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from bot.hosting.store import HostingError

BASH = shutil.which("bash") if sys.platform != "win32" else next(
    (p for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe") if Path(p).exists()), None)


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


def _posix(p: Path) -> str:
    """A path as the server's shell sees it (Git's bash on Windows: /c/...)."""
    if sys.platform != "win32":
        return str(p)
    s = str(p.resolve()).replace("\\", "/")
    return f"/{s[0].lower()}{s[2:]}"


def _site(folder: Path, n: int) -> Path:
    (folder / "css").mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(f"<h1>release {n}</h1>")
    (folder / "css" / "site.css").write_text("body{}")
    (folder / ".git").mkdir(exist_ok=True)
    (folder / ".git" / "HEAD").write_text("not published")
    return folder


# ---- SSH ------------------------------------------------------------------------------------------------------------ #

@pytest.fixture
def ssh_server(tmp_path):
    asyncssh = pytest.importorskip("asyncssh")
    if not BASH or not (shutil.which("ssh")):
        pytest.skip("needs OpenSSH's ssh and a POSIX shell (bash)")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_file = tmp_path / "id_ed25519"
    key_file.write_bytes(client_key.export_private_key())
    if sys.platform == "win32":                       # OpenSSH refuses a key others can read: owner only
        import subprocess
        subprocess.run(["icacls", str(key_file), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F"],
                       capture_output=True, check=True)
    else:
        key_file.chmod(0o600)
    env = {**os.environ, "MSYS": "winsymlinks:nativestrict"}     # ln -s makes real symlinks, as on Linux

    async def run(process):
        local = await asyncio.create_subprocess_exec(BASH, "-c", process.command or "true", stdin=asyncio.subprocess.PIPE,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                     env=env, cwd=str(tmp_path))
        await process.redirect(stdin=local.stdin, stdout=local.stdout, stderr=local.stderr)
        process.exit(await local.wait())
        await process.wait_closed()

    port = _free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def serve():
        asyncio.set_event_loop(loop)

        async def start():
            holder["server"] = await asyncssh.create_server(
                asyncssh.SSHServer, "127.0.0.1", port, server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
                authorized_client_keys=asyncssh.import_authorized_keys(client_key.export_public_key().decode()),
                process_factory=run, encoding=None)
            ready.set()
        loop.run_until_complete(start())
        loop.run_forever()
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    assert ready.wait(20)
    yield {"port": port, "key": key_file, "user": os.environ.get("USERNAME") or os.environ.get("USER") or "abp"}

    def close():
        holder["server"].close()
        loop.stop()
    loop.call_soon_threadsafe(close)
    th.join(10)


def test_ssh_publish_releases_and_rollback(hosting, tmp_path, ssh_server):
    from bot.hosting import accounts, deploy
    www = tmp_path / "www"
    acc = accounts.add("ssh", "Test VPS", {"host": "127.0.0.1", "port": str(ssh_server["port"]), "user": ssh_server["user"],
                                           "key_path": str(ssh_server["key"]), "web_root": _posix(www),
                                           "known_hosts": str(tmp_path / "known_hosts")})
    v = accounts.verify(acc["id"])
    assert v["ok"] and "no Caddy yet" in v["note"] and "no passwordless sudo" in v["note"], v
    assert (tmp_path / "known_hosts").read_text().startswith("[127.0.0.1]")      # not the person's own known_hosts
    site = {"id": "blog", "name": "Blog", "domains": []}
    releases = []
    for n in range(7):
        res = deploy.publish(site, _site(tmp_path / f"src{n}", n), {"account": acc["id"]})
        assert res["path"] == f"{_posix(www)}/blog/current"
        releases.append(res["release"])
        time.sleep(1.05)                                  # releases are named by the second
    current = www / "blog" / "current"
    assert (current / "index.html").read_text() == "<h1>release 6</h1>" and (current / "css" / "site.css").exists()
    assert not (current / ".git").exists()                # never published
    kept = sorted(p.name for p in (www / "blog" / "releases").iterdir())
    assert kept == releases[-5:] or kept == releases[-6:]     # the last five (plus the one being switched to)
    prev = deploy.ssh_rollback(site, accounts.get(acc["id"]))
    assert prev == releases[-2] and (current / "index.html").read_text() == "<h1>release 5</h1>"
    assert "uname" not in deploy.ssh_run(accounts.get(acc["id"]), "echo hi; uname -s")
    with pytest.raises(HostingError, match=r"failed on the server \(3\)"):
        deploy.ssh_run(accounts.get(acc["id"]), "exit 3")
    bad = accounts.add("ssh", "Wrong key", {"host": "127.0.0.1", "port": str(ssh_server["port"]), "user": "x",
                                            "key_path": str(tmp_path / "missing-key"), "known_hosts": str(tmp_path / "known_hosts")})
    with pytest.raises(HostingError, match="cannot connect over SSH"):
        deploy.ssh_check(accounts.get(bad["id"]))


def test_ssh_rollback_needs_an_earlier_release(hosting, tmp_path, ssh_server):
    from bot.hosting import accounts, deploy
    acc = accounts.add("ssh", "One", {"host": "127.0.0.1", "port": str(ssh_server["port"]), "user": ssh_server["user"],
                                      "key_path": str(ssh_server["key"]), "web_root": _posix(tmp_path / "www"),
                                      "known_hosts": str(tmp_path / "kh")})
    site = {"id": "solo", "name": "Solo", "domains": []}
    deploy.ssh_publish(site, _site(tmp_path / "s", 1), accounts.get(acc["id"]), lambda m: None)
    with pytest.raises(HostingError, match="no earlier release"):
        deploy.ssh_rollback(site, accounts.get(acc["id"]))


# ---- FTP ------------------------------------------------------------------------------------------------------------ #

@pytest.fixture
def ftp_server(tmp_path):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    root = tmp_path / "ftp-root"
    (root / "public_html").mkdir(parents=True)
    auth = DummyAuthorizer()
    auth.add_user("site", "s3cret-for-tests", str(root), perm="elradfmwMT")
    handler = type("H", (FTPHandler,), {"authorizer": auth, "banner": "ABP test FTP"})
    server = FTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.2}, daemon=True)
    th.start()
    yield {"port": server.address[1], "root": root}
    server.close_all()
    th.join(5)


def test_ftp_publishes_only_what_changed_and_removes_extras(hosting, tmp_path, ftp_server):
    from bot.hosting import accounts, deploy
    acc = accounts.add("ftp", "Shared host", {"host": "127.0.0.1", "port": str(ftp_server["port"]), "user": "site",
                                              "password": "s3cret-for-tests", "tls": "no"})
    v = accounts.verify(acc["id"])
    assert v["ok"] and "ABP test FTP" in v["note"], v
    site = {"id": "shop", "name": "Shop", "domains": []}
    src = _site(tmp_path / "src", 1)
    logs = []
    first = deploy.publish(site, src, {"account": acc["id"]}, logs.append)
    assert first == {"uploaded": 2, "unchanged": 0, "removed": 0, "remote_dir": "public_html"}
    pub = ftp_server["root"] / "public_html"
    assert (pub / "index.html").read_text() == "<h1>release 1</h1>" and (pub / "css" / "site.css").exists()
    (src / "index.html").write_text("<h1>release two!</h1>")          # a different size: sent again
    (pub / "old.html").write_text("stale")
    again = deploy.publish(site, src, {"account": acc["id"], "delete_extra": True})
    assert again == {"uploaded": 1, "unchanged": 1, "removed": 1, "remote_dir": "public_html"}
    assert (pub / "index.html").read_text() == "<h1>release two!</h1>" and not (pub / "old.html").exists()
    assert "1 file(s) uploaded" not in logs[0] and "2 file(s) uploaded" in logs[0]
    tls = accounts.add("ftp", "Wants TLS", {"host": "127.0.0.1", "port": str(ftp_server["port"]), "user": "site",
                                            "password": "s3cret-for-tests"})
    with pytest.raises(HostingError, match="FTPS"):                  # FTPS by default; this server has no TLS
        deploy.ftp_check(accounts.get(tls["id"]))
    wrong = accounts.add("ftp", "Wrong pw", {"host": "127.0.0.1", "port": str(ftp_server["port"]), "user": "site",
                                             "password": "nope", "tls": "no"})
    assert not accounts.verify(wrong["id"])["ok"]
    with pytest.raises(HostingError, match="no files"):
        deploy.files_of((tmp_path / "empty"))  if (tmp_path / "empty").mkdir() is None else None


# ---- the hosting service: builds, targets, going live ---------------------------------------------------------------- #

def _built_site(service, tmp_path: Path, **extra) -> dict:
    src = tmp_path / "app"
    src.mkdir()
    cmd = (f'"{sys.executable}" -c "import pathlib; d = pathlib.Path(\'dist\'); d.mkdir(exist_ok=True); '
           f'(d / \'index.html\').write_text(\'<h1>built</h1>\')"')
    return service.create({"name": "Built", "kind": "static", "build": {"command": cmd, "cwd": str(src), "output": "dist"}, **extra})


def test_build_then_publish_to_saved_targets(hosting, tmp_path, ftp_server):
    from bot.hosting import accounts, service
    acc = accounts.add("ftp", "Host", {"host": "127.0.0.1", "port": str(ftp_server["port"]), "user": "site",
                                       "password": "s3cret-for-tests", "tls": "no"})
    s = _built_site(service, tmp_path)
    with pytest.raises(HostingError, match="no deploy targets"):
        service.publish(s["id"])
    logs = []
    out = service.publish(s["id"], acc["id"], logs.append)
    assert out[0]["ok"] and out[0]["uploaded"] == 1 and any(m.startswith("building:") for m in logs)
    assert (ftp_server["root"] / "public_html" / "index.html").read_text() == "<h1>built</h1>"
    site = service.get(s["id"])
    assert site["targets"] == [{"account": acc["id"]}] and site["root"].endswith("dist")       # the edge serves the build
    again = service.publish(s["id"])                                       # every saved target
    assert again[0]["ok"] and again[0]["unchanged"] == 1
    assert [h["action"] for h in service.get(s["id"])["history"]] == ["publish", "publish"]
    assert (hosting / "logs" / f"build-{s['id']}.log").exists()
    broken = service.create({"name": "Broken", "kind": "static", "build": {"command": f'"{sys.executable}" -c "raise SystemExit(4)"',
                                                                              "cwd": str(tmp_path)}})
    with pytest.raises(HostingError, match=r"build failed \(4\)"):
        service.build(broken["id"])
    nothing = service.create({"name": "Nothing", "kind": "static", "build": {"cwd": str(tmp_path), "output": "nope"}})
    with pytest.raises(HostingError, match="did not produce"):
        service.build(nothing["id"])
    proxy = service.create({"name": "Api", "kind": "proxy", "upstream": "http://127.0.0.1:9"})
    with pytest.raises(HostingError, match="only static sites"):
        service.build(proxy["id"])
    gone = service.publish(s["id"], accounts.add("ftp", "Down", {"host": "127.0.0.1", "port": str(_free_port()), "user": "x",
                                                                 "password": "y", "tls": "no"})["id"])
    assert not gone[0]["ok"] and "FTP" in gone[0]["error"]


def test_go_live_on_a_server_over_ssh(hosting, tmp_path, ssh_server):
    from bot.hosting import accounts, service
    acc = accounts.add("ssh", "VPS", {"host": "127.0.0.1", "port": str(ssh_server["port"]), "user": ssh_server["user"],
                                      "key_path": str(ssh_server["key"]), "web_root": _posix(tmp_path / "www"),
                                      "known_hosts": str(tmp_path / "kh")})
    s = _built_site(service, tmp_path)
    steps = [p["step"] for p in service.plan(s["id"], "server", acc["id"])]
    assert steps == ["build", "publish", "dns"]
    res = service.go_live(s["id"], "server", acc["id"])
    assert res["ok"], res
    assert (tmp_path / "www" / s["id"] / "current" / "index.html").read_text() == "<h1>built</h1>"
    assert service.get(s["id"])["exposure"] == {"mode": "server", "account": acc["id"]}
    # with a domain, Caddy is set up on the server, which needs passwordless sudo: this server has none
    named = service.create({"name": "Named", "kind": "static", "root": str(_site(tmp_path / "n", 1)), "domains": "named.example.com"})
    res = service.go_live(named["id"], "server", acc["id"])
    assert not res["ok"] and res["steps"][-1]["step"] == "publish" and not res["steps"][-1]["ok"]
    assert service.get(named["id"])["history"][-1]["action"] == "go-live"


def test_plans_go_live_on_the_lan_and_background_ticks(hosting, tmp_path):
    from bot.hosting import service
    s = service.create({"name": "Lan", "kind": "static", "root": str(_site(tmp_path / "l", 1)), "domains": "lan.localhost"})
    for mode, account, msg in (("cloudflare-tunnel", None, "Cloudflare account"), ("server", None, "SSH account"),
                               ("provider", None, "provider account"), ("moon", None, "the mode is one of")):
        with pytest.raises(HostingError, match=msg):
            service.plan(s["id"], mode, account)
    assert [p["step"] for p in service.plan(s["id"], "port-forward")] == ["edge", "dns", "upnp", "certificate", "check"]
    assert [p["step"] for p in service.plan(s["id"], "tailscale-funnel")] == ["edge", "funnel", "check"]
    with pytest.raises(HostingError, match="unknown setting"):
        service.set_settings({"colour": "red"})
    with pytest.raises(HostingError, match="engine"):
        service.set_settings({"engine": "nginx"})
    with pytest.raises(HostingError, match="0-65535"):
        service.set_settings({"http_port": 70000})
    service.set_settings({"http_port": _free_port(), "https_port": 0, "bind": "127.0.0.1", "autostart_edge": True, "ddns": True})
    try:
        res = service.go_live(s["id"], "lan")
        assert res["ok"] and [r["step"] for r in res["steps"]] == ["edge", "info", "check"] and "lan.localhost" in res["steps"][1]["note"]
        st = service.edge_status()
        assert st["running"] and "lan.localhost" in st["health"]["hosts"]
        service.edge_stop()
        service._last.update({"edge": 0.0, "ddns": 0.0, "renew": 0.0})
        service.tick()                                  # a site is exposed and the edge is down: started again
        assert service.edge_status()["running"]
        assert service.ddns_tick() == []                # no site is served by address
        assert service.check(s["id"]) == {"site": s["id"], "names": [], "ok": True}     # .localhost names are not checked
    finally:
        service.edge_stop()
    assert service.public({**service.get(s["id"]), "auth": {"user": "u", "password_hash": "h"}})["auth"] == {"user": "u"}
    assert service.remove(s["id"]) and service.sites() == {}


# ---- providers: Netlify and Vercel (stand-ins of their deploy APIs) --------------------------------------------------- #

def _netlify(state: dict):
    import hashlib as _h
    import json as _j

    import httpx

    def h(req):
        p = req.url.path.removeprefix("/api/v1")
        state["calls"].append(f"{req.method} {p}")
        if req.headers["authorization"] != "Bearer unused":
            return httpx.Response(401, json={"code": 401, "message": "Access Denied"})
        if p == "/sites" and req.method == "GET":
            return httpx.Response(200, json=[{"id": "s1", "name": "abp-shop"}] if state.get("site") else [])
        if p == "/sites":
            state["site"] = _j.loads(req.content)["name"]
            return httpx.Response(201, json={"id": "s1", "name": state["site"]})
        if p == "/sites/s1/deploys":
            files = _j.loads(req.content)["files"]
            need = [sha for sha in files.values() if sha not in state["have"]]
            state["pending"] = need
            return httpx.Response(200, json={"id": "d1", "required": need})
        if p.startswith("/deploys/d1/files/"):
            sha = _h.sha1(req.content).hexdigest()
            assert sha in state["pending"]
            state["have"].add(sha)
            return httpx.Response(200, json={})
        if p == "/deploys/d1":
            return httpx.Response(200, json={"state": "ready", "ssl_url": "https://abp-shop.netlify.app"})
        if p == "/sites/s1" and req.method == "PATCH":
            state["custom_domain"] = _j.loads(req.content)["custom_domain"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "Not Found"})
    return h


def test_netlify_uploads_only_files_it_lacks_and_connects_the_domain(hosting, tmp_path, monkeypatch):
    import httpx

    from bot.hosting import accounts, deploy, service
    state = {"calls": [], "have": set()}
    monkeypatch.setattr(deploy, "_TRANSPORT", httpx.MockTransport(_netlify(state)))
    acc = accounts.add("netlify", "nl", {"token": "unused"})
    s = service.create({"name": "Shop", "kind": "static", "root": str(_site(tmp_path / "s", 1)), "domains": "shop.localhost"})
    res = service.go_live(s["id"], "provider", acc["id"])
    assert res["ok"], res
    assert [r["step"] for r in res["steps"]] == ["publish", "provider-domain", "check"]
    assert res["steps"][0]["note"] == "https://abp-shop.netlify.app" and "CNAME to abp-shop.netlify.app" in res["steps"][1]["note"]
    assert state["site"] == "abp-shop" and len(state["have"]) == 2
    assert service.get(s["id"])["targets"][0]["site_id"] == "s1"       # remembered: no lookup next time
    state["calls"].clear()
    again = service.publish(s["id"])
    assert again[0]["ok"] and again[0]["uploaded"] == 0 and not any(c.startswith("GET /sites?") for c in state["calls"])
    r = deploy.netlify_publish(service.get(s["id"]), Path(service.get(s["id"])["root"]), accounts.get(acc["id"]),
                               {"site_id": "s1", "set_domain": True}, lambda m: None)
    assert r["uploaded"] == 0 and state["custom_domain"] == "shop.localhost"
    bad = accounts.add("netlify", "bad", {"token": "wrong"})
    with pytest.raises(HostingError):
        deploy.publish(service.get(s["id"]), tmp_path / "s", {"account": bad["id"]})


def test_vercel_uploads_by_sha_and_reports_a_failed_build(hosting, tmp_path, monkeypatch):
    import hashlib as _h
    import json as _j

    import httpx

    from bot.hosting import accounts, deploy
    seen = {"files": set(), "fail": False}

    def h(req):
        assert req.url.params.get("teamId") == "team_1"
        if req.url.path == "/v2/files":
            assert _h.sha1(req.content).hexdigest() == req.headers["x-vercel-digest"]
            seen["files"].add(req.headers["x-vercel-digest"])
            return httpx.Response(200, json={})
        if req.url.path == "/v13/deployments":
            body = _j.loads(req.content)
            assert {f["sha"] for f in body["files"]} == seen["files"] and body["target"] == "production"
            return httpx.Response(200, json={"id": "dpl_1", "readyState": "ERROR" if seen["fail"] else "READY",
                                             "url": "abp-blog.vercel.app", "errorMessage": "build failed"})
        return httpx.Response(404, json={"error": {"message": "nope"}})
    monkeypatch.setattr(deploy, "_TRANSPORT", httpx.MockTransport(h))
    acc = accounts.get(accounts.add("vercel", "vc", {"token": "unused", "team_id": "team_1"})["id"])
    site = {"id": "blog", "name": "Blog", "domains": []}
    r = deploy.vercel_publish(site, _site(tmp_path / "v", 1), acc, {}, lambda m: None)
    assert r == {"url": "https://abp-blog.vercel.app", "deploy_id": "dpl_1", "project": "abp-blog"} and len(seen["files"]) == 2
    seen["fail"] = True
    with pytest.raises(HostingError, match="Vercel deployment ERROR: build failed"):
        deploy.vercel_publish(site, tmp_path / "v", acc, {}, lambda m: None)
    with pytest.raises(HostingError, match="cannot receive deployments"):
        deploy.publish(site, tmp_path / "v", {"account": accounts.add("desec", "d", {"token": "unused"})["id"]})
