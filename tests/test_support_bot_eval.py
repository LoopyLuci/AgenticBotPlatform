"""bot/support_bot/eval.py — held-out evaluation harness (Phase 2 of the
"Support Bot NLU upgrade" plan) — and hybrid.py's retrain_all()
regression gate built on top of it.
"""
from __future__ import annotations

from bot.support_bot import eval as eval_mod
from bot.support_bot import hybrid, model_io
from bot.support_bot.training_data import EXAMPLES


def test_low_count_intents_never_appear_in_holdout():
    examples = [
        ("a1", "rare"), ("a2", "rare"), ("a3", "rare"),  # only 3 — below the default min of 6
        ("b1", "common"), ("b2", "common"), ("b3", "common"),
        ("b4", "common"), ("b5", "common"), ("b6", "common"), ("b7", "common"), ("b8", "common"),
    ]

    train, holdout = eval_mod.stratified_split(examples, min_examples_for_holdout=6)

    holdout_intents = {intent for _, intent in holdout}
    assert "rare" not in holdout_intents
    # All 3 "rare" examples must still be present, just in train.
    assert sum(1 for _, intent in train if intent == "rare") == 3


def test_split_is_stratified_per_intent():
    examples = [(f"phrase-a-{i}", "intent_a") for i in range(10)] + [(f"phrase-b-{i}", "intent_b") for i in range(10)]

    train, holdout = eval_mod.stratified_split(examples, min_examples_for_holdout=6, holdout_frac=0.2)

    assert sum(1 for _, i in holdout if i == "intent_a") == 2
    assert sum(1 for _, i in holdout if i == "intent_b") == 2
    assert len(train) + len(holdout) == len(examples)


def test_split_is_deterministic_given_a_seed():
    examples = [(f"phrase-{i}", "intent_a") for i in range(20)]
    train1, holdout1 = eval_mod.stratified_split(examples, seed=1)
    train2, holdout2 = eval_mod.stratified_split(examples, seed=1)
    assert holdout1 == holdout2
    assert train1 == train2


def test_evaluate_on_real_training_data_returns_sane_accuracy():
    train, holdout = eval_mod.stratified_split(list(EXAMPLES))
    result = eval_mod.evaluate(train, holdout)

    assert result["n_holdout"] == len(holdout)
    assert result["n_train"] == len(train)
    assert 0.0 <= result["holdout_accuracy"] <= 1.0
    assert set(result["per_intent"]) <= {intent for _, intent in holdout}


def test_evaluate_with_no_holdout_returns_none_accuracy():
    result = eval_mod.evaluate([("x", "intent_a")], [])
    assert result["holdout_accuracy"] is None
    assert result["n_holdout"] == 0


def test_retrain_all_without_gate_behaves_as_before(tmp_path, monkeypatch):
    scratch = tmp_path / "current.json"
    monkeypatch.setattr(model_io, "CURRENT_PATH", scratch)

    result = hybrid.retrain_all()

    assert result["accepted"] is True
    assert result["eval"] is None
    assert scratch.exists()


def test_retrain_all_with_gate_accepts_when_no_previous_model(tmp_path, monkeypatch):
    scratch = tmp_path / "current.json"
    monkeypatch.setattr(model_io, "CURRENT_PATH", scratch)

    result = hybrid.retrain_all(accept_if_regression_under=0.05)

    assert result["accepted"] is True
    assert result["eval"]["holdout_accuracy"] is not None
    saved = model_io.load_model(path=scratch)
    assert saved["eval"]["holdout_accuracy"] == result["eval"]["holdout_accuracy"]


def test_retrain_all_with_gate_rejects_a_real_regression(tmp_path, monkeypatch):
    scratch = tmp_path / "current.json"
    monkeypatch.setattr(model_io, "CURRENT_PATH", scratch)

    # Seed a "previous" model claiming perfect accuracy, impossible for a
    # real retrain to match or beat within tolerance — forces rejection.
    model_io.save_model(
        hybrid.tfidf_model.model.export_state(), hybrid.neural_model.nn_model.export_state(),
        sorted({i for _, i in EXAMPLES}), training_hash="sha256:seed",
        eval_result={"holdout_accuracy": 1.0}, path=scratch,
    )

    result = hybrid.retrain_all(accept_if_regression_under=0.001)

    assert result["accepted"] is False
    assert "regressed" in result["reason"]
    # The seeded file must be untouched — still the perfect-accuracy stub,
    # not overwritten by the rejected candidate's real eval result.
    saved = model_io.load_model(path=scratch)
    assert saved["eval"]["holdout_accuracy"] == 1.0
    assert saved["training_data_hash"] == "sha256:seed"
