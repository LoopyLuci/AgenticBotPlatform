"""bot/support_bot/cascade.py — Tier 0 of the Support Bot NLU cascade:
per-Knowledge-Module classical classifiers + the cross-module reduction.
Phase 2/3 of the next-generation modular hybrid plan.
"""
from __future__ import annotations

from bot.support_bot import cascade, knowledge_modules, module_manifest


def _isolate_module_storage(monkeypatch, tmp_path):
    """Redirects module_manifest's MANIFEST_PATH/MODULES_DIR (both
    body-level global lookups, not bound-at-def-time defaults — see that
    module's own docstring) to a throwaway directory, so a test's
    retrain_module()/set_enabled() call can never touch the real
    data/support_bot_models/ this checkout's live desktop testing uses."""
    monkeypatch.setattr(module_manifest, "MANIFEST_PATH", tmp_path / "manifest.json")
    monkeypatch.setattr(module_manifest, "MODULES_DIR", tmp_path / "modules")


def setup_function(_fn):
    cascade._module_classifiers.clear()


def teardown_function(_fn):
    cascade._module_classifiers.clear()


def test_reduce_cross_module_picks_highest_confidence():
    intent, confidence, source, module_id = cascade.reduce_cross_module({
        "bots": ("bot_create", 0.4, "tfidf"),
        "mcp": ("mcp_list", 0.9, "nn"),
    })
    assert (intent, confidence, source, module_id) == ("mcp_list", 0.9, "nn", "mcp")


def test_reduce_cross_module_ties_favor_tfidf():
    intent, confidence, source, module_id = cascade.reduce_cross_module({
        "bots": ("bot_create", 0.5, "nn"),
        "mcp": ("mcp_list", 0.5, "tfidf"),
    })
    assert (intent, source, module_id) == ("mcp_list", "tfidf", "mcp")


def test_reduce_cross_module_everything_unknown_returns_unknown_with_no_module():
    intent, confidence, source, module_id = cascade.reduce_cross_module({
        "bots": ("unknown", 0.1, "unknown"),
        "mcp": ("unknown", 0.05, "unknown"),
    })
    assert (intent, confidence, source, module_id) == ("unknown", 0.0, "unknown", None)


def test_reduce_cross_module_single_confident_module_wins_over_unknowns():
    intent, confidence, source, module_id = cascade.reduce_cross_module({
        "bots": ("bot_create", 0.6, "ensemble"),
        "mcp": ("unknown", 0.1, "unknown"),
    })
    assert (intent, confidence, source, module_id) == ("bot_create", 0.6, "ensemble", "bots")


def test_build_module_classifiers_trains_only_on_that_modules_intents():
    pair = cascade.build_module_classifiers("bots")
    bots_intents = set(knowledge_modules.intents_for_module("bots"))
    assert set(pair.tfidf._centroids.keys()) <= bots_intents
    assert set(pair.nn._classes) <= bots_intents


