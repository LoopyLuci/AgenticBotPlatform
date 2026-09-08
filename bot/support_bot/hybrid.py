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
from typing import Any, Callable, Optional

from bot import db
from bot.support_bot import model as tfidf_model
from bot.support_bot import model_io
from bot.support_bot import nn_model as neural_model

# The pluggable set of sub-models the hybrid votes across. Each entry's
# predict_fn must return (intent, confidence) with intent == "unknown"
# when not confident — see model.py / nn_model.py's own predict()
# docstrings for the exact contract every classifier here must honor.
CLASSIFIERS: list[tuple[str, Callable[[str], tuple[str, float]]]] = [
    ("tfidf", tfidf_model.model.predict),
    ("nn", neural_model.nn_model.predict),
]


def _load_persisted_state_if_present() -> None:
    """Both sub-models already train fresh at their own module import
    time (today's behavior, unchanged as the fallback). If a previously
    saved model.json exists, load it in place over that fresh training —
    picks up whatever the last accepted retrain actually was, instead of
    silently reverting to the static training_data.py baseline on every
    process restart."""
    # Explicit module-attribute lookup at call time, not a bare
    # load_model() call — model_io.load_model's own path= default is
    # bound at function-definition time, so a test's monkeypatch of
    # model_io.CURRENT_PATH would silently not apply if this relied on
    # that default instead.
    data = model_io.load_model(path=model_io.CURRENT_PATH)
    if data is None:
        return
    try:
        tfidf_model.model.load_state(data["tfidf"])
        neural_model.nn_model.load_state(data["nn"])
    except (KeyError, TypeError):
        # A malformed/older-shape file must never break Support Bot
        # startup — the freshly-trained-at-import state (already built
        # before this function runs) stays in place untouched.
        pass


_load_persisted_state_if_present()


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


def vote(tfidf_intent: str, tfidf_confidence: float, nn_intent: str, nn_confidence: float) -> tuple[str, float, str, bool]:
    """The hybrid decision rule (see this module's own docstring),
    extracted as a pure function so bot/support_bot/eval.py can score a
    CANDIDATE pair of classifiers (trained on a train-only split, never
    touching the live singletons) with the exact same logic classify()
    uses in production — one implementation, never two that could drift.
    Returns (intent, confidence, source, agreed)."""
    agreed = tfidf_intent == nn_intent and tfidf_intent != "unknown"
    if agreed:
        return tfidf_intent, max(tfidf_confidence, nn_confidence), "ensemble", True
    if tfidf_intent != "unknown" and nn_intent != "unknown":
        # Both confident, but disagree — trust the more confident one;
        # ties favor TF-IDF since it's the explainable, auditable model.
        if tfidf_confidence >= nn_confidence:
            return tfidf_intent, tfidf_confidence, "tfidf", False
        return nn_intent, nn_confidence, "nn", False
    if tfidf_intent != "unknown":
        return tfidf_intent, tfidf_confidence, "tfidf", False
    if nn_intent != "unknown":
        return nn_intent, nn_confidence, "nn", False
    return "unknown", max(tfidf_confidence, nn_confidence), "unknown", False


def classify(text: str, *, log: bool = True) -> HybridResult:
    votes = {name: fn(text) for name, fn in CLASSIFIERS}
    tfidf_intent, tfidf_confidence = votes["tfidf"]
    nn_intent, nn_confidence = votes["nn"]

    intent, confidence, source, agreed = vote(tfidf_intent, tfidf_confidence, nn_intent, nn_confidence)

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


def _gather_examples() -> list[tuple[str, str]]:
    from bot.support_bot.training_data import EXAMPLES

    examples = list(EXAMPLES)
    try:
        examples += [(row["phrase"], row["intent"]) for row in db.list_support_bot_phrases()]
    except Exception:
        pass
    return examples


