"""Regression tests for the security audit fixes.

Each one is a hole that was found by reading the code and confirmed:
- a paired phone at tier `none` could create a hook, i.e. run a shell
  command as the server user;
- the unauthenticated OAuth callback reflected `?error=` into HTML;
- a blank `DASHBOARD_TOKEN=` (as .env.example ships it) left the process
  with an empty token, which also opened the token-bootstrap routes;
- no baseline security headers at all.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from bot import db, envfile
from bot.dashboard.server import build_app

_TOKEN = "test-dashboard-token"
_DESKTOP = {"X-Dashboard-Token": _TOKEN}
_HOOK = {"event": "PreToolUse", "command": "echo hi"}


@pytest.fixture
def app_client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", _TOKEN)
    return TestClient(build_app())


def _device_headers(tier: str) -> dict:
    _key_id, plaintext = db.create_api_key(f"test-{tier}", permission_tier=tier)
    return {"X-Dashboard-Token": plaintext}


def _valid_hook_event() -> str:
    from bot.agent_runtime import hooks

    return sorted(hooks.VALID_EVENTS)[0]


# ---------------------------------------------------------------- hooks (RCE)
@pytest.mark.parametrize("tier", ["none", "standard", "elevated"])
def test_a_device_below_unrestricted_cannot_create_a_shell_hook(app_client, tier):
    resp = app_client.post(
        "/api/hooks", headers=_device_headers(tier),
        json={"event": _valid_hook_event(), "command": "echo pwned"},
    )
    assert resp.status_code == 403
    assert db.list_agent_hooks() == []


def test_an_unrestricted_device_can_create_a_hook(app_client):
    resp = app_client.post(
        "/api/hooks", headers=_device_headers("unrestricted"),
        json={"event": _valid_hook_event(), "command": "echo ok"},
    )
    assert resp.status_code == 200


def test_the_desktop_token_can_always_create_a_hook(app_client):
    resp = app_client.post(
        "/api/hooks", headers=_DESKTOP, json={"event": _valid_hook_event(), "command": "echo ok"},
    )
    assert resp.status_code == 200


def test_a_low_tier_device_cannot_enable_a_hook_someone_else_made(app_client):
    hook_id = app_client.post(
        "/api/hooks", headers=_DESKTOP, json={"event": _valid_hook_event(), "command": "echo ok"},
    ).json()["id"]
    app_client.post(f"/api/hooks/{hook_id}/disable", headers=_DESKTOP)

    resp = app_client.post(f"/api/hooks/{hook_id}/enable", headers=_device_headers("none"))

    assert resp.status_code == 403


def test_a_low_tier_device_can_still_disable_and_list_hooks(app_client):
    hook_id = app_client.post(
        "/api/hooks", headers=_DESKTOP, json={"event": _valid_hook_event(), "command": "echo ok"},
    ).json()["id"]
    headers = _device_headers("none")

    assert app_client.get("/api/hooks", headers=headers).status_code == 200
    assert app_client.post(f"/api/hooks/{hook_id}/disable", headers=headers).status_code == 200


# ---------------------------------------------------------------------- XSS
def test_oauth_callback_does_not_reflect_markup(app_client):
    resp = app_client.get("/api/mcp-external/oauth/callback", params={"error": "<script>alert(1)</script>"})
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


# ----------------------------------------------------------------- headers
def test_baseline_security_headers_are_sent(app_client):
    resp = app_client.get("/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors" in csp and "object-src 'none'" in csp and "base-uri" in csp


def test_the_dashboard_page_still_gets_the_headers(app_client):
    assert app_client.get("/").headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------- token bootstrap window
def test_bootstrap_routes_refuse_a_remote_caller_when_no_token_is_set(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "")
    remote = TestClient(build_app(), client=("203.0.113.9", 50000))
    assert remote.get("/api/env/content").status_code == 503


def test_bootstrap_routes_still_work_from_this_machine_when_no_token_is_set(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "")
    local = TestClient(build_app(), client=("127.0.0.1", 50000))
    assert local.get("/api/setup/status").status_code == 200


def test_bootstrap_is_closed_once_a_token_exists(app_client):
    remote = TestClient(build_app(), client=("203.0.113.9", 50000))
    assert remote.get("/api/env/content").status_code == 401


# ------------------------------------------------ blank DASHBOARD_TOKEN= line
def test_a_blank_token_line_is_filled_in_place_not_duplicated(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=1\nDASHBOARD_TOKEN=\nBAR=2\n", encoding="utf-8")
    monkeypatch.setattr(envfile, "resolve", lambda: env_file)
    monkeypatch.setattr(envfile, "BACKUP_DIR", tmp_path / "backups")

    token = envfile.ensure_dashboard_token()

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert token and len(token) >= 32
    assert lines.count(f"DASHBOARD_TOKEN={token}") == 1
    assert sum(1 for ln in lines if ln.startswith("DASHBOARD_TOKEN=")) == 1
    assert "FOO=1" in lines and "BAR=2" in lines
    # Idempotent: the next boot must read the SAME token back, not mint another.
    assert envfile.ensure_dashboard_token() == token


def test_main_replaces_an_empty_token_from_the_environment():
    """load_dotenv() puts '' in os.environ for a blank line and setdefault
    keeps it; bot/main.py must overwrite an empty value, not preserve it."""
    import inspect

    from bot import main

    source = inspect.getsource(main)
    assert 'os.environ.setdefault("DASHBOARD_TOKEN"' not in source
    assert 'if not os.environ.get("DASHBOARD_TOKEN")' in source
