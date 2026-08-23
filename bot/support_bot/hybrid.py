"""The hybrid classifier: deterministic TF-IDF centroid model +
real trained neural network, combined into one production-facing
decision, with a self-monitoring log of every call.

## Why hybrid, not "pick one"

The two sub-models fail differently. The TF-IDF centroid model
(model.py) is fast, fully explainable, and robust to a training set this
small — but it's a nearest-centroid lookup, so it can be fooled by a
phrase that shares vocabulary with the wrong intent. The neural network
(nn_model.py) learns non-linear feature interactions the centroid model
can't represent — but it's a black box, and on a training set this size
it can occasionally be overconfident about the wrong answer. Running both
and using *agreement* as the primary confidence signal is the actual
"hybrid" here: when two differently-biased models agree, that's much
stronger evidence than either alone.

## The decision rule (`classify`)

1. Both agree on a non-"unknown" intent → **ensemble**, confidence is the
   higher of the two (agreement already de-risked the lower one).
2. Both are confident but disagree → trust whichever sub-model is more
   confident (tie-breaks toward TF-IDF, the more explainable one).
3. Only one is confident → use that one.
4. Neither is confident → "unknown", exactly like today.

## Modularity / scalability

`CLASSIFIERS` is a plain list of `(name, predict_fn)` pairs. Adding a
third sub-model (a future embedding-based classifier, a real LLM-backed
classifier, whatever) means appending one entry here and extending the
voting rule below — nothing about engine.py or the dashboard needs to
change, since they only ever see a `HybridResult`.

## Self-monitoring

Every call logs both sub-models' raw verdicts plus the final decision to
`support_bot_classifications` (bot/db.py) — `health()` turns that log into
the Training tab's model-health numbers (agreement rate, unknown rate,
average confidence per model). This is what "self-monitoring" means here:
observable, queryable behavior over real traffic, not a static claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bot import db
from bot.support_bot import model as tfidf_model
from bot.support_bot import nn_model as neural_model

# The pluggable set of sub-models the hybrid votes across. Each entry's
# predict_fn must return (intent, confidence) with intent == "unknown"
# when not confident — see model.py / nn_model.py's own predict()
# docstrings for the exact contract every classifier here must honor.
CLASSIFIERS: list[tuple[str, Callable[[str], tuple[str, float]]]] = [
    ("tfidf", tfidf_model.model.predict),
    ("nn", neural_model.nn_model.predict),
]


@dataclass
class HybridResult:
    intent: str
    confidence: float
    tfidf_intent: str
    tfidf_confidence: float
    nn_intent: str
    nn_confidence: float
    agreed: bool
    source: str  # "ensemble" | "tfidf" | "nn" | "unknown"


def classify(text: str, *, log: bool = True) -> HybridResult:
    votes = {name: fn(text) for name, fn in CLASSIFIERS}
    tfidf_intent, tfidf_confidence = votes["tfidf"]
    nn_intent, nn_confidence = votes["nn"]

    agreed = tfidf_intent == nn_intent and tfidf_intent != "unknown"
    if agreed:
        intent, confidence, source = tfidf_intent, max(tfidf_confidence, nn_confidence), "ensemble"
    elif tfidf_intent != "unknown" and nn_intent != "unknown":
        # Both confident, but disagree — trust the more confident one;
        # ties favor TF-IDF since it's the explainable, auditable model.
        if tfidf_confidence >= nn_confidence:
            intent, confidence, source = tfidf_intent, tfidf_confidence, "tfidf"
        else:
            intent, confidence, source = nn_intent, nn_confidence, "nn"
    elif tfidf_intent != "unknown":
        intent, confidence, source = tfidf_intent, tfidf_confidence, "tfidf"
    elif nn_intent != "unknown":
        intent, confidence, source = nn_intent, nn_confidence, "nn"
    else:
        intent, confidence, source = "unknown", max(tfidf_confidence, nn_confidence), "unknown"

    if log:
        try:
            db.log_support_bot_classification(
                text=text,
                tfidf_intent=tfidf_intent, tfidf_confidence=tfidf_confidence,
                nn_intent=nn_intent, nn_confidence=nn_confidence,
                final_intent=intent, final_confidence=confidence,
                source=source, agreed=agreed,
            )
        except Exception:
            # Self-monitoring must never be able to break the classifier
            # it's monitoring — a logging failure degrades observability,
            # not the actual reply the user gets.
            pass

    return HybridResult(
        intent=intent, confidence=confidence,
        tfidf_intent=tfidf_intent, tfidf_confidence=tfidf_confidence,
        nn_intent=nn_intent, nn_confidence=nn_confidence,
        agreed=agreed, source=source,
    )


def retrain_all() -> dict[str, int]:
    """Retrains every sub-model on the current baseline + Training-tab
    phrases. Called after every add/delete in the Training tab."""
    return {
        "tfidf": tfidf_model.retrain(),
        "nn": neural_model.retrain(),
    }


def health() -> dict[str, Any]:
    """Self-monitoring summary — see bot/db.py's
    get_support_bot_classification_stats() for exactly what's computed."""
    return db.get_support_bot_classification_stats()
