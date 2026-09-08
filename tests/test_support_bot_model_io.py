"""bot/support_bot/model_io.py — versioned JSON persistence for the
Support Bot's two classifiers (Phase 1 of the "Support Bot NLU upgrade"
plan). Save->load must reproduce identical classifications to the
in-memory model, since this file format is what a future Kotlin engine
on Android will also load and must classify identically against.
"""
from __future__ import annotations

from bot.support_bot import hybrid, model_io
from bot.support_bot import model as tfidf_model
from bot.support_bot import nn_model as neural_model
from bot.support_bot.model import TfidfCentroidModel
from bot.support_bot.nn_model import NeuralIntentClassifier
from bot.support_bot.training_data import EXAMPLES


def test_compute_training_hash_is_order_independent():
    a = [("restart the bot", "bot_restart"), ("what is my status", "status")]
    b = list(reversed(a))
    assert model_io.compute_training_hash(a) == model_io.compute_training_hash(b)


def test_compute_training_hash_changes_with_content():
    a = [("restart the bot", "bot_restart")]
    b = [("restart the bot", "bot_disable")]
    assert model_io.compute_training_hash(a) != model_io.compute_training_hash(b)


def test_save_and_load_model_roundtrip(tmp_path):
    path = tmp_path / "current.json"
    intents = sorted({intent for _, intent in EXAMPLES})
    model_io.save_model(
        {"idf": {"restart": 1.5}, "centroids": {"bot_restart": {"restart": 1.5}}},
        {"vocab": {"restart": 0}, "idf": [1.5], "classes": ["bot_restart"], "hidden_units": 64,
         "w1": [[0.1]], "b1": [0.0], "w2": [[0.1]], "b2": [0.0]},
        intents, training_hash="sha256:abc", path=path,
    )

    data = model_io.load_model(path=path)

    assert data["format_version"] == model_io.FORMAT_VERSION
    assert data["training_data_hash"] == "sha256:abc"
    assert data["intents"] == intents
    assert data["tfidf"]["idf"] == {"restart": 1.5}
    assert data["nn"]["classes"] == ["bot_restart"]


def test_load_model_returns_none_for_missing_file(tmp_path):
    assert model_io.load_model(path=tmp_path / "nonexistent.json") is None


def test_load_model_returns_none_for_corrupt_json(tmp_path):
    path = tmp_path / "current.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert model_io.load_model(path=path) is None


def test_load_model_returns_none_for_wrong_format_version(tmp_path):
    path = tmp_path / "current.json"
    path.write_text('{"format_version": 999}', encoding="utf-8")
    assert model_io.load_model(path=path) is None


def test_tfidf_export_load_state_roundtrip_preserves_predictions():
    original = TfidfCentroidModel(list(EXAMPLES))
    restored = TfidfCentroidModel([("placeholder text", "help")])  # different training, will be overwritten

    restored.load_state(original.export_state())

    for text, _ in EXAMPLES[:20]:
        assert restored.predict(text) == original.predict(text)


def test_nn_export_load_state_roundtrip_preserves_predictions():
    original = NeuralIntentClassifier()
    restored = NeuralIntentClassifier()  # different random init/training, will be overwritten

    restored.load_state(original.export_state())

    for text, _ in EXAMPLES[:20]:
        orig_intent, orig_conf = original.predict(text)
        rest_intent, rest_conf = restored.predict(text)
        assert rest_intent == orig_intent
        assert abs(rest_conf - orig_conf) < 1e-9


def test_hybrid_retrain_all_persists_and_reload_reproduces_classification(tmp_path, monkeypatch):
    scratch = tmp_path / "current.json"
    monkeypatch.setattr(model_io, "CURRENT_PATH", scratch)

    hybrid.retrain_all()
    assert scratch.exists()

    before = hybrid.classify("restart the bot", log=False)

    # Simulate a fresh process: retrain both singletons back to a
    # different (arbitrary) state, then reload from the persisted file
    # and confirm classification is restored, not left at the arbitrary
    # state.
    tfidf_model.model.load_state(TfidfCentroidModel([("something else entirely", "help")]).export_state())
    hybrid._load_persisted_state_if_present()

    after = hybrid.classify("restart the bot", log=False)
    assert after.intent == before.intent
    assert after.tfidf_intent == before.tfidf_intent


def test_load_persisted_state_is_a_noop_when_no_file_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(model_io, "CURRENT_PATH", tmp_path / "nonexistent.json")
    # Must not raise.
    hybrid._load_persisted_state_if_present()
