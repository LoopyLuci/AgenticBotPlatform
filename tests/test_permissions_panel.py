"""The Permissions & approvals screen: it ships identically in both UIs and only calls routes (and methods) that exist."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "bot/dashboard/static/permissions-panel.js"


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_the_panel_is_identical_in_both_uis():
    assert JS.read_text(encoding="utf-8") == (ROOT / "desktop-app/ui/permissions-panel.js").read_text(encoding="utf-8")


def test_both_pages_mount_it_with_a_nav_item():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert re.search(r'<section id="permissions"[^>]*>', text) and 'id="pm-root"' in text and 'href="#permissions"' in text
        assert re.search(r'<script src="(/static/)?permissions-panel\.js"></script>', text)


def test_the_panel_shows_what_the_screen_promises():
    js = JS.read_text(encoding="utf-8")
    for needle in ("Waiting for you", "Recent decisions", "Approve once", "Always allow", "Deny", "Host rules", "Rules for one bot",
                   "Add rule", "window.confirm", "MCP tool pins", "Untrusted sessions", "Clear mark", "Nothing is waiting",
                   "No MCP servers", "setInterval", "document.hidden"):
        assert needle in js, needle
    assert "http://" not in js and "https://" not in js, "no CDN or external address"
    assert not re.search(r"\$\{(a|r|t|h)\.(tool|summary|body|session|server|description|match|note|decision|mode)\}", js), "values go through E()"


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
    for m in re.finditer(r"(?:api|one)\((['`])(/api/[^'`?]*?)(?:\?[^'`]*)?\1(?:, \{ method: '(\w+)')?", js):
        path = re.sub(r"\$\{[^}]+\}", "{id}", m.group(2))
        calls.append((m.group(3) or "GET", path))
    assert {"/api/approvals", "/api/mcp/pins", "/api/agent/permissions", "/api/bots", "/api/agent/taint"} <= {p for _, p in calls}
    assert ("POST", "/api/approvals/{id}/resolve") in calls and ("PUT", "/api/instances/{id}/permissions") in calls
    assert ("POST", "/api/mcp/pins/approve") in calls and ("POST", "/api/agent/taint/clear") in calls
    for method, path in calls:
        assert exists(method, path), (method, path)
    assert client.get("/static/permissions-panel.js").status_code == 200
