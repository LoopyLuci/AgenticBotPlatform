"""The Coordination screen: one panel file shipped identically in the dashboard and the desktop app, registered like the
other panels, escaping everything it draws, and only calling routes that exist."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
JS = "coordination-panel.js"


def test_the_panel_is_identical_in_both_uis():
    dash = (ROOT / "bot/dashboard/static" / JS).read_bytes()
    desk = (ROOT / "desktop-app/ui" / JS).read_bytes()
    assert dash == desk and len(dash) > 1000


def test_the_panel_is_registered_in_both_pages():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="co-root"' in text and 'href="#coordination"' in text
        assert re.search(r'<script src="(/static/)?coordination-panel\.js"></script>', text)


def test_everything_drawn_goes_through_the_escaper():
    js = (ROOT / "bot/dashboard/static" / JS).read_text(encoding="utf-8")
    interpolations = re.findall(r"\$\{([^}]*)\}", js)
    raw = [i for i in interpolations if i.strip().startswith(("t.", "h.", "b.")) and "E(" not in i and "?" not in i]   # a ternary between constants is not server text
    assert not raw, raw
    # values from the server reach the page only inside E(...) or as numbers/known constants
    assert "innerHTML" in js and "E(t.title)" in js and "E(h.resource)" in js


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_the_panel_only_calls_routes_that_exist(client):
    js = (ROOT / "bot/dashboard/static" / JS).read_text(encoding="utf-8")
    h = {"X-Dashboard-Token": "test-token"}
    for route in sorted(set(re.findall(r"/api/coordination/\w+", js))):
        assert client.get(route, headers=h).status_code == 200, route
    assert client.get("/static/" + JS).status_code == 200
