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


def _isolate_modules(monkeypatch, tmp_path):
    from bot.support_bot import cascade, module_manifest

    monkeypatch.setattr(module_manifest, "MANIFEST_PATH", tmp_path / "manifest.json")
    monkeypatch.setattr(module_manifest, "MODULES_DIR", tmp_path / "modules")
    cascade._module_classifiers.clear()


def test_model_route_with_unknown_module_returns_404(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)

    resp = client.get("/api/support-bot/model", params={"module": "not_a_real_module"}, headers=_auth())
    assert resp.status_code == 404


def test_model_route_with_module_falls_back_to_a_fresh_build(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)

    resp = client.get("/api/support-bot/model", params={"module": "bots"}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["format_version"] == model_io.FORMAT_VERSION
    assert "bot_create" in body["intents"]
    assert "status" not in body["intents"]  # "status" belongs to core_status, not "bots"


def test_model_route_with_module_prefers_a_persisted_file(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)
    from bot.support_bot import cascade

    cascade.retrain_module("bots")

    resp = client.get("/api/support-bot/model", params={"module": "bots"}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["training_data_hash"].startswith("sha256:")


def test_manifest_route_lists_every_module(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)
    from bot.support_bot import knowledge_modules

    resp = client.get("/api/support-bot/manifest", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == set(knowledge_modules.all_module_ids())
    assert body["bots"]["enabled"] is True
    assert "bot_create" in body["bots"]["intents"]
    assert body["core_status"]["unloadable"] is False


def test_classify_route_returns_a_decision(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/classify", json={"text": "restart bot X"}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "bot_restart"
    assert 0.0 <= body["confidence"] <= 1.0
    assert body["server_model_version"].startswith("sha256:")


def test_classify_route_rejects_empty_text(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/classify", json={"text": "   "}, headers=_auth())
    assert resp.status_code == 400


def test_classify_route_requires_auth(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/classify", json={"text": "restart bot X"})
    assert resp.status_code in (401, 403)


def test_module_set_enabled_route_toggles_and_persists(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)
    from bot.support_bot import module_manifest

    resp = client.post("/api/support-bot/modules/bots/enabled", json={"enabled": False}, headers=_auth())
    assert resp.status_code == 200
    assert module_manifest.is_enabled("bots") is False


def test_module_set_enabled_route_rejects_disabling_core_status(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/modules/core_status/enabled", json={"enabled": False}, headers=_auth())
    assert resp.status_code == 400


def test_module_set_enabled_route_requires_a_bool(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/modules/bots/enabled", json={"enabled": "yes"}, headers=_auth())
    assert resp.status_code == 400


def test_module_retrain_route_persists_and_returns_result(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)
    from bot.support_bot import module_manifest

    resp = client.post("/api/support-bot/modules/bots/retrain", json={}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert module_manifest.module_path("bots").exists()


def test_module_retrain_route_rejects_unknown_module(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _isolate_modules(monkeypatch, tmp_path)

    resp = client.post("/api/support-bot/modules/not_a_real_module/retrain", json={}, headers=_auth())
    assert resp.status_code == 404


def test_classify_route_falls_through_to_tier2_on_nonsense(temp_db, monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    from bot.support_bot import llm_fallback

    async def fake_classify_and_record(text, candidate_intents):
        return ("mcp_list", 0.5)

    monkeypatch.setattr(llm_fallback, "classify_and_record", fake_classify_and_record)

    resp = client.post("/api/support-bot/classify", json={"text": "asdkjfh qwoeiru nonsense"}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "mcp_list"
    assert body["source"] == "llm_fallback"
