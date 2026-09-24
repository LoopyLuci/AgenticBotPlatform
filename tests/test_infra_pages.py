"""The Tailscale / Containers / VMs / Infra Automation pages are in both UIs, identical, and wired in."""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGES = {
    "dashboard": (ROOT / "bot/dashboard/static/dashboard.html").read_text(encoding="utf-8"),
    "desktop": (ROOT / "desktop-app/ui/index.html").read_text(encoding="utf-8"),
}
SECTIONS = {"tailscale": "ts-root", "containers": "ct-root", "vms": "vm-root", "infra-rules": "ir-root"}


def test_the_panel_script_is_identical_in_both_apps():
    dash = (ROOT / "bot/dashboard/static/infra-panel.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/infra-panel.js").read_text(encoding="utf-8")
    assert dash == desk
    for route in ("/api/tailscale/", "/api/docker/", "/api/vms/", "/api/infra/rules"):
        assert route in dash


@pytest.mark.parametrize("name", ["dashboard", "desktop"])
@pytest.mark.parametrize("section,root", SECTIONS.items())
def test_each_ui_has_each_page_its_nav_item_and_the_script(name, section, root):
    text = PAGES[name]
    assert f'<section id="{section}">' in text and f'id="{root}"' in text
    assert f'href="#{section}"' in text
    assert re.search(r'<script src="(/static/)?infra-panel\.js"></script>', text)


def test_the_dashboard_serves_the_script(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from bot.dashboard.server import build_app
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    assert TestClient(build_app()).get("/static/infra-panel.js").status_code == 200
