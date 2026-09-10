"""bot/dashboard/server.py's /desktop-ui static mount — the piece that
makes desktop-app/ui/* editable without a `cargo tauri build`: the
compiled Tauri window navigates here once booted (see
desktop-app/ui/main.js's hideBootOverlay()), and StaticFiles serves
whatever's currently on disk, with zero caching.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot.dashboard import server as server_module
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_desktop_ui_serves_index_html_fresh_from_disk(temp_db, monkeypatch, tmp_path):
    desktop_ui_dir = tmp_path / "desktop-ui"
    desktop_ui_dir.mkdir()
    (desktop_ui_dir / "index.html").write_text("<html><body>v1</body></html>", encoding="utf-8")
    (desktop_ui_dir / "main.js").write_text("console.log('v1');", encoding="utf-8")
    monkeypatch.setattr(server_module, "DESKTOP_UI_DIR", desktop_ui_dir)

    client = _client(monkeypatch)
    resp = client.get("/desktop-ui/")
    assert resp.status_code == 200
    assert "v1" in resp.text

    resp_js = client.get("/desktop-ui/main.js")
    assert resp_js.status_code == 200
    assert "v1" in resp_js.text

    # No caching layer — a plain overwrite must be visible on the very
    # next request, same guarantee the existing /static mount already
    # provides for dashboard.html's own assets.
    (desktop_ui_dir / "index.html").write_text("<html><body>v2</body></html>", encoding="utf-8")
    resp2 = client.get("/desktop-ui/")
    assert "v2" in resp2.text


def test_desktop_ui_mount_is_absent_when_the_directory_does_not_exist(temp_db, monkeypatch, tmp_path):
    """A headless-only deployment with no desktop-app/ checkout must not
    fail to build the app at all — the mount is simply skipped."""
    monkeypatch.setattr(server_module, "DESKTOP_UI_DIR", tmp_path / "does-not-exist")
    client = _client(monkeypatch)
    resp = client.get("/desktop-ui/")
    assert resp.status_code == 404
