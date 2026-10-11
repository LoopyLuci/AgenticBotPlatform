"""The Usage & limits page ships in both UIs, identical, with its nav item, and only calls routes that exist."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_the_panel_is_identical_in_both_uis():
    dash = (ROOT / "bot/dashboard/static/usage-panel.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/usage-panel.js").read_text(encoding="utf-8")
    assert dash == desk


def test_both_pages_mount_it_with_a_nav_item():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert re.search(r'<section id="usage"[^>]*>', text) and 'id="ul-root"' in text and 'href="#usage"' in text
        assert re.search(r'<script src="(/static/)?usage-panel\.js"></script>', text)


def test_the_panel_shows_what_the_screen_promises():
    js = (ROOT / "bot/dashboard/static/usage-panel.js").read_text(encoding="utf-8")
    for needle in ("blocked until", "429s", "toLocaleString", "setInterval", "data-days=\"7\"", "No model calls"):
        assert needle in js, needle
    assert "http://" not in js and "https://" not in js, "no CDN or external address"


def test_the_panel_only_calls_routes_that_exist(temp_db):
    from bot.dashboard.server import build_app

    app = build_app()
    routes = {getattr(r, "path", "") for r in app.routes}
    js = (ROOT / "bot/dashboard/static/usage-panel.js").read_text(encoding="utf-8")
    called = set(re.findall(r"'(/api/models/[a-z-]+)", js))
    assert called == {"/api/models/usage-limits"}
    assert called <= routes
    assert TestClient(app).get("/static/usage-panel.js").status_code == 200
