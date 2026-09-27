"""The Sentinel panel ships in both UIs, identical, and is wired into each page."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_the_panel_is_identical_in_both_uis():
    dash = (ROOT / "bot/dashboard/static/sentinel-panel.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/sentinel-panel.js").read_text(encoding="utf-8")
    assert dash == desk


def test_both_pages_mount_and_load_it():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="sn-root"' in text
        assert re.search(r'<script src="(/static/)?sentinel-panel\.js"></script>', text)


def test_the_dashboard_serves_it(temp_db):
    from bot.dashboard.server import build_app

    assert TestClient(build_app()).get("/static/sentinel-panel.js").status_code == 200
