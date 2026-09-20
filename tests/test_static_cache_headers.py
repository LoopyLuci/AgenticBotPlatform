"""Static assets (dashboard.html, /static/*, /desktop-ui/*) must be served
with Cache-Control: no-cache. Without it a browser/WebView2 applies a
heuristic freshness lifetime from Last-Modified and can keep running
JS/HTML from BEFORE an app update after a normal reload — confirmed live:
a rebuilt server's real <script> load returned the pre-fix bytes while a
plain fetch()/curl saw the fresh file. ETag/Last-Modified stay in place,
so an unchanged file still costs only a cheap 304, not a re-download.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot.dashboard.server import build_app


def test_index_is_served_no_cache(temp_db):
    resp = TestClient(build_app()).get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_static_js_is_served_no_cache(temp_db):
    resp = TestClient(build_app()).get("/static/terminal-panel.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert "etag" in resp.headers  # revalidation still possible


def test_desktop_ui_is_served_no_cache(temp_db):
    resp = TestClient(build_app()).get("/desktop-ui/main.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_an_unchanged_static_file_still_revalidates_to_304(temp_db):
    client = TestClient(build_app())
    first = client.get("/static/terminal-panel.js")
    second = client.get("/static/terminal-panel.js", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
