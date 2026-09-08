"""bot/dashboard/server.py's /api/support-bot/generate|pending routes —
Phase 4 of the "Support Bot NLU upgrade" plan. /generate is mocked at
bot.support_bot.synthetic_gen.generate_synthetic_batch — never a real
swarm/LLM call from a route test.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db
from bot.dashboard.server import build_app
from bot.support_bot import model_io


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(model_io, "CURRENT_PATH", tmp_path / "current.json")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def test_generate_route_calls_synthetic_gen(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    async def _fake(**kw):
        return {"dispatched": 2, "pending_added": 3, "dispatch_id": "d1"}

    monkeypatch.setattr("bot.support_bot.synthetic_gen.generate_synthetic_batch", _fake)

    resp = client.post("/api/support-bot/generate", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == {"dispatched": 2, "pending_added": 3, "dispatch_id": "d1"}


def test_pending_list_defaults_to_pending_status(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    db.add_support_bot_pending_example("reboot please", "status", source_provider="alpha", source_model="m1")

    resp = client.get("/api/support-bot/pending", headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["phrase"] == "reboot please"
    assert body[0]["status"] == "pending"


def test_approve_pending_example_adds_phrase_and_retrains(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    pending_id = db.add_support_bot_pending_example("reboot please", "status", source_provider="alpha", source_model="m1")

    resp = client.post(f"/api/support-bot/pending/{pending_id}/approve", headers=_auth())

    assert resp.status_code == 200
    assert db.get_support_bot_pending_example(pending_id)["status"] == "approved"
    phrases = db.list_support_bot_phrases()
    assert any(p["phrase"] == "reboot please" and p["intent"] == "status" for p in phrases)


def test_reject_pending_example_never_touches_phrases(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    pending_id = db.add_support_bot_pending_example("reboot please", "status", source_provider="alpha", source_model="m1")

    resp = client.post(f"/api/support-bot/pending/{pending_id}/reject", headers=_auth())

    assert resp.status_code == 200
    assert db.get_support_bot_pending_example(pending_id)["status"] == "rejected"
    assert db.list_support_bot_phrases() == []


def test_approve_a_nonexistent_pending_example_is_404(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.post("/api/support-bot/pending/999999/approve", headers=_auth())
    assert resp.status_code == 404


def test_approve_an_already_resolved_example_is_404(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    pending_id = db.add_support_bot_pending_example("reboot please", "status", source_provider="alpha", source_model="m1")
    db.resolve_support_bot_pending_example(pending_id, "approved")

    resp = client.post(f"/api/support-bot/pending/{pending_id}/approve", headers=_auth())

    assert resp.status_code == 404
