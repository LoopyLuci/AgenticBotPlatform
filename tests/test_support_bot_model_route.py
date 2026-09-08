"""bot/support_bot/hybrid.py's export_current_model() and the
GET /api/support-bot/model route it backs (Phase 6 of the "Support Bot
NLU upgrade" plan) — the file a Kotlin engine on Android loads for local
classification.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bot.dashboard.server import build_app
from bot.support_bot import hybrid, model_io


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(model_io, "CURRENT_PATH", tmp_path / "current.json")
    return TestClient(build_app())


def _auth():
    return {"X-Dashboard-Token": "test-token"}


def test_export_current_model_falls_back_to_live_singletons_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(model_io, "CURRENT_PATH", tmp_path / "current.json")

    data = hybrid.export_current_model()

    assert data["format_version"] == model_io.FORMAT_VERSION
    assert data["tfidf"]["confidence_threshold"] == 0.22
    assert data["nn"]["confidence_threshold"] == 0.35
    assert "idf" in data["tfidf"] and "centroids" in data["tfidf"]
    assert "w1" in data["nn"] and "vocab" in data["nn"]


def test_export_current_model_prefers_a_persisted_file(tmp_path, monkeypatch):
    scratch = tmp_path / "current.json"
    monkeypatch.setattr(model_io, "CURRENT_PATH", scratch)
    model_io.save_model(
        {"idf": {}, "centroids": {}, "confidence_threshold": 0.22}, {"vocab": {}, "idf": [], "classes": [],
         "hidden_units": 64, "w1": [], "b1": [], "w2": [], "b2": [], "confidence_threshold": 0.35},
        [], training_hash="sha256:seed", path=scratch,
    )

    data = hybrid.export_current_model()

    assert data["training_data_hash"] == "sha256:seed"


def test_model_route_returns_a_valid_model(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.get("/api/support-bot/model", headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert body["format_version"] == model_io.FORMAT_VERSION
    assert "tfidf" in body and "nn" in body
