"""Installing and running octopus-router from ABP (bot/octopus/router_app.py), with git/npm/node stubbed, and the
Octopus routes that drive it and the Router."""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bot import envfile, integrations, providers
from bot.octopus import router, router_app, sso

D = {"X-Dashboard-Token": "test-token"}


@pytest.fixture
def home(tmp_path, monkeypatch, temp_db):
    store: dict[str, str] = {}
    monkeypatch.setattr(envfile, "get_var", lambda k: store.get(k))
    monkeypatch.setattr(envfile, "set_var", lambda k, v, actor="": store.__setitem__(k, v))
    monkeypatch.setattr(providers, "_module_cache", (0.0, {}))
    monkeypatch.setattr(router_app, "_cfg", lambda: {"dir": str(tmp_path / "router"), "port": 3999})
    cfg: dict = {}
    from bot.config import config

    def set_value(path, value, actor=""):
        node = cfg
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = value
    monkeypatch.setattr(config, "set_value", set_value)
    monkeypatch.setattr(router, "url", lambda: "http://127.0.0.1:3999")
    monkeypatch.setattr(integrations, "frame_src", lambda: cfg.get("integrations", {}).get("frame_src", []))
    monkeypatch.setattr(integrations, "frame_ancestors", lambda: cfg.get("integrations", {}).get("frame_ancestors", []))
    ran = []

    def fake_run(cmd, cwd, timeout=0):
        ran.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            d = tmp_path / "router"
            d.mkdir(parents=True, exist_ok=True)
            (d / ".git").mkdir()
            (d / "client").mkdir()
        if "build" in cmd:
            (tmp_path / "router" / "client" / "dist").mkdir(parents=True, exist_ok=True)
        if cmd[1:2] == ["ci"] and cwd == tmp_path / "router":
            (tmp_path / "router" / "node_modules").mkdir(exist_ok=True)
    monkeypatch.setattr(router_app, "_run", fake_run)
    monkeypatch.setattr(router_app, "node_version", lambda: (24, 11))
    monkeypatch.setattr(router_app, "_npm", lambda: "npm")
    return {"dir": tmp_path / "router", "store": store, "cfg": cfg, "ran": ran}


def test_install_writes_secrets_once_and_wires_both_ways(home):
    router_app._job.update(state="running", log=[])
    router_app._install_job(False)
    assert router_app._job["state"] == "done", router_app._job
    env = router_app._read_env(home["dir"])
    assert len(env["ROUTER_SECRET"]) >= 32 and len(env["ROUTER_OWNER_TOKEN"]) >= 32 and env["PORT"] == "3999"
    key = env["ABP_DASHBOARD_TOKEN"]
    assert integrations.scopes_for(key) == sorted(integrations.PRESETS["octopus-router"]["scopes"])
    assert home["store"][router.TOKEN_VAR] == env["ROUTER_OWNER_TOKEN"]           # ABP holds the Router's token
    assert home["cfg"]["octopus"]["router_url"] == "http://127.0.0.1:3999"
    assert "http://127.0.0.1:3999" in home["cfg"]["integrations"]["frame_src"]
    s = router_app.status()
    assert s["installed"] and s["configured"] and not s["running"] and s["port"] == 3999
    # an update keeps every secret (the Router's stored keys are sealed with ROUTER_SECRET)
    router_app._install_job(True)
    assert router_app._read_env(home["dir"]) == env
    assert ["git", "-C", str(home["dir"]), "pull", "--ff-only", "-q"] in home["ran"]


def test_install_refuses_an_old_node_and_reports_it(home, monkeypatch):
    monkeypatch.setattr(router_app, "node_version", lambda: (20, 1))
    router_app._job.update(state="running", log=[], error="")
    router_app._install_job(False)
    assert router_app._job["state"] == "failed" and "Node.js 22" in router_app._job["error"]
    with pytest.raises(RuntimeError, match="install the Router first"):
        router_app.start()


def test_install_runs_once_at_a_time(home, monkeypatch):
    started = []
    monkeypatch.setattr(router_app, "_install_job", lambda update: started.append(update) or time.sleep(0.3))
    router_app._job.update(state="idle")
    router_app.install()
    router_app.install()             # while the first is running: no second job
    time.sleep(0.5)
    assert started == [False]
    router_app._job.update(state="idle")


