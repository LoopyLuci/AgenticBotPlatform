"""Tier 0 of the Support Bot NLU cascade (see the "next-generation modular
hybrid" plan): one classical TfidfCentroidModel+NeuralIntentClassifier
pair PER enabled Knowledge Module, combined by a thin cross-module
reduction sitting above hybrid.py's own, completely unmodified,
`vote()` function.

Within a single module, classification is byte-for-byte hybrid.py's
existing decision rule — `vote()` is reused, not reimplemented. What's
new here is only the layer *above* that: given N modules each
independently voting, pick the single best answer across all of them.
This is the Python reference implementation Android's HybridClassifier.kt
mirrors (see that file's own module-level cross-module reduction).

Module classifier pairs are lazily built and cached (same "train once,
reuse" lifecycle as hybrid.py's own module-level singletons), with any
previously-persisted state (data/support_bot_models/modules/<id>/current.json)
loaded on top of the fresh training, same as hybrid.py's own
_load_persisted_state_if_present(). Call retrain_module()/
rebuild_all_modules() after training data changes for a module, exactly
like hybrid.retrain_all() does for the single global pair today —
including the same optional accept_if_regression_under eval gate, scoped
per module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bot.support_bot import calibration, embeddings, hybrid, knowledge_modules, model_io, module_manifest
from bot.support_bot.model import TfidfCentroidModel
from bot.support_bot.nn_model import NeuralIntentClassifier

# A from-scratch MLP trained on a Knowledge Module's own (small) example
# subset can be badly overconfident on out-of-distribution input — fewer
# classes competing in the softmax, and few enough examples that 800
# epochs of full-batch gradient descent (nn_model.py's own training
# regime) can essentially memorize the training set. Confirmed live in
# this module's own test suite: a 4-intent, ~12-example module classified
# pure gibberish as a real intent at >0.5 confidence. Below this many
# total examples, the module's NN sub-model is replaced with a stub that
# always defers ("unknown", 0.0) — hybrid.vote() already handles one
# sub-model always deferring gracefully, so the module's TF-IDF
# sub-model (robust at any size, per model.py's own docstring) carries
# it alone until Phase 3's confidence calibration replaces this coarse
# floor with something better-justified.
_MIN_EXAMPLES_FOR_MODULE_NN = 20


class _AlwaysUnknownClassifier:
    """Stand-in for NeuralIntentClassifier when a module has too few
    examples to train one safely — see _MIN_EXAMPLES_FOR_MODULE_NN."""

    def predict(self, text: str) -> tuple[str, float]:
        return "unknown", 0.0


@dataclass
class ModuleClassifierPair:
    tfidf: TfidfCentroidModel
    nn: Any  # NeuralIntentClassifier, or _AlwaysUnknownClassifier for tiny modules


# module_id -> its lazily-built classifier pair. Never reset implicitly —
# only rebuild_module()/rebuild_all_modules() replace an entry, mirroring
# hybrid.py's singletons never silently retraining themselves mid-request.
_module_classifiers: dict[str, ModuleClassifierPair] = {}


def _examples_for_module(
    module_id: str, examples: Optional[list[tuple[str, str]]] = None,
) -> list[tuple[str, str]]:
    intents = set(knowledge_modules.intents_for_module(module_id))
    if not intents:
        return []
    source = examples if examples is not None else hybrid._gather_examples()
    return [(text, intent) for text, intent in source if intent in intents]


def build_module_classifiers(
    module_id: str, *, examples: Optional[list[tuple[str, str]]] = None,
) -> ModuleClassifierPair:
    """Trains (from scratch, never from persisted state — see
    get_module_classifiers() for that path) and caches module_id's
    classifier pair. Pass `examples` (the full gathered corpus) when
    rebuilding several modules in a row, so each one doesn't re-run
    hybrid._gather_examples() (a DB query) on its own."""
    module_examples = _examples_for_module(module_id, examples)
    nn: Any = (
        NeuralIntentClassifier(module_examples)
        if len(module_examples) >= _MIN_EXAMPLES_FOR_MODULE_NN
        else _AlwaysUnknownClassifier()
    )
    pair = ModuleClassifierPair(tfidf=TfidfCentroidModel(module_examples), nn=nn)
    _module_classifiers[module_id] = pair
    return pair


def _load_persisted_module_state(module_id: str, pair: ModuleClassifierPair) -> None:
    """Mirrors hybrid.py's _load_persisted_state_if_present(): if a
    previous retrain_module() call persisted this module's state, load it
    in place over the fresh training build_module_classifiers() just did
    — picks up the last ACCEPTED retrain instead of silently reverting to
    training_data.py on every process restart. A missing/malformed file,
    or a tiny module whose NN is the always-defers stub (no load_state()
    method), is never an error — the freshly-trained state stays."""
    data = model_io.load_model(path=module_manifest.module_path(module_id))
    if data is None:
        return
    try:
        pair.tfidf.load_state(data["tfidf"])
        if hasattr(pair.nn, "load_state"):
            pair.nn.load_state(data["nn"])
    except (KeyError, TypeError):
        pass


def get_module_classifiers(module_id: str) -> ModuleClassifierPair:
    """Lazily builds a module's classifier pair on first use, then
    overlays any persisted state from a previous retrain_module() call."""
    pair = _module_classifiers.get(module_id)
    if pair is None:
        pair = build_module_classifiers(module_id)
        _load_persisted_module_state(module_id, pair)
    return pair


def retrain_module(module_id: str, accept_if_regression_under: Optional[float] = None) -> dict[str, Any]:
    """The module-scoped equivalent of hybrid.retrain_all() — same
    contract, same regression-gate semantics, just scoped to one module's
    own examples and its own persisted file
    (module_manifest.module_path(module_id)) instead of the single global
    current.json. See hybrid.retrain_all()'s own docstring for the full
    explanation of accept_if_regression_under's two modes; this is a
    line-for-line port of that logic onto a per-module classifier pair."""
    if knowledge_modules.MODULE_REGISTRY.get(module_id) is None:
        raise ValueError(f"unknown module: {module_id!r}")

    module_examples = _examples_for_module(module_id)
    path = module_manifest.module_path(module_id)

    if accept_if_regression_under is None:
        pair = build_module_classifiers(module_id)
        _save_module_state(module_id, pair, module_examples, path)
        return {"accepted": True, "n_examples": len(module_examples), "eval": None}

    from bot.support_bot import eval as eval_mod

    previous = model_io.load_model(path=path)
    previous_accuracy = (previous or {}).get("eval", {}).get("holdout_accuracy")

    train, holdout = eval_mod.stratified_split(module_examples)
    result = eval_mod.evaluate(train, holdout)
    new_accuracy = result["holdout_accuracy"]

    if (
        previous_accuracy is not None
        and new_accuracy is not None
        and new_accuracy < previous_accuracy - accept_if_regression_under
    ):
        return {
            "accepted": False,
            "reason": (
                f"module {module_id!r} holdout accuracy {new_accuracy:.3f} regressed more than "
                f"{accept_if_regression_under:.3f} below the active module's {previous_accuracy:.3f}"
            ),
            "eval": result,
        }

    # Confidence calibration is fit on the SAME holdout split eval used —
    # never on training data, for the same "don't grade your own homework"
    # reason evaluate() itself never touches the live singletons. A module
    # with no holdout (too few examples per intent, see
    # eval.stratified_split's own min-examples floor) simply gets no
    # calibration data — calibration.apply_calibration() already handles
    # an empty map as a no-op passthrough.
    calibration_pairs = eval_mod.collect_confidence_correctness(train, holdout)
    calibration_map = calibration.fit_isotonic(calibration_pairs)

    pair = build_module_classifiers(module_id)
    _save_module_state(
        module_id, pair, module_examples, path,
        eval_result=result, calibration_map=calibration_map,
    )
    return {"accepted": True, "n_examples": len(module_examples), "eval": result}


def _save_module_state(
    module_id: str, pair: ModuleClassifierPair, examples: list[tuple[str, str]], path: Path,
    *, eval_result: Optional[dict[str, Any]] = None,
    calibration_map: Optional[calibration.CalibrationMap] = None,
) -> None:
    intents = sorted({intent for _, intent in examples})
    training_hash = model_io.compute_training_hash(examples)
    # A tiny module's NN sub-model is the always-defers stub, which has no
    # export_state() — persist an empty nn block rather than crashing;
    # _load_persisted_module_state()'s hasattr(load_state) guard means a
    # future rebuild past the NN-training threshold simply retrains fresh
    # instead of trying to load this placeholder back.
    nn_state = pair.nn.export_state() if hasattr(pair.nn, "export_state") else {}
    model_io.save_model(
        pair.tfidf.export_state(), nn_state, intents,
        training_hash=training_hash, eval_result=eval_result,
        calibration=calibration_map.to_dict() if calibration_map is not None else None,
        path=path,
    )
    module_manifest.set_version(module_id, training_hash)


def get_calibrated_confidence(module_id: str, raw_confidence: float) -> float:
    """Applies module_id's persisted calibration map (from its last
    retrain_module(accept_if_regression_under=...) call) to a raw
    confidence score — falls back to the raw score unchanged if the
    module has never been retrained-with-eval yet (no calibration data),
    exactly like calibration.apply_calibration()'s own empty-map
    contract."""
    data = model_io.load_model(path=module_manifest.module_path(module_id))
    calib = calibration.CalibrationMap.from_dict((data or {}).get("calibration") or {})
    return calibration.apply_calibration(calib, raw_confidence)


def rebuild_all_modules(accept_if_regression_under: Optional[float] = None) -> dict[str, dict[str, Any]]:
    """Retrains and persists every registered module — the module-scoped
    equivalent of hybrid.retrain_all(), called once per module. Returns
    each module's retrain_module() result, so a caller (the dashboard's
    eventual "retrain everything" action) can show per-module before/after
    numbers rather than one opaque global result."""
    return {
        module_id: retrain_module(module_id, accept_if_regression_under)
        for module_id in knowledge_modules.all_module_ids()
    }


def classify_tier1(text: str) -> tuple[str, float, str]:
    """Tier 1 of the cascade: the single global, unpartitioned
    hybrid.classify() (unchanged, always-freshest-trained), optionally
    boosted by Tier 1.5's embedding layer when
    config/backends.yaml's support_bot.tier1_5_embeddings.enabled is
    true. Off by default, this is a pure passthrough to hybrid.classify()
    — Tier 1.5 never runs at all unless explicitly enabled, and even
    then only ever supplies an answer when the classical result wasn't
    already confident (see embeddings.fuse_tier1_5()'s "boost" mode).
    Returns (intent, confidence, source); source is "ensemble"/"tfidf"/
    "nn"/"unknown" from hybrid.classify() alone, or "embedding"/"fused"
    when Tier 1.5 actually contributed the answer."""
    result = hybrid.classify(text)
    if not embeddings.is_enabled():
        return result.intent, result.confidence, result.source

    try:
        module_examples = hybrid._gather_examples()
        index = embeddings.get_or_build_global_index(module_examples)
        embedding_intent, embedding_confidence = embeddings.classify_via_embeddings(text, index)
    except embeddings.EmbeddingsUnavailableError:
        # Enabled in config but the extra isn't installed — degrade to
        # the classical result rather than erroring the whole request;
        # an operator who flips this flag without installing the
        # dependency gets a classifier that just behaves as if Tier 1.5
        # were off, not a broken endpoint.
        return result.intent, result.confidence, result.source

    cfg = embeddings._cfg()
    return embeddings.fuse_tier1_5(
        result.intent, result.confidence, embedding_intent, embedding_confidence,
        fusion=cfg.get("fusion", "boost"), weight=cfg.get("weight", 0.3),
    )


async def classify_full_cascade(text: str) -> tuple[str, float, str]:
    """The complete server-side cascade: Tier 1 (+ Tier 1.5 if enabled)
    via classify_tier1() above, falling through to Tier 2 (a real LLM
    call, bot/support_bot/llm_fallback.py) only when that still comes
    back "unknown" — keeping the (real, if free-tier) cost and latency
    of an LLM call off the hot path for every message the classical
    tiers already resolve. A confident Tier 2 result is fed back into
    the pending-review queue by llm_fallback.classify_and_record()
    itself; this function just decides WHETHER to reach for it and
    returns whatever the deepest tier that answered produced."""
    intent, confidence, source = classify_tier1(text)
    if intent != "unknown":
        return intent, confidence, source

    from bot.support_bot import knowledge_modules, llm_fallback

    tier2_result = await llm_fallback.classify_and_record(text, sorted(knowledge_modules.all_intents()))
    if tier2_result is None:
        return intent, confidence, source  # Tier 2 unavailable — the Tier 1 "unknown" stands
    tier2_intent, tier2_confidence = tier2_result
    if tier2_intent == "unknown":
        return "unknown", tier2_confidence, "unknown"
    return tier2_intent, tier2_confidence, "llm_fallback"


@dataclass
class CascadeResult:
    intent: str
    confidence: float
    module_id: Optional[str]
    source: str  # "ensemble" | "tfidf" | "nn" | "unknown"
    # module_id -> (intent, confidence, source) — every enabled module's
    # own independent vote, for observability/debugging, not just the
    # single winner.
    per_module: dict[str, tuple[str, float, str]] = field(default_factory=dict)


def reduce_cross_module(per_module: dict[str, tuple[str, float, str]]) -> tuple[str, float, str, Optional[str]]:
    """The cross-module reduction itself, extracted as a PURE function —
    exactly the same reason hybrid.py extracts `vote()`: this is the
    single implementation both classify_tier0() below and Android's
    CascadeClassifier.kt (a hand-written Kotlin port) must agree on, and
    a pure function over plain (intent, confidence, source) tuples is
    testable with fixed inputs, no trained classifier or model file
    needed on either side.

    Given every enabled module's own independent hybrid.vote() result,
    picks the single highest-confidence non-"unknown" one; a tie prefers
    whichever result came from the tfidf sub-model (generalizing
    hybrid.vote()'s own "ties favor tfidf" rule to the cross-module
    case). Returns (intent, confidence, source, module_id) —
    module_id is None only when every module said "unknown"."""
    best: Optional[tuple[str, float, str, str]] = None  # (intent, confidence, source, module_id)
    for module_id, (intent, confidence, source) in per_module.items():
        if intent == "unknown":
            continue
        is_better = (
            best is None
            or confidence > best[1]
            or (confidence == best[1] and source == "tfidf" and best[2] != "tfidf")
        )
        if is_better:
            best = (intent, confidence, source, module_id)

    if best is None:
        return "unknown", 0.0, "unknown", None
    intent, confidence, source, module_id = best
    return intent, confidence, source, module_id


def classify_tier0(text: str, *, enabled_modules: Optional[list[str]] = None) -> CascadeResult:
    """Runs hybrid.vote() independently within every currently-enabled
    Knowledge Module (or `enabled_modules`, for testing/explicit scoping),
    then hands every module's result to reduce_cross_module() to pick the
    single final answer."""
    modules = enabled_modules if enabled_modules is not None else module_manifest.enabled_module_ids()
    per_module: dict[str, tuple[str, float, str]] = {}

    for module_id in modules:
        pair = get_module_classifiers(module_id)
        tfidf_intent, tfidf_confidence = pair.tfidf.predict(text)
        nn_intent, nn_confidence = pair.nn.predict(text)
        intent, confidence, source, _agreed = hybrid.vote(tfidf_intent, tfidf_confidence, nn_intent, nn_confidence)
        per_module[module_id] = (intent, confidence, source)

    intent, confidence, source, module_id = reduce_cross_module(per_module)
    return CascadeResult(intent=intent, confidence=confidence, module_id=module_id, source=source, per_module=per_module)
