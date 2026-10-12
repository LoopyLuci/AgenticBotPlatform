"""The Skills screen: one panel file shipped identically in both UIs, text-safe, and approvals stay a person's write-auth action."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "bot/dashboard/static/skills-panel.js"
H = {"X-Dashboard-Token": "test-token"}


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_the_panel_is_identical_in_both_uis():
    assert JS.read_text(encoding="utf-8") == (ROOT / "desktop-app/ui/skills-panel.js").read_text(encoding="utf-8")


def test_both_pages_mount_it_with_a_nav_item():
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert re.search(r'<section id="skills"[^>]*>', text) and 'id="sk-root"' in text and 'href="#skills"' in text
        assert re.search(r'<script src="(/static/)?skills-panel\.js"></script>', text)


def test_skill_content_goes_through_the_escaping_helper():
    js = JS.read_text(encoding="utf-8")
    assert "const E =" in js and "esc(s)" in js
    assert '<div class="sk-pre">${E(d.text)}</div>' in js
    assert not re.search(r"\$\{(d|h|p|f|c|ev)\.(text|name|description|message|file|detail|source|id)\}", js), "values go through E()"
    assert "http://" not in js
    assert "insertAdjacentHTML" not in js and "outerHTML" not in js


def test_approval_asks_first_and_uses_the_write_routes():
    js = JS.read_text(encoding="utf-8")
    assert "window.confirm" in js
    for route in ("/api/skills/quarantine/approve", "/api/skills/quarantine/reject", "/api/skills/drafts/approve",
                  "/api/skills/drafts/reject", "/api/skills/fetch"):
        assert route in js
    assert "A pack with blocking findings cannot be approved" in js


def test_the_panel_only_calls_routes_that_exist(client):
    paths = {getattr(r, "path", "") for r in client.app.routes}
    js = JS.read_text(encoding="utf-8")
    called = set(re.findall(r"'(/api/skills/[a-z/]+)", js))
    assert called
    for c in called:
        assert c.rstrip("/") in paths or c.rstrip("/") + "/{name}/evaluation" in paths, c
    assert "/api/skills/drafts/{name}/evaluation" in paths
    assert client.get("/static/skills-panel.js").status_code == 200


def test_approve_and_fetch_require_write_auth_and_reads_work(client):
    for path in ("/api/skills/quarantine/approve", "/api/skills/quarantine/reject", "/api/skills/drafts/approve", "/api/skills/drafts/reject"):
        assert client.post(path, json={"name": "x"}).status_code == 401
    assert client.post("/api/skills/fetch", json={"url": "https://github.com/a/b"}).status_code == 401
    for path in ("/api/skills/packs", "/api/skills/quarantine", "/api/skills/drafts"):
        assert client.get(path, headers=H).status_code == 200
        assert client.get(path).status_code == 401
