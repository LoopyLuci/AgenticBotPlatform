"""The dashboard token is always generated and never asked for.

No screen, dialog or wizard field takes it. The server puts it in the dashboard page when that page is loaded from this
machine, and in no other case. The token used here is a plain placeholder, not a key-shaped string.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from bot import setup_wizard
from bot.dashboard.server import build_app

TOKEN = "unused-dashboard-token"
LOCAL = "http://127.0.0.1:8787"
ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGES = ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html", "desktop-app/ui/main.js")


def _client(monkeypatch, *, host: str = "127.0.0.1", base: str = LOCAL):
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    return TestClient(build_app(), base_url=base, client=(host, 50000))


def _injected(response) -> bool:
    # The page's own code reads window.__ABP_TOKEN__; only the server writes the assignment.
    return "window.__ABP_TOKEN__=" in response.text


@pytest.mark.parametrize("path", ["/", "/desktop-ui/", "/desktop-ui/index.html"])
def test_a_local_page_load_gets_the_token(monkeypatch, path):
    response = _client(monkeypatch).get(path)
    assert response.status_code == 200
    assert _injected(response)
    assert f'window.__ABP_TOKEN__="{TOKEN}"' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_a_local_page_on_localhost_and_ipv6_loopback_also_gets_it(monkeypatch):
    assert _injected(_client(monkeypatch, base="http://localhost:8787").get("/"))
    # The test client cannot take a bracketed IPv6 base URL, so the Host header is set by hand.
    assert _injected(_client(monkeypatch, host="::1").get("/", headers={"Host": "[::1]:8787"}))


@pytest.mark.parametrize(
    "case, kwargs, headers",
    [
        ("another machine", {"host": "192.168.1.20"}, {}),
        ("a public name in front of it", {"base": "http://abp.example.test"}, {}),
        ("a proxy that forwarded it", {}, {"X-Forwarded-For": "203.0.113.9"}),
        ("a Forwarded header", {}, {"Forwarded": "for=203.0.113.9"}),
        ("a script from another web app", {}, {"Origin": "http://localhost:3000"}),
        ("a cors fetch", {}, {"Sec-Fetch-Mode": "cors"}),
        ("a cross-site request", {}, {"Sec-Fetch-Site": "cross-site"}),
    ],
)
def test_the_token_is_never_handed_to_anything_but_a_plain_local_page_load(monkeypatch, case, kwargs, headers):
    response = _client(monkeypatch, **kwargs).get("/", headers=headers)
    assert response.status_code == 200, case
    assert not _injected(response), case
    assert TOKEN not in response.text, case
    assert response.headers["cache-control"] == "no-cache", case


def test_no_token_is_placed_when_none_is_configured(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    client = TestClient(build_app(), base_url=LOCAL, client=("127.0.0.1", 50000))
    assert not _injected(client.get("/"))


def test_a_token_with_markup_in_it_cannot_break_out_of_the_script(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", 'x</script><script>alert(1)</script>')
    client = TestClient(build_app(), base_url=LOCAL, client=("127.0.0.1", 50000))
    body = client.get("/").text
    assert "</script><script>alert(1)" not in body
    assert "\\u003c/script\\u003e" in body


def test_the_injected_token_actually_authorizes_the_api(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/providers").status_code == 401
    assert client.get("/api/providers", headers={"X-Dashboard-Token": TOKEN}).status_code == 200


# ---------------------------------------------------- no screen asks for it --
@pytest.mark.parametrize("rel", PAGES)
def test_no_ui_has_a_token_dialog_button_or_input(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for needle in ("tokenModal", "btn-token", "tokenInput", "tokenSave", "showTokenModal", "paste DASHBOARD_TOKEN", "Set token"):
        assert needle not in text, f"{rel} still has {needle!r}"


def test_the_setup_wizard_never_asks_for_the_token():
    assert "DASHBOARD_TOKEN" not in setup_wizard.FIELDS
    assert "DASHBOARD_TOKEN" not in setup_wizard.check_status()["fields"]


def test_the_wizard_route_that_generated_a_token_is_gone(monkeypatch):
    client = _client(monkeypatch)
    response = client.post("/api/setup/generate-token", headers={"X-Dashboard-Token": TOKEN})
    assert response.status_code in (404, 405)


def test_the_bot_generates_a_token_when_there_is_none(monkeypatch, tmp_path):
    from bot import envfile

    monkeypatch.setattr(envfile, "PROJECT_ENV", tmp_path / ".env")
    monkeypatch.setattr(envfile, "candidates", lambda: [tmp_path / ".env"])
    token = envfile.ensure_dashboard_token()
    assert token and len(token) >= 16
    assert envfile.ensure_dashboard_token() == token  # stable, not regenerated