def test_the_router_app_and_router_routes(home, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app
    c = TestClient(build_app())
    monkeypatch.setattr(router_app, "install", lambda update=False: {"job": {"state": "running"}, "update": update})
    assert c.post("/api/octopus/router-app/update", headers=D).json()["update"] is True
    assert c.post("/api/octopus/router-app/nope", headers=D).status_code == 404
    assert c.get("/api/octopus/router-app", headers=D).json()["port"] == 3999
    assert c.post("/api/octopus/router-app/start", headers=D).status_code == 409      # not installed
    assert c.get("/api/octopus/router-app").status_code == 401

    home["store"][router.TOKEN_VAR] = "router-owner-token-0123"

    def fake(method, url, json=None, params=None, headers=None, timeout=None):
        path = url.split("3999", 1)[1]
        body = {"/api/usage": {"usage": []}, "/api/keys": {"keys": []}, "/api/runs": {"runs": []},
                "/api/workspaces": {"workspaces": []}, "/api/missions": {"missions": [{"id": "m1"}]},
                "/api/missions/m1": {"mission": {"id": "m1"}}, "/api/settings": {"chat": {}},
                "/api/botplatform/status": {"url": "x"}, "/api/conversations/c1/messages": {"messages": []},
                "/api/route/preview": {"model": "local"}, "/api/runs/r1/confirm": {"ok": True},
                "/api/runs/r1/cancel": {"ok": True}}.get(path)
        if path == "/api/runs" and method == "POST":
            return httpx.Response(200, json={"run": {"id": "r1"}})
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, json={"error": "no"})
    monkeypatch.setattr(router.httpx, "request", fake)
    for what in ("usage", "keys", "runs", "workspaces", "missions", "settings", "botplatform"):
        assert c.get(f"/api/octopus/router/{what}", headers=D).status_code == 200, what
    assert c.get("/api/octopus/router/missions/m1", headers=D).json()["mission"]["id"] == "m1"
    assert c.get("/api/octopus/router/conversations/c1/messages", headers=D).status_code == 200
    assert c.post("/api/octopus/router/route-preview", headers=D, json={"messages": []}).json()["model"] == "local"
    assert c.post("/api/octopus/router/runs", headers=D, json={"prompt": "x"}).json()["run"]["id"] == "r1"
    assert c.post("/api/octopus/router/runs/r1/confirm", headers=D, json={"approve": True}).json()["ok"]
    assert c.post("/api/octopus/router/runs/r1/cancel", headers=D).json()["ok"]
    assert c.get("/api/octopus/router/missions/..%2Fx", headers=D).status_code in (400, 404)


def test_sso_and_connector_routes(home, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app
    from bot.octopus import connectors
    c = TestClient(build_app())
    monkeypatch.setattr(sso.httpx, "post", lambda url, json=None, headers=None, timeout=None: httpx.Response(
        200, json={"success": True, "token": "a.eyJ1c2VybmFtZSI6Imx1Y2kifQ.b", "valid": True, "user": {"username": "luci"}}))
    pushed = []
    monkeypatch.setattr(connectors, "push", lambda only=None: pushed.append(only) or {})
    r = c.post("/api/octopus/sso/login", headers=D, json={"username": "luci", "password": "pw", "code": "123456"})
    assert r.status_code == 200 and r.json()["username"] == "luci" and pushed == [None]
    assert "a.eyJ" not in r.text                                    # the session is never returned
    assert c.get("/api/octopus/sso", headers=D).json()["signed_in"]
    assert c.get("/api/octopus/sso/verify", headers=D).json()["valid"]
    assert c.post("/api/octopus/sso/logout", headers=D).json()["signed_in"] is False
    monkeypatch.setattr(sso.httpx, "post", lambda *a, **k: httpx.Response(401, json={"success": False, "error": "Invalid"}))
    assert c.post("/api/octopus/sso/login", headers=D, json={"username": "luci", "password": "x"}).status_code == 401
    assert c.post("/api/octopus/connectors/push", headers=D).status_code == 200
    rows = c.get("/api/octopus/connectors", headers=D).json()["connectors"]
    assert len(rows) == len(connectors.CONNECTORS) and {"installed", "running", "repo"} <= set(rows[0])


def test_start_and_stop_manage_the_process(home, monkeypatch):
    router_app._job.update(state="running", log=[])
    router_app._install_job(False)
    started = {}

    class P:
        pid = 424242
    monkeypatch.setattr(router_app, "_spawn", lambda cmd, **kw: started.update(cmd=cmd, cwd=kw.get("cwd")) or P())
    monkeypatch.setattr(router_app, "_alive", lambda pid: pid == 424242 and router_app._pid_file().exists())
    monkeypatch.setattr(router.httpx, "request", lambda *a, **k: httpx.Response(200, json={"ok": True}))
    s = router_app.start()
    assert s["running"] and s["pid"] == 424242 and started["cmd"][1:] == ["--env-file=.env", "server/index.js"]
    assert router_app.start()["pid"] == 424242                        # already running: no second process
    killed = []

    class Proc:
        def children(self, recursive=False):
            return []

        def terminate(self):
            killed.append("term")

        def wait(self, t):
            return 0
    import psutil
    monkeypatch.setattr(psutil, "Process", lambda pid: Proc())
    assert router_app.stop()["running"] is False and killed == ["term"] and not router_app._pid_file().exists()
