"""bot/dashboard/server.py's /api/platform-guides and /api/validate-field
routes — exercised against the real FastAPI app via TestClient, matching
the precedent in tests/test_hotreload_route.py, so a route wiring mistake
fails here rather than only in the Add-a-bot form's live validation.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_platform_guides_needs_no_auth_and_covers_all_five_platforms(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/platform-guides")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"telegram", "discord", "slack", "matrix", "whatsapp"}
    for platform, guide in body.items():
        assert guide["label"]
        assert guide["fields"]
        assert guide["setup_guide"]


def test_validate_field_route_requires_token(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/validate-field", json={"platform": "telegram", "field": "bot_token", "value": "x"})
    assert resp.status_code == 401


def test_validate_field_route_returns_validator_result(temp_db, monkeypatch):
    client = _client(monkeypatch)
    resp = client.post(
        "/api/validate-field",
        headers={"X-Dashboard-Token": "test-token"},
        json={"platform": "whatsapp", "field": "verify_token", "value": "short-enough-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["message"]
