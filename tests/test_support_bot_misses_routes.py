"""bot/dashboard/server.py's /api/support-bot/misses routes — Phase 5 of
the "Support Bot NLU upgrade" plan (active-learning review of real
disagreements/unknowns from support_bot_classifications).
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


def _log_miss(text="reboot the thing", tfidf_intent="unknown", nn_intent="unknown", final_intent="unknown", agreed=False):
    db.log_support_bot_classification(
        text=text, tfidf_intent=tfidf_intent, tfidf_confidence=0.1, nn_intent=nn_intent, nn_confidence=0.1,
        final_intent=final_intent, final_confidence=0.1, source="unknown", agreed=agreed,
    )


def test_misses_route_surfaces_a_real_disagreement(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _log_miss(tfidf_intent="bot_restart", nn_intent="status", final_intent="bot_restart", agreed=False)

    resp = client.get("/api/support-bot/misses", headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["text"] == "reboot the thing"


def test_misses_route_excludes_a_clean_agreement(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    db.log_support_bot_classification(
        text="what's the status", tfidf_intent="status", tfidf_confidence=0.9, nn_intent="status", nn_confidence=0.9,
        final_intent="status", final_confidence=0.9, source="ensemble", agreed=True,
    )

    resp = client.get("/api/support-bot/misses", headers=_auth())

    assert resp.json() == []


def test_labeling_a_miss_adds_a_phrase_and_marks_reviewed(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _log_miss()
    classification_id = db.get_recent_misses()[0]["id"]

    resp = client.post(f"/api/support-bot/misses/{classification_id}/label", headers=_auth(), json={"intent": "bot_restart"})

    assert resp.status_code == 200
    phrases = db.list_support_bot_phrases()
    assert any(p["phrase"] == "reboot the thing" and p["intent"] == "bot_restart" for p in phrases)


def test_labeled_miss_no_longer_appears_in_unreviewed_list(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _log_miss()
    classification_id = db.get_recent_misses()[0]["id"]
    client.post(f"/api/support-bot/misses/{classification_id}/label", headers=_auth(), json={"intent": "bot_restart"})

    resp = client.get("/api/support-bot/misses", headers=_auth())

    assert resp.json() == []


def test_labeling_requires_an_intent(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _log_miss()
    classification_id = db.get_recent_misses()[0]["id"]

    resp = client.post(f"/api/support-bot/misses/{classification_id}/label", headers=_auth(), json={})

    assert resp.status_code == 400


def test_labeling_a_nonexistent_classification_is_404(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.post("/api/support-bot/misses/999999/label", headers=_auth(), json={"intent": "status"})
    assert resp.status_code == 404
