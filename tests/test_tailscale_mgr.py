"""bot/tailscale_mgr.py + /api/tailscale/*: argv is always a list built from
validated values (no flag/shell injection), the control-plane API is
allow-listed and needs a stored key, and the routes are desktop-token-only."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot import db, tailscale_mgr as ts
from bot.dashboard.server import build_app


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(ts, "is_installed", lambda: True)
    monkeypatch.setattr(ts, "_run", lambda args, timeout=30.0, stdin=None: (seen.append(args) or (True, "ok")))
    return seen


def test_set_prefs_builds_one_validated_call(calls):
    ts.set_prefs({"shields_up": True, "advertise_routes": "10.0.0.0/8,192.168.1.5/24", "hostname": "abp-box"})
    args = calls[0]
    assert args[0] == "set"
    assert "--shields-up=true" in args and "--hostname=abp-box" in args
    assert "--advertise-routes=10.0.0.0/8,192.168.1.0/24" in args


@pytest.mark.parametrize("changes", [
    {"nope": True}, {"hostname": "a; rm -rf /"}, {"exit_node": "--auth-key=x"},
    {"advertise_routes": "not-a-cidr"}, {"shields_up": "maybe"}, {},
])
def test_bad_prefs_are_rejected_before_any_process_runs(calls, changes):
    with pytest.raises(ts.TailscaleError):
        ts.set_prefs(changes)
    assert calls == []


def test_serve_and_funnel_argv(calls):
    ts.serve_set("3000", funnel=True, mode="https", port=8443, path="/app")
    assert calls[0] == ["funnel", "--bg", "--yes", "--https=8443", "--set-path=/app", "3000"]
    ts.serve_off(funnel=True, port=8443)
    assert calls[1] == ["funnel", "--https=8443", "off"]
    for bad in ("--evil", "a b", "http://x;y"):
        with pytest.raises(ts.TailscaleError):
            ts.serve_set(bad)


def test_auth_keys_must_look_like_auth_keys(calls):
    with pytest.raises(ts.TailscaleError):
        ts.up(auth_key="--exit-node=1.2.3.4")
    assert calls == []


def test_api_is_allow_listed_and_needs_a_key(monkeypatch):
    monkeypatch.setattr(ts.envfile, "get_var", lambda k, *a: "")
    with pytest.raises(ts.TailscaleError, match="API key"):
        ts.devices()
    monkeypatch.setattr(ts.envfile, "get_var", lambda k, *a: "k")
    for bad in ("/oauth/token", "/tailnet/-/../x", "/foo"):
        with pytest.raises(ts.TailscaleError, match="not a supported"):
            ts.api("GET", bad)


def test_api_sends_bearer_and_resolves_default_tailnet(monkeypatch):
    monkeypatch.setattr(ts.envfile, "get_var", lambda k, *a: {"TAILSCALE_API_KEY": "sekret"}.get(k, ""))
    seen = {}

    class R:
        status_code = 200
        content = b"{}"
        text = "{}"

        def json(self):
            return {"devices": []}

    monkeypatch.setattr(ts.httpx, "request", lambda m, url, **kw: seen.update(url=url, **kw) or R())
    assert ts.devices() == {"devices": []}
    assert seen["url"].endswith("/tailnet/-/devices")
    assert seen["headers"]["Authorization"] == "Bearer sekret"


def test_key_create_never_logs_or_echoes_the_api_key(monkeypatch):
    monkeypatch.setattr(ts.envfile, "get_var", lambda k, *a: "sekret")
    captured = {}
    monkeypatch.setattr(ts, "api", lambda m, p, body=None, **kw: captured.update(body=body) or {"key": "tskey-auth-x"})
    ts.key_create(tags=["tag:ci"], expiry_seconds=5)
    assert captured["body"]["expirySeconds"] == 60          # clamped
    assert "sekret" not in str(captured)


def test_routes_are_desktop_token_only(temp_db, monkeypatch, calls):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    client = TestClient(build_app())
    good = {"X-Dashboard-Token": "test-token"}
    _, phone = db.create_api_key("phone", kind="device")
    _, peer = db.create_api_key("peer: x", kind="peer_server")
    assert client.post("/api/tailscale/serve", json={"target": "3000"}).status_code in (401, 403)
    for key in (phone, peer):
        assert client.post("/api/tailscale/serve", json={"target": "3000"},
                           headers={"X-Dashboard-Token": key}).status_code in (401, 403)
    assert client.post("/api/tailscale/serve", json={"target": "3000"}, headers=good).status_code == 200
    assert client.post("/api/tailscale/prefs", json={"hostname": "bad host!"}, headers=good).status_code == 400
