"""Held-out evaluation for the Support Bot's hybrid classifier — Phase 2
of the "Support Bot NLU upgrade" plan. Measures real accuracy on data
neither sub-model trained on, rather than trusting training-set fit
(which both sub-models can trivially memorize at this corpus size).

An intent with too few examples can't meaningfully hold out a fraction
of them (holding out 20% of 4 examples is 0-1 items — not a real
measurement) — those intents train on 100% of their data and are simply
excluded from the holdout set, not from training.
"""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from bot.support_bot import hybrid
from bot.support_bot.model import TfidfCentroidModel
from bot.support_bot.nn_model import NeuralIntentClassifier

DEFAULT_MIN_EXAMPLES_FOR_HOLDOUT = 6
DEFAULT_HOLDOUT_FRAC = 0.2


def stratified_split(
    examples: list[tuple[str, str]], *,
    min_examples_for_holdout: int = DEFAULT_MIN_EXAMPLES_FOR_HOLDOUT,
    holdout_frac: float = DEFAULT_HOLDOUT_FRAC,
    seed: int = 42,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Splits `examples` into (train, holdout), stratified by intent — each
    intent contributes its own holdout_frac share, not a global random
    split (which could leave a whole intent with zero holdout examples,
    or zero training examples, purely by chance). An intent with fewer
    than `min_examples_for_holdout` examples is excluded from holdout
    entirely — ALL of its examples go to train."""
    by_intent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for example in examples:
        by_intent[example[1]].append(example)

    rng = random.Random(seed)
    train: list[tuple[str, str]] = []
    holdout: list[tuple[str, str]] = []
    for intent, items in by_intent.items():
        if len(items) < min_examples_for_holdout:
            train.extend(items)
            continue
        shuffled = list(items)
        rng.shuffle(shuffled)
        n_holdout = max(1, round(len(shuffled) * holdout_frac))
        holdout.extend(shuffled[:n_holdout])
        train.extend(shuffled[n_holdout:])
    return train, holdout


def evaluate(train: list[tuple[str, str]], holdout: list[tuple[str, str]]) -> dict[str, Any]:
    """Trains a CANDIDATE tfidf+nn pair on `train` only (fresh instances —
    never touches the live hybrid.py singletons) and scores them against
    `holdout` using the exact same hybrid.vote() logic production
    classification uses. Returns {accuracy, per_intent: {intent:
    {precision, recall, support}}, n_train, n_holdout}."""
    if not holdout:
        return {"holdout_accuracy": None, "per_intent": {}, "n_train": len(train), "n_holdout": 0}

    candidate_tfidf = TfidfCentroidModel(train)
    candidate_nn = NeuralIntentClassifier(train)

    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    support: dict[str, int] = defaultdict(int)
    correct = 0

    for text, true_intent in holdout:
        tfidf_intent, tfidf_confidence = candidate_tfidf.predict(text)
        nn_intent, nn_confidence = candidate_nn.predict(text)
        predicted_intent, _confidence, _source, _agreed = hybrid.vote(
            tfidf_intent, tfidf_confidence, nn_intent, nn_confidence
        )
        support[true_intent] += 1
        if predicted_intent == true_intent:
            correct += 1
            tp[true_intent] += 1
        else:
            fn[true_intent] += 1
            fp[predicted_intent] += 1

    per_intent: dict[str, dict[str, Any]] = {}
    for intent in support:
        precision_denom = tp[intent] + fp[intent]
        recall_denom = tp[intent] + fn[intent]
        per_intent[intent] = {
            "precision": (tp[intent] / precision_denom) if precision_denom else 0.0,
            "recall": (tp[intent] / recall_denom) if recall_denom else 0.0,
            "support": support[intent],
        }

    return {
        "holdout_accuracy": correct / len(holdout),
        "per_intent": per_intent,
        "n_train": len(train),
        "n_holdout": len(holdout),
    }