def test_get_module_classifiers_caches_and_reuses(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    first = cascade.get_module_classifiers("mcp")
    second = cascade.get_module_classifiers("mcp")
    assert first is second


def test_tiny_module_gets_an_always_unknown_nn_stand_in():
    # "mcp" has ~13 examples across 4 intents — below
    # _MIN_EXAMPLES_FOR_MODULE_NN, so its NN sub-model must be the
    # always-defers stub, not a from-scratch-trained (and easily
    # overconfident-on-garbage) NeuralIntentClassifier.
    pair = cascade.build_module_classifiers("mcp")
    assert isinstance(pair.nn, cascade._AlwaysUnknownClassifier)
    assert pair.nn.predict("anything at all") == ("unknown", 0.0)


def test_classify_tier0_finds_an_in_module_phrase():
    result = cascade.classify_tier0("create a new bot", enabled_modules=["bots"])
    assert result.intent == "bot_create"
    assert result.module_id == "bots"
    assert result.confidence > 0.0


def test_classify_tier0_returns_unknown_when_every_module_is_unsure():
    result = cascade.classify_tier0("asdkjfh qwoeiru zzzzz nonsense", enabled_modules=["bots", "mcp"])
    assert result.intent == "unknown"
    assert result.module_id is None
    assert set(result.per_module.keys()) == {"bots", "mcp"}


def test_classify_tier0_only_considers_the_given_modules():
    # "engage the emergency stop" is squarely an estop-module phrase — with
    # estop excluded, no other module should confidently claim it.
    result = cascade.classify_tier0("engage the emergency stop", enabled_modules=["bots", "mcp"])
    assert result.intent != "estop_engage"


def test_classify_tier0_picks_the_higher_confidence_module_result():
    result = cascade.classify_tier0("restart bot X", enabled_modules=["bots", "desktop_app"])
    # "restart bot X" is a bots-module phrase almost verbatim from
    # training_data.py — it should win over desktop_app's own restart-ish
    # phrasings ("restart claude desktop").
    assert result.intent == "bot_restart"
    assert result.module_id == "bots"


def test_classify_tier0_respects_currently_enabled_modules_by_default(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    module_manifest.set_enabled("bots", False)

    result = cascade.classify_tier0("create a new bot")
    # "bots" is disabled, so nothing should confidently claim bot_create —
    # confirms classify_tier0() reads the live manifest when no explicit
    # enabled_modules list is passed.
    assert result.intent != "bot_create"


def test_retrain_module_without_regression_gate_persists_and_records_version(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    original = cascade.get_module_classifiers("bots")

    result = cascade.retrain_module("bots")

    assert result["accepted"] is True
    assert result["eval"] is None
    rebuilt = cascade._module_classifiers["bots"]
    assert rebuilt is not original
    manifest = module_manifest.load_manifest()
    assert manifest["bots"]["version"] is not None
    assert module_manifest.module_path("bots").exists()


def test_retrain_module_rejects_unknown_module(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    import pytest

    with pytest.raises(ValueError):
        cascade.retrain_module("not_a_real_module")


def test_retrain_module_persisted_state_survives_cache_clear(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    cascade.retrain_module("bots")

    # Simulate a fresh process: clear the in-memory cache, then confirm
    # get_module_classifiers() loads the persisted state back rather than
    # silently retraining from scratch (mirrors hybrid.py's own
    # _load_persisted_state_if_present() contract).
    cascade._module_classifiers.clear()
    pair = cascade.get_module_classifiers("bots")
    result = pair.tfidf.predict("create a new bot")
    assert result[0] == "bot_create"


def test_retrain_module_with_regression_gate_rejects_a_real_regression(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    # First accepted retrain establishes a baseline holdout accuracy.
    first = cascade.retrain_module("bots", accept_if_regression_under=0.5)
    assert first["accepted"] is True
    baseline_accuracy = first["eval"]["holdout_accuracy"]

    # An impossibly strict tolerance (0.0 = "no regression at all
    # allowed") against the SAME data must still accept, since nothing
    # changed — sanity check before testing an actual rejection path.
    same_again = cascade.retrain_module("bots", accept_if_regression_under=0.0)
    assert same_again["accepted"] is True

    # A monkeypatched evaluate() that reports a much worse accuracy must
    # be rejected — the persisted file must be untouched (still the
    # baseline's own hash), not overwritten by the "regressed" attempt.
    from bot.support_bot import eval as eval_mod

    def _fake_evaluate(train, holdout):
        return {"holdout_accuracy": 0.0, "per_intent": {}, "n_train": len(train), "n_holdout": len(holdout)}

    monkeypatch.setattr(eval_mod, "evaluate", _fake_evaluate)
    before = module_manifest.load_manifest()["bots"]["version"]
    rejected = cascade.retrain_module("bots", accept_if_regression_under=0.01)
    assert rejected["accepted"] is False
    assert "regressed" in rejected["reason"]
    after = module_manifest.load_manifest()["bots"]["version"]
    assert after == before  # untouched — rejection never persists


def test_rebuild_all_modules_retrains_every_registered_module(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    results = cascade.rebuild_all_modules()
    assert set(results.keys()) == set(knowledge_modules.all_module_ids())
    assert all(r["accepted"] for r in results.values())


def test_classify_tier1_is_a_pure_passthrough_when_embeddings_disabled(monkeypatch):
    from bot.support_bot import embeddings

    monkeypatch.setattr(embeddings, "is_enabled", lambda: False)
    intent, confidence, source = cascade.classify_tier1("restart bot X")
    assert intent == "bot_restart"
    assert source in ("ensemble", "tfidf", "nn")


def test_classify_tier1_degrades_gracefully_when_embeddings_enabled_but_unavailable(monkeypatch):
    from bot.support_bot import embeddings

    monkeypatch.setattr(embeddings, "is_enabled", lambda: True)

    def _raise(*_a, **_kw):
        raise embeddings.EmbeddingsUnavailableError("not installed")

    monkeypatch.setattr(embeddings, "get_or_build_global_index", _raise)
    intent, confidence, source = cascade.classify_tier1("restart bot X")
    assert intent == "bot_restart"  # falls back to the classical result unchanged


def test_classify_tier1_fuses_in_an_embedding_result_when_enabled(monkeypatch):
    from bot.support_bot import embeddings

    monkeypatch.setattr(embeddings, "is_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "get_or_build_global_index", lambda examples: embeddings.ExemplarIndex([], [], []))
    monkeypatch.setattr(embeddings, "classify_via_embeddings", lambda text, index: ("mcp_list", 0.9))
    monkeypatch.setattr(embeddings, "_cfg", lambda: {"fusion": "boost", "weight": 0.3})

    # Nonsense text the classical model itself has no confident answer
    # for — Tier 1.5's (mocked) embedding opinion should supply the
    # final answer under "boost" fusion.
    intent, confidence, source = cascade.classify_tier1("asdkjfh qwoeiru zzzzz nonsense")
    assert intent == "mcp_list"
    assert source == "embedding"


def test_get_calibrated_confidence_passes_through_when_never_retrained(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    # "bots" has never been retrain_module()'d in this isolated storage —
    # no persisted calibration data exists yet.
    assert cascade.get_calibrated_confidence("bots", 0.73) == 0.73


def test_retrain_module_with_eval_persists_a_usable_calibration_map(monkeypatch, tmp_path):
    _isolate_module_storage(monkeypatch, tmp_path)
    result = cascade.retrain_module("bots", accept_if_regression_under=0.5)
    assert result["accepted"] is True

    from bot.support_bot import model_io

    data = model_io.load_model(path=module_manifest.module_path("bots"))
    assert "calibration" in data
    # calling get_calibrated_confidence must not crash and must return a
    # float in [0, 1] regardless of whether a real bin matched.
    calibrated = cascade.get_calibrated_confidence("bots", 0.5)
    assert 0.0 <= calibrated <= 1.0