def retrain_all(accept_if_regression_under: Optional[float] = None) -> dict[str, Any]:
    """Retrains every sub-model on the current baseline + Training-tab
    phrases, then persists the result to model_io's current.json so a
    future process restart picks up this retrain instead of reverting to
    the static training_data.py baseline. Called after every add/delete
    in the Training tab.

    `accept_if_regression_under=None` (the default): no eval/regression
    check — always retrains on 100% of the data and saves, matching the
    original add/delete-a-phrase behavior exactly (kept as the default so
    every existing call site is unaffected by this parameter's addition).

    `accept_if_regression_under=<tolerance>`: evaluates a candidate model
    (trained on a held-out train/holdout split, see bot/support_bot/eval.py)
    against the holdout set BEFORE touching the live singletons. If a
    previous model.json exists with its own recorded holdout_accuracy and
    the candidate's accuracy is more than `tolerance` below it, the retrain
    is REJECTED — the live singletons and current.json are left completely
    untouched (reversible by construction: nothing was changed to begin
    with). Otherwise, retrains the live singletons on the FULL example
    set (train+holdout combined — holding data back forever after the
    eval gate has already used it would waste it) and saves, this time
    with the new eval result attached. Always returns the eval result and
    an `accepted` flag so a caller (the dashboard's "Retrain & Evaluate"
    action) can show before/after numbers either way."""
    examples = _gather_examples()

    if accept_if_regression_under is None:
        n_tfidf = tfidf_model.retrain()
        n_nn = neural_model.retrain()
        _save_current_state(examples)
        return {"accepted": True, "tfidf": n_tfidf, "nn": n_nn, "eval": None}

    from bot.support_bot import eval as eval_mod

    previous = model_io.load_model(path=model_io.CURRENT_PATH)
    previous_accuracy = (previous or {}).get("eval", {}).get("holdout_accuracy")

    train, holdout = eval_mod.stratified_split(examples)
    result = eval_mod.evaluate(train, holdout)
    new_accuracy = result["holdout_accuracy"]

    if (
        previous_accuracy is not None
        and new_accuracy is not None
        and new_accuracy < previous_accuracy - accept_if_regression_under
    ):
        return {
            "accepted": False,
            "reason": f"holdout accuracy {new_accuracy:.3f} regressed more than {accept_if_regression_under:.3f} below the active model's {previous_accuracy:.3f}",
            "eval": result,
        }

    n_tfidf = tfidf_model.retrain()
    n_nn = neural_model.retrain()
    _save_current_state(examples, eval_result=result)
    return {"accepted": True, "tfidf": n_tfidf, "nn": n_nn, "eval": result}


def _save_current_state(examples: list[tuple[str, str]], *, eval_result: Optional[dict[str, Any]] = None) -> None:
    intents = sorted({intent for _, intent in examples})
    # Explicit path=model_io.CURRENT_PATH for the same reason noted in
    # _load_persisted_state_if_present() above — never rely on
    # save_model's own default parameter value here.
    model_io.save_model(
        tfidf_model.model.export_state(),
        neural_model.nn_model.export_state(),
        intents,
        training_hash=model_io.compute_training_hash(examples),
        eval_result=eval_result,
        path=model_io.CURRENT_PATH,
    )


def export_current_model() -> dict[str, Any]:
    """Whatever the live singletons currently classify with, in the same
    portable JSON shape model_io.py persists — served over
    GET /api/support-bot/model (Phase 6 of the Support Bot NLU upgrade
    plan) so the Android app's Kotlin engine always has something valid
    to load, even before any retrain-with-eval has ever run and saved a
    current.json to disk."""
    persisted = model_io.load_model(path=model_io.CURRENT_PATH)
    if persisted is not None:
        return persisted
    examples = _gather_examples()
    return {
        "format_version": model_io.FORMAT_VERSION,
        "training_data_hash": model_io.compute_training_hash(examples),
        "intents": sorted({intent for _, intent in examples}),
        "tfidf": tfidf_model.model.export_state(),
        "nn": neural_model.nn_model.export_state(),
        "eval": {},
    }


def health() -> dict[str, Any]:
    """Self-monitoring summary — see bot/db.py's
    get_support_bot_classification_stats() for exactly what's computed."""
    return db.get_support_bot_classification_stats()
