"""bot/dashboard/server.py's /api/support-bot/training|retrain|health
routes — Phase 3 of the "Support Bot NLU upgrade" plan (bulk phrase
import, an explicit retrain-and-evaluate action, and health's new eval
block). Every test redirects bot.support_bot.model_io.CURRENT_PATH to a
scratch path — retraining is real (not mocked) and must never write to
the real data/support_bot_models/current.json on disk.
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


def test_single_phrase_add_still_works(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/training", headers=_auth(), json={"phrase": "reboot the bot", "intent": "bot_restart"})

    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body and "ids" not in body
    assert body["trained_on"]["accepted"] is True


def test_bulk_phrase_import(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/training", headers=_auth(), json={"phrases": [
        {"phrase": "reboot the bot", "intent": "bot_restart"},
        {"phrase": "kick the bot back on", "intent": "bot_restart"},
    ]})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["ids"]) == 2
    assert len(db.list_support_bot_phrases()) == 2


def test_bulk_import_rejects_a_malformed_item(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/training", headers=_auth(), json={"phrases": [
        {"phrase": "reboot the bot", "intent": "bot_restart"},
        {"phrase": "", "intent": "bot_restart"},
    ]})

    assert resp.status_code == 400
    # Nothing should have been committed from a request that ultimately failed.
    assert len(db.list_support_bot_phrases()) == 0


def test_explicit_retrain_route_returns_eval(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/retrain", headers=_auth(), json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["eval"]["holdout_accuracy"] is not None


def test_explicit_retrain_route_rejects_with_custom_tolerance(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    from bot.support_bot import hybrid

    model_io.save_model(
        hybrid.tfidf_model.model.export_state(), hybrid.neural_model.nn_model.export_state(),
        [], training_hash="sha256:seed", eval_result={"holdout_accuracy": 1.0}, path=tmp_path / "current.json",
    )

    resp = client.post("/api/support-bot/retrain", headers=_auth(), json={"accept_if_regression_under": 0.001})

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False


def test_health_includes_eval_block_after_a_retrain(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    client.post("/api/support-bot/retrain", headers=_auth(), json={})

    resp = client.get("/api/support-bot/health", headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["eval"]["holdout_accuracy"] is not None


def test_health_eval_block_empty_when_never_retrained_with_gate(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.get("/api/support-bot/health", headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["eval"] == {}
