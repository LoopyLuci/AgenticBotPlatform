"""The Octopus estate from ABP: the catalog, status probes, octopus-router's client and provider, SSO through
octopus-auth, the /api/octopus routes and the page. The Router and auth are fakes with the real response shapes
(read from octopus-router server/index.js and octopus-auth index.js)."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bot import envfile, providers
from bot.octopus import estate, router, sso

ROOT = Path(__file__).resolve().parent.parent
D = {"X-Dashboard-Token": "test-token"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    store: dict[str, str] = {}
    monkeypatch.setattr(envfile, "get_var", lambda k: store.get(k))
    monkeypatch.setattr(envfile, "set_var", lambda k, v, actor="": store.__setitem__(k, v))
    monkeypatch.setattr(providers, "_module_cache", (0.0, {}))
    return store


def fake_router(method, url, json=None, params=None, headers=None, timeout=None):
    path = url.split("3030", 1)[1]
    authed = (headers or {}).get("Authorization") == "Bearer router-owner-token-0123"
    if path == "/api/build":
        return httpx.Response(200, json={"ok": True, "service": "octopus-router", "startedAt": 1})
    if not authed:
        return httpx.Response(401, json={"error": "owner token required"})
    if path == "/api/me":
        return httpx.Response(200, json={"owner": "serpopard", "providers": [{"id": "openrouter", "label": "OpenRouter"}]})
    if path == "/api/models":
        return httpx.Response(200, json={"models": [{"alias": "auto", "provider": "routing", "cost": "free"}], "catalog": {}})
    if path == "/api/chat":
        assert json["messages"][-1]["content"] == "hi" and json["model"] == "auto"
        return httpx.Response(200, json={"reply": "hello", "model": "auto", "provider": "phantom"})
    if path == "/api/usage":
        return httpx.Response(200, json={"usage": []})
    return httpx.Response(404, json={"error": "not found"})


def test_the_catalog_covers_the_hub_tiles_with_their_real_subdomains():
    hub = {"budget": "budget", "octopus-planner": "plan", "octopus-shopper": "shop", "octopus-cortex": "chat",
           "octopus-author": "write", "octopus-ee": "ee", "octopus-business": "business"}
    by_id = {s["id"]: s for s in estate.services()}
    for sid, sub in hub.items():
        sid = sid if sid.startswith("octopus-") else f"octopus-{sid}"
        assert by_id[sid]["url"] == f"https://{sub}.{estate.DEFAULT_DOMAIN}", sid
    assert by_id["octopus-router"]["url"] is None and by_id["octopus-tech-site"]["url"] == f"https://{estate.DEFAULT_DOMAIN}"
    assert len(by_id) >= 40 and all(s["repo"].startswith("https://github.com/Octopus-Security/") for s in by_id.values())


def test_overrides_move_or_hide_a_service(monkeypatch):
    monkeypatch.setattr(estate, "_cfg", lambda: {"domain": "example.test", "services": {
        "octopus-budget": {"url": "http://10.0.0.5:3000"}, "octopus-mail": {"disabled": True}}})
    by_id = {s["id"]: s for s in estate.services()}
    assert by_id["octopus-budget"]["url"] == "http://10.0.0.5:3000" and "octopus-mail" not in by_id
    assert by_id["octopus-health"]["url"] == "https://health.example.test"


def test_status_probes_in_parallel_and_reads_the_build(monkeypatch):
    def handler(req):
        if req.url.host.startswith("budget."):
            return httpx.Response(200, json={"service": "octopus-budget", "commit": "abc1234def"})
        if req.url.host.startswith("health."):
            return httpx.Response(302, headers={"location": "/login"})
        raise httpx.ConnectError("no route")
    real = httpx.AsyncClient
    monkeypatch.setattr(estate.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    import asyncio
    st = asyncio.run(estate.status(refresh=True))
    assert st["octopus-budget"]["state"] == "up" and st["octopus-budget"]["build"]["commit"] == "abc1234def"
    assert st["octopus-health"]["state"] == "up"            # a redirect to sign-in is a live service
    assert st["octopus-games"]["state"] == "down" and st["octopus-router"]["state"] == "no-probe"


def test_router_client_status_chat_and_provider(env, monkeypatch):
    monkeypatch.setattr(router.httpx, "request", fake_router)
    s = router.status()
    assert s["reachable"] and not s["configured"] and "authorized" not in s
    with pytest.raises(router.RouterError) as e:
        router.models()
    assert e.value.status == 412
    with pytest.raises(ValueError):
        router.set_token("short")
    router.set_token("router-owner-token-0123")
    s = router.status()
    assert s["authorized"] and s["owner"] == "serpopard" and s["providers"] == ["openrouter"]
    assert router.chat([{"role": "user", "content": "hi"}])["reply"] == "hello"
    p = providers.get_provider("octopus-router")
    assert p["base_url"] == "http://127.0.0.1:3030/v1" and p["api_key"] == "router-owner-token-0123"
    with pytest.raises(router.RouterError):
        router.messages("../../etc")


def _jwt(payload: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(payload)}.sig"


def test_sso_login_keeps_only_the_session(env, monkeypatch):
    sent = {}
    tok = _jwt({"username": "luci", "role": "admin", "exp": int(time.time()) + 7 * 86400})

    def post(url, json=None, headers=None, timeout=None):
        sent[url.rsplit("/", 1)[1]] = json
        if url.endswith("/login"):
            if json["totpCode"] != "123456":
                return httpx.Response(401, json={"success": False, "error": "Invalid credentials"})
            return httpx.Response(200, json={"success": True, "token": tok})
        if url.endswith("/verify"):
            return httpx.Response(200, json={"success": True, "valid": True, "user": {"username": "luci"}})
        return httpx.Response(404)
    monkeypatch.setattr(sso.httpx, "post", post)
    with pytest.raises(sso.SsoError):
        sso.login("luci", "pw", "000000")
    assert not sso.token()
    out = sso.login("luci", "pw", "123456")
    assert out["signed_in"] and out["username"] == "luci" and out["expires_in_s"] > 6 * 86400
    assert env == {sso.TOKEN_VAR: tok}                       # the password and code are not kept
    assert sso.verify()["valid"] and not sso.refresh_if_needed()
    assert sent["login"]["password"] == "pw"
    sso.logout()
    assert not sso.state()["signed_in"]


def test_routes_are_desktop_only_and_never_return_secrets(temp_db, env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(router.httpx, "request", fake_router)
    from bot.dashboard.server import build_app
    c = TestClient(build_app())
    assert c.get("/api/octopus/router").status_code == 401
    r = c.put("/api/octopus/router/token", headers=D, json={"token": "router-owner-token-0123"})
    assert r.status_code == 200 and r.json()["authorized"] and "router-owner-token-0123" not in r.text
    assert c.get("/api/octopus/router/models", headers=D).json()["models"][0]["alias"] == "auto"
    assert c.post("/api/octopus/router/chat", headers=D, json={"prompt": "hi"}).json()["reply"] == "hello"
    assert c.get("/api/octopus/router/nope", headers=D).status_code == 404
    from bot.config import config
    monkeypatch.setattr(config, "set_value", lambda *a, **k: None)
    assert c.put("/api/octopus/router/url", headers=D, json={"url": "javascript:x"}).status_code == 422
    # an integration key (even the Router's own) cannot reach these
    k = c.post("/api/integrations/keys", headers=D, json={"preset": "octopus-router", "allow_framing": False}).json()["key"]
    assert c.get("/api/octopus/router", headers={"X-Dashboard-Token": k}).status_code == 403


def test_the_page_is_in_both_uis_and_identical():
    a = (ROOT / "bot/dashboard/static/octopus-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/octopus-panel.js").read_text(encoding="utf-8")
    assert a == b
    for html in ((ROOT / "bot/dashboard/static/dashboard.html").read_text(encoding="utf-8"),
                 (ROOT / "desktop-app/ui/index.html").read_text(encoding="utf-8")):
        assert 'id="octopus"' in html and 'id="ocp-root"' in html and "octopus-panel.js" in html and 'href="#octopus"' in html


def test_connectors_are_modules_and_get_the_right_session(env, monkeypatch):
    from bot.modules import client, harness, registry
    from bot.octopus import connectors
    registry.modules(refresh=True)
    octo = [m for m in registry.modules().values() if m.area == "octopus"]
    assert len(octo) == len(connectors.CONNECTORS) + len(connectors.RUNNERS) >= 30
    assert all(m.repo.startswith("https://github.com/LoopyLuci/abp-octopus-") for m in octo)
    env[sso.TOKEN_VAR] = "sso-session"
    env[router.TOKEN_VAR] = "router-owner-token-0123"
    running = {"octopus-budget", "octopus-router"}
    monkeypatch.setattr(client, "find", lambda m, timeout=2.0: object() if m.id in running else None)
    sent = {}
    monkeypatch.setattr(client, "call", lambda m, op, args, timeout=0: sent.setdefault(m.id, (op, args)) and {"signed_in": True})
    assert connectors.push() == {"octopus-budget": "signed in", "octopus-router": "signed in"}
    assert sent == {"octopus-budget": ("auth.set_token", {"token": "sso-session"}),
                    "octopus-router": ("auth.set_token", {"token": "router-owner-token-0123"})}
    sent.clear()
    import bot.dashboard.octopus_api  # noqa: F401 - registers the hook
    assert connectors.on_hub_started in harness.HUB_STARTED
    connectors.on_hub_started("cacheit")                # not a connector: nothing
    assert sent == {}
