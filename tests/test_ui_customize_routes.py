"""bot/dashboard/server.py's /api/ui-customize routes — exercised
against the real FastAPI app via TestClient, matching the precedent set
by tests/test_hotreload_route.py / test_agent_settings_routes.py. The
generation call itself is faked (no real model call) since these tests
are about routing/auth/error-shape, not the generation logic already
covered by tests/test_ui_customize.py.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from bot import ui_customize
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def _isolate(tmp_path, monkeypatch):
    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text(
        '<html><body><section id="overview">ok</section>'
        "<script>function getToken(){} function connectLiveEventsSocket(){}</script>"
        "</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(ui_customize, "TARGETS", {"dashboard": dashboard})
    monkeypatch.setattr(ui_customize, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(ui_customize, "PENDING_ROOT", tmp_path / "history" / "pending")
    monkeypatch.setattr(ui_customize, "BACKUPS_ROOT", tmp_path / "history" / "backups")
    monkeypatch.setattr(ui_customize, "MANIFEST_PATH", tmp_path / "history" / "manifest.json")
    return dashboard


def test_generate_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/ui-customize/generate", json={"target": "dashboard", "instruction": "x"})
    assert resp.status_code == 401


def test_generate_returns_a_valid_change(temp_db, monkeypatch, tmp_path):
    dashboard = _isolate(tmp_path, monkeypatch)
    original = dashboard.read_text(encoding="utf-8")
    new_content = original.replace("ok</section>", 'ok<button id="new-btn">Ping</button></section>')

    async def fake_generate_raw(target, instruction, current_content):
        return "added a button", new_content

    monkeypatch.setattr(ui_customize, "_generate_raw", fake_generate_raw)

    client = _client(monkeypatch)
    resp = client.post(
        "/api/ui-customize/generate",
        json={"target": "dashboard", "instruction": "add a ping button"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert "new-btn" in body["preview_html"]
    # Route must not have written the real file.
    assert dashboard.read_text(encoding="utf-8") == original


def test_apply_writes_the_file_and_history_shows_it(temp_db, monkeypatch, tmp_path):
    dashboard = _isolate(tmp_path, monkeypatch)
    original = dashboard.read_text(encoding="utf-8")
    new_content = original.replace("ok</section>", 'ok<button id="new-btn">Ping</button></section>')

    async def fake_generate_raw(target, instruction, current_content):
        return "added a button", new_content

    monkeypatch.setattr(ui_customize, "_generate_raw", fake_generate_raw)
    monkeypatch.setattr(ui_customize, "_broadcast_static_file_changed", lambda t: None)

    client = _client(monkeypatch)
    gen = client.post(
        "/api/ui-customize/generate",
        json={"target": "dashboard", "instruction": "add a ping button"},
        headers=_auth(),
    ).json()

    apply_resp = client.post("/api/ui-customize/apply", json={"change_id": gen["change_id"]}, headers=_auth())
    assert apply_resp.status_code == 200
    assert dashboard.read_text(encoding="utf-8") == new_content

    hist = client.get("/api/ui-customize/history", headers=_auth()).json()["history"]
    assert hist[0]["entry_id"] == apply_resp.json()["entry_id"]

    revert_resp = client.post("/api/ui-customize/revert", json={"entry_id": apply_resp.json()["entry_id"]}, headers=_auth())
    assert revert_resp.status_code == 200
    assert dashboard.read_text(encoding="utf-8") == original


def test_apply_rejects_unknown_change_id(temp_db, monkeypatch, tmp_path):
    _isolate(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    resp = client.post("/api/ui-customize/apply", json={"change_id": "nope"}, headers=_auth())
    assert resp.status_code == 400
