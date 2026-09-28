"""The Model Router page ships in both UIs, identical, with its nav item, and the dashboard serves it."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_the_panel_is_identical_in_both_uis():
    dash = (ROOT / "bot/dashboard/static/router-panel.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/router-panel.js").read_text(encoding="utf-8")
    assert dash == desk


def test_both_pages_mount_it_with_a_nav_item():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert '<section id="router">' in text and 'id="rt-root"' in text and 'href="#router"' in text
        assert re.search(r'<script src="(/static/)?router-panel\.js"></script>', text)


def test_the_panel_only_calls_router_routes_that_exist(temp_db):
    from bot.dashboard.server import build_app

    app = build_app()
    routes = {getattr(r, "path", "") for r in app.routes}
    js = (ROOT / "bot/dashboard/static/router-panel.js").read_text(encoding="utf-8")
    called = set(re.findall(r"'(/api/router/[a-z/]+)", js)) | set(re.findall(r"`(/api/router/[a-z/]+)", js))
    assert called
    for path in called:
        stem = path.rstrip("/")
        assert any(r == stem or r.startswith(stem + "/{") or r.startswith(stem + "/") for r in routes), path
    assert TestClient(app).get("/static/router-panel.js").status_code == 200
