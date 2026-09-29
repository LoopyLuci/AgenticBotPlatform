"""Integration keys (bot/integrations.py) and the contract octopus-router depends on.

The Router's server/botplatform.js (Octopus-Security/octopus-router) drives ABP's bots and Docker manager with
one ABP credential. These tests make exactly its calls with an integration key minted from the
"octopus-router" preset, so a change to ABP that would break the Router's Bot Platform view fails here first:
the routes, their auth, and the response shapes it reads. They also prove the key reaches nothing else.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot import bot_instances, docker_mgr as dk, integrations
from bot.dashboard.server import build_app

_BOT_TOKEN = "123456789:AAExampleTokenFromBotFather1234"


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    return TestClient(build_app())


@pytest.fixture
def router_key(client):
    r = client.post("/api/integrations/keys", headers={"X-Dashboard-Token": "test-token"},
                    json={"preset": "octopus-router", "allow_framing": False})
    assert r.status_code == 200, r.text
    return {"X-Dashboard-Token": r.json()["key"]}


def test_minting_needs_the_desktop_token_and_known_scopes(client, router_key):
    assert client.post("/api/integrations/keys", headers=router_key, json={"preset": "octopus-router"}).status_code in (401, 403)
    bad = client.post("/api/integrations/keys", headers={"X-Dashboard-Token": "test-token"}, json={"scopes": ["root"]})
    assert bad.status_code == 422
    listing = client.get("/api/integrations", headers={"X-Dashboard-Token": "test-token"}).json()
    assert listing["keys"][0]["preset"] == "octopus-router" and "docker:control" in listing["keys"][0]["scopes"]
    assert "key" not in listing["keys"][0]


def test_the_routers_status_and_bot_calls(client, router_key):
    # botplatform.js: GET healthz (no auth), GET api/overview, GET api/bots, POST api/bots/{n}/{action}
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/overview", headers=router_key).status_code == 200
    bot_instances.create_instance(name="tg", platform="telegram", backend="ui", credentials={"bot_token": _BOT_TOKEN},
                                  allowed_user_ids=[1])
    r = client.get("/api/bots", headers=router_key)
    assert r.status_code == 200
    rows = r.json()
    assert rows and {"id", "name", "platform", "backend", "enabled", "live_running", "circuit"} <= set(rows[0])  # shapeBot
    assert _BOT_TOKEN not in r.text                      # credentials never cross
    one = client.get(f"/api/bots/{rows[0]['id']}", headers=router_key)
    assert one.status_code == 200 and _BOT_TOKEN not in one.text
    assert client.post(f"/api/bots/{rows[0]['id']}/disable", headers=router_key).status_code == 200
    assert client.get("/api/integrations/whoami", headers=router_key).json()["caller"] == "integration"


def test_the_routers_docker_calls(client, router_key, monkeypatch):
    seen = []
    monkeypatch.setattr(dk, "_run", lambda args, **kw: (seen.append(args) or (True, "")))
    monkeypatch.setattr(dk, "containers", lambda all_=True: [{"ID": "abc", "Names": "web", "State": "running"}])
    monkeypatch.setattr(dk, "stacks", lambda: [{"Name": "octo", "Status": "running(1)"}])
    hosts = client.get("/api/infra/hosts", headers=router_key)
    assert hosts.status_code == 200 and hosts.json()["hosts"][0]["id"] == "local"
    monkeypatch.setattr(dk, "info", lambda: {"installed": True, "running": True, "server_version": "27", "os": "NixOS",
                                             "containers": 1, "running_containers": 1, "images": 3})
    info = client.get("/api/docker/info", headers=router_key)
    assert info.status_code == 200 and {"running", "installed", "server_version", "os", "images"} <= set(info.json())
    assert client.get("/api/docker/containers", headers=router_key).json()[0]["Names"] == "web"
    assert client.get("/api/docker/stacks", headers=router_key).json()[0]["Name"] == "octo"
    assert client.post("/api/docker/containers/web/action", headers=router_key, json={"action": "restart"}).status_code == 200
    monkeypatch.setattr(dk, "container_logs", lambda ident, tail, since, ts: {"logs": "line"})
    assert client.get("/api/docker/containers/web/logs?tail=50&timestamps=true", headers=router_key).json()["logs"] == "line"
    monkeypatch.setattr(dk, "stack_action", lambda name, action, *a, **kw: {"ok": True, "output": "pulled"})
    r = client.post("/api/docker/stacks/octo/action", headers=router_key, json={"action": "pull"})
    assert r.status_code == 200 and r.json()["ok"] is True
    monkeypatch.setattr(dk, "stack_logs", lambda name, tail: {"logs": "s"})
    assert client.get("/api/docker/stacks/octo/logs?tail=20", headers=router_key).json()["logs"] == "s"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/config"),                        # settings
    ("GET", "/api/providers"),                     # model keys
    ("POST", "/api/bots"),                         # create a bot (credentials)
    ("DELETE", "/api/bots/1"),
    ("POST", "/api/docker/containers"),            # create/run a container
    ("DELETE", "/api/docker/containers/web"),      # destructive
    ("POST", "/api/docker/containers/web/exec"),
    ("POST", "/api/docker/prune"),
    ("GET", "/api/env"),
    ("POST", "/api/integrations/keys"),
    ("POST", "/api/modules/cacheit/call"),         # not in this preset
])
def test_everything_outside_its_scopes_is_refused(client, router_key, method, path):
    r = client.request(method, path, headers=router_key, json={})
    assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


def test_a_revoked_key_is_dead(client, router_key):
    kid = client.get("/api/integrations", headers={"X-Dashboard-Token": "test-token"}).json()["keys"][0]["id"]
    assert client.delete(f"/api/integrations/keys/{kid}", headers={"X-Dashboard-Token": "test-token"}).status_code == 200
    assert client.get("/api/overview", headers=router_key).status_code == 401


def test_framing_allowlist_reaches_the_page_policy(client, monkeypatch):
    monkeypatch.setattr(integrations, "frame_ancestors", lambda: ["http://127.0.0.1:3030"])
    monkeypatch.setattr(integrations, "frame_src", lambda: ["https://router.example"])
    from bot.dashboard.server import page_csp
    csp = page_csp("n")
    assert "frame-ancestors 'self' http://127.0.0.1:3030" in csp and "frame-src 'self' https://router.example" in csp
    assert "frame-ancestors 'self' http://127.0.0.1:3030" in client.get("/healthz").headers["content-security-policy"]


@pytest.mark.parametrize("origin", ["javascript:alert(1)", "https://a.example/path", "https://*.example", "ftp://x"])
def test_only_plain_origins_are_accepted(origin):
    with pytest.raises(ValueError):
        integrations.normalize_origin(origin)


def test_a_down_docker_daemon_is_one_readable_line(monkeypatch):
    # With the daemon down, `docker info --format '{{json .}}'` prints the whole empty info document and then the
    # message; the Router's Bot Platform view showed all of it.
    import json
    doc = json.dumps({"ID": "", "Containers": 0, "ServerErrors": ["Cannot connect to the Docker daemon"]})
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    monkeypatch.setattr(dk, "_run", lambda args, **kw: (False, doc + "\nerror during connect: pipe missing"))
    info = dk.info()
    assert info["running"] is False and info["error"] == "Cannot connect to the Docker daemon error during connect: pipe missing"
