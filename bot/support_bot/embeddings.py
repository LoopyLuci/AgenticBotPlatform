"""Tier 1.5 of the Support Bot NLU cascade (next-generation modular
hybrid plan): an OPTIONAL, server-only semantic layer on top of the
classical Tier 0/1 classifiers.

Every other classifier in this subsystem is deliberately dependency-free
(model.py/nn_model.py's own docstrings explain why — pure Python, or at
most numpy, never a heavyweight ML stack). A real sentence-embedding
model breaks that invariant: `sentence-transformers` pulls in torch, a
genuinely large dependency (hundreds of MB, sometimes GB with CUDA
wheels) that has no place being a HARD requirement of a project whose
core classifiers were explicitly built to avoid exactly this. So this
module never imports `sentence_transformers` at module scope — only
lazily, inside `_load_backend()`, and only when
`config/backends.yaml`'s `support_bot.tier1_5_embeddings.enabled` is
true AND something actually calls `classify_via_embeddings()`. A
BotServer install that never enables this tier never needs the
dependency installed at all; one that does gets a clear, actionable
error (not a silent ImportError swallowed into "unknown") if it enabled
the flag without installing the extra.

This tier is SERVER-ONLY — it is never shipped to, or expected on,
Android. The on-device Tier 0 classifiers (bot/support_bot/cascade.py)
remain 100% classical, matching the plan's explicit requirement that
fully-offline capability is never compromised by this tier's existence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


class EmbeddingsUnavailableError(Exception):
    """Raised when Tier 1.5 is enabled in config but the
    `sentence-transformers` extra isn't installed — never silently
    degrades to "unknown", since that would look like a classification
    failure rather than what it actually is: a missing dependency an
    operator opted into needing."""


def _cfg() -> dict:
    from bot.config import config

    return (config.current.get("support_bot") or {}).get("tier1_5_embeddings") or {}


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", False))


_backend: Optional["_SentenceTransformerBackend"] = None


class _SentenceTransformerBackend:
    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise EmbeddingsUnavailableError(
                "support_bot.tier1_5_embeddings.enabled is true, but the "
                "'sentence-transformers' package isn't installed — run "
                "`pip install sentence-transformers` (a real ML/torch "
                "dependency, hence optional) or set enabled: false."
            ) from exc
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, convert_to_numpy=False).tolist()  # type: ignore[union-attr]


def _load_backend() -> _SentenceTransformerBackend:
    global _backend
    if _backend is None:
        _backend = _SentenceTransformerBackend(_cfg().get("model_name", "sentence-transformers/all-MiniLM-L6-v2"))
    return _backend


def reset_backend_cache() -> None:
    """Test/config-reload hook — forces the next call to reload the
    model (e.g. after model_name changes)."""
    global _backend
    _backend = None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)


@dataclass
class ExemplarIndex:
    """Precomputed embeddings for a set of (phrase, intent) exemplars —
    build once per module via build_exemplar_index(), reuse across many
    classify_via_embeddings() calls rather than re-embedding the whole
    exemplar set on every request."""
    phrases: list[str]
    intents: list[str]
    vectors: list[list[float]]


def build_exemplar_index(examples: list[tuple[str, str]]) -> ExemplarIndex:
    if not examples:
        return ExemplarIndex(phrases=[], intents=[], vectors=[])
    backend = _load_backend()
    phrases = [text for text, _ in examples]
    intents = [intent for _, intent in examples]
    vectors = backend.embed(phrases)
    return ExemplarIndex(phrases=phrases, intents=intents, vectors=vectors)


_global_index: Optional[ExemplarIndex] = None


def get_or_build_global_index(examples: list[tuple[str, str]]) -> ExemplarIndex:
    """Caches ONE exemplar index built from the full (global,
    unpartitioned) example corpus — Tier 1.5 always operates at Tier 1's
    own scope (every intent at once), never per-module, so there's only
    ever one index to maintain here. Call reset_global_index_cache()
    after training data changes, exactly like cascade.py's own
    _module_classifiers cache needs rebuild_module() after a retrain."""
    global _global_index
    if _global_index is None:
        _global_index = build_exemplar_index(examples)
    return _global_index


def reset_global_index_cache() -> None:
    global _global_index
    _global_index = None


def classify_via_embeddings(text: str, index: ExemplarIndex) -> tuple[str, float]:
    """Nearest-exemplar classification by cosine similarity — the
    embedding-space analogue of model.py's TF-IDF centroid approach, but
    against individual exemplars rather than a per-intent mean (a
    centroid in embedding space would blur together phrasings that are
    semantically close to their OWN intent but far from each other,
    which defeats the point of using real embeddings). Returns
    ("unknown", 0.0) for an empty index (never raises for a module with
    too few examples) — this tier is a bonus signal, not something that
    should ever be load-bearing enough to introduce a new failure mode
    on a thin module.

    Raises EmbeddingsUnavailableError if the backend can't be loaded
    (propagated from build_exemplar_index()/here) — callers decide
    whether to catch that or let it surface (see cascade.py's
    consumer, which treats it as "Tier 1.5 unavailable, fall through to
    the classical result unchanged")."""
    if not index.vectors:
        return "unknown", 0.0
    backend = _load_backend()
    query_vec = backend.embed([text])[0]
    best_intent: Optional[str] = None
    best_score = -1.0
    for intent, vec in zip(index.intents, index.vectors):
        score = _cosine(query_vec, vec)
        if score > best_score:
            best_intent, best_score = intent, score
    return (best_intent or "unknown"), max(best_score, 0.0)


def fuse_tier1_5(
    classical_intent: str, classical_confidence: float,
    embedding_intent: str, embedding_confidence: float,
    *, fusion: str = "boost", weight: float = 0.3,
    classical_threshold: float = 0.22,
) -> tuple[str, float, str]:
    """The fusion rule itself, extracted as a PURE function (same
    reasoning as hybrid.vote()/cascade.reduce_cross_module()) so it's
    directly testable with fixed inputs — no real embedding model
    needed to verify the fusion LOGIC is correct. Returns
    (intent, confidence, source) where source is "classical",
    "embedding", or "fused".

    "boost": the classical result wins outright whenever it's already
    confident (>= classical_threshold) — Tier 1.5 only ever gets to
    supply an answer when classical had none, matching "an optional
    layer that can be turned on later," never a vote that could
    override an already-confident classical decision.

    "weighted": a straight confidence blend regardless of either
    confidence, weighted by `weight` toward the embedding result — only
    meaningful when the two agree on intent; on disagreement, whichever
    has the higher WEIGHTED score wins."""
    if fusion == "boost":
        if classical_confidence >= classical_threshold and classical_intent != "unknown":
            return classical_intent, classical_confidence, "classical"
        if embedding_intent != "unknown":
            return embedding_intent, embedding_confidence, "embedding"
        return classical_intent, classical_confidence, "classical"

    if fusion == "weighted":
        if classical_intent == "unknown" and embedding_intent == "unknown":
            return "unknown", 0.0, "classical"
        if classical_intent == embedding_intent:
            blended = (1 - weight) * classical_confidence + weight * embedding_confidence
            return classical_intent, blended, "fused"
        classical_weighted = (1 - weight) * classical_confidence
        embedding_weighted = weight * embedding_confidence
        if classical_weighted >= embedding_weighted:
            return classical_intent, classical_confidence, "classical"
        return embedding_intent, embedding_confidence, "embedding"

    raise ValueError(f"unknown fusion mode: {fusion!r}")
