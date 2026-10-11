"""The Vault screen: identical in both UIs, wired into both pages, only calls routes that exist, and the routes
never put a secret in any response (success or error)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot import vault
from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "bot/dashboard/static/vault-panel.js"
HEAD = {"X-Dashboard-Token": "test-token"}
PASSWORD = "Sup3r-Secret-pw-9431"
TOTP = "JBSWY3DPEHPK3PXP"
NOTE = "note-secret-7788"


@pytest.fixture
def client(monkeypatch, temp_db, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setenv("ABP_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("ABP_VAULT_KEY", raising=False)
    return TestClient(build_app())


def _add(client, **over):
    body = {"name": "github", "origin": "https://GitHub.com/login", "username": "me", "password": PASSWORD, "totp_secret": TOTP}
    body.update(over)
    return client.post("/api/vault", json=body, headers=HEAD)


def _never_leaks(*responses):
    for r in responses:
        for secret in (PASSWORD, TOTP, NOTE):
            assert secret not in r.text, (r.request.method, r.request.url.path)


def test_the_panel_is_identical_in_both_uis():
    assert JS.read_text(encoding="utf-8") == (ROOT / "desktop-app/ui/vault-panel.js").read_text(encoding="utf-8")


def test_both_pages_mount_it_with_a_nav_item():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert re.search(r'<section id="vault"[^>]*>', text) and 'id="vt-root"' in text and 'href="#vault"' in text
        assert re.search(r'<script src="(/static/)?vault-panel\.js"></script>', text)


def test_the_panel_shows_what_the_screen_promises():
    js = JS.read_text(encoding="utf-8")
    for needle in ("Key protection", "The vault is empty", "Add a login", "window.confirm", "Delete", "Search",
                   'type="password"', 'autocomplete="new-password"'):
        assert needle in js, needle
    assert "http://" not in js and "cdn" not in js.lower(), "no CDN or external address"
    assert not re.search(r"\$\{(e|d)\.(name|origin|username|key_note|error)\}", js), "values go through E()"
    assert not re.search(r"\be\.(password|totp_secret)\b", js), "the list code never reads a secret field"
    assert "localStorage" not in js and "sessionStorage" not in js


def test_the_panel_only_calls_routes_that_exist_with_the_right_methods(client):
    js = JS.read_text(encoding="utf-8")
    routes = {}
    for r in client.app.routes:
        for m in getattr(r, "methods", None) or ():
            routes.setdefault(m, set()).add(getattr(r, "path", ""))

    def exists(method, path):
        pat = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}")) + "$")
        return any(pat.match(p) or p == path for p in routes.get(method, ()))

    calls = []
    for m in re.finditer(r"api\((['`])(/api/[^'`?]*?)(?:\?[^'`]*)?\1(?:, \{ method: '(\w+)')?", js):
        calls.append((m.group(3) or "GET", re.sub(r"\$\{[^}]+\}", "{id}", m.group(2))))
    assert {("GET", "/api/vault"), ("POST", "/api/vault")} <= set(calls)
    assert ("DELETE", "/api/vault/{id}") in calls
    for method, path in calls:
        assert exists(method, path), (method, path)
    assert client.get("/static/vault-panel.js").status_code == 200


def test_empty_vault_and_key_status(client):
    r = client.get("/api/vault", headers=HEAD)
    assert r.status_code == 200
    d = r.json()
    assert d["entries"] == [] and d["readable"] is True and d["key_source"] == "none"
    assert "ABP_VAULT_KEY" in d["key_note"]
    assert str(vault._dir()) not in r.text, "no folder path in the response"


def test_add_list_delete_never_return_a_secret(client):
    added = _add(client)
    assert added.status_code == 200 and added.json() == {"ok": True, "name": "github"}
    listed = client.get("/api/vault", headers=HEAD)
    d = listed.json()
    assert d["key_source"] == "file"
    (e,) = d["entries"]
    assert e["name"] == "github" and e["origin"] == "https://github.com" and e["username"] == "me"
    assert e["has_password"] is True and e["has_totp"] is True and isinstance(e["added"], int)
    assert set(e) == {"name", "origin", "username", "has_password", "has_totp", "added"}
    deleted = client.delete("/api/vault/github", headers=HEAD)
    assert deleted.json() == {"ok": True}
    assert client.get("/api/vault", headers=HEAD).json()["entries"] == []
    _never_leaks(added, listed, deleted)


def test_the_stored_secret_is_really_stored_but_only_the_vault_can_read_it(client):
    _add(client)
    assert vault.value("github", "password", page_url="https://github.com/x") == PASSWORD


def test_note_text_is_never_shown(client):
    vault.add("withnote", origin="https://example.com", password=PASSWORD, note=NOTE)
    r = client.get("/api/vault", headers=HEAD)
    _never_leaks(r)
    assert "note" not in r.json()["entries"][0]


def test_errors_never_echo_the_input(client):
    bad = [
        _add(client, name="bad name!"),
        _add(client, origin="not a url"),
        _add(client, totp_secret="not base32 !!" + PASSWORD),
        _add(client, password=12345),
        client.post("/api/vault", content=b"{" + PASSWORD.encode(), headers=HEAD),
        client.post("/api/vault", json=[PASSWORD], headers=HEAD),
        client.post("/api/vault", json={"name": "x", "origin": "https://a.com", "password": PASSWORD * 2000}, headers=HEAD),
    ]
    assert [r.status_code for r in bad] == [400, 400, 400, 400, 400, 400, 413]
    _never_leaks(*bad)
    assert client.get("/api/vault", headers=HEAD).json()["entries"] == []


def test_adding_an_existing_name_does_not_overwrite_it(client):
    assert _add(client).status_code == 200
    again = _add(client, password="another-password-1")
    assert again.status_code == 409
    _never_leaks(again)
    assert vault.value("github", "password", page_url="https://github.com") == PASSWORD


def test_delete_unknown_is_404(client):
    assert client.delete("/api/vault/nope", headers=HEAD).status_code == 404


def test_all_routes_need_the_token(client):
    assert client.get("/api/vault").status_code in (401, 403)
    assert client.post("/api/vault", json={"name": "a", "origin": "https://a.com", "password": PASSWORD}).status_code in (401, 403)
    assert client.delete("/api/vault/github").status_code in (401, 403)
    assert vault.listing() == []


def test_key_from_the_environment_is_reported(client, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ABP_VAULT_KEY", Fernet.generate_key().decode())
    d = client.get("/api/vault", headers=HEAD).json()
    assert d["key_source"] == "env" and "environment variable" in d["key_note"]
    assert not (Path(vault._dir()) / "vault.key").exists()


def test_an_unreadable_vault_is_reported_without_leaking(client, monkeypatch):
    from cryptography.fernet import Fernet

    _add(client)
    monkeypatch.setenv("ABP_VAULT_KEY", Fernet.generate_key().decode())    # the wrong key
    r = client.get("/api/vault", headers=HEAD)
    d = r.json()
    assert r.status_code == 200 and d["readable"] is False and d["entries"] == [] and "decrypted" in d["error"]
    _never_leaks(r)
