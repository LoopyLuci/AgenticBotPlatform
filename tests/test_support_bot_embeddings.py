"""bot/support_bot/embeddings.py — Tier 1.5 of the Support Bot NLU
cascade. Fusion logic is tested with fixed inputs (no real embedding
model needed); the actual sentence-transformers backend is exercised
only by confirming it degrades correctly when the dependency isn't
installed, never by requiring it to actually be present in this test
environment.
"""
from __future__ import annotations

import pytest

from bot.support_bot import embeddings


def test_is_enabled_defaults_to_false(monkeypatch):
    monkeypatch.setattr(embeddings, "_cfg", lambda: {})
    assert embeddings.is_enabled() is False


def test_is_enabled_reads_config(monkeypatch):
    monkeypatch.setattr(embeddings, "_cfg", lambda: {"enabled": True})
    assert embeddings.is_enabled() is True


def test_build_exemplar_index_on_empty_examples_never_loads_a_backend():
    # Must not attempt to import sentence_transformers at all for an
    # empty module — the common case for a module too small to bother.
    index = embeddings.build_exemplar_index([])
    assert index.phrases == []
    assert index.vectors == []


def test_classify_via_embeddings_on_empty_index_returns_unknown_without_loading_a_backend():
    index = embeddings.ExemplarIndex(phrases=[], intents=[], vectors=[])
    intent, confidence = embeddings.classify_via_embeddings("anything", index)
    assert (intent, confidence) == ("unknown", 0.0)


def test_loading_the_backend_without_the_dependency_raises_a_clear_error(monkeypatch):
    # Force the lazy singleton to actually attempt a load — if
    # sentence-transformers isn't installed in this test environment
    # (the expected case, per this module's whole "never a hard
    # dependency" design), this must raise EmbeddingsUnavailableError,
    # never a bare ImportError or a silent "unknown".
    embeddings.reset_backend_cache()
    try:
        import sentence_transformers  # noqa: F401
        pytest.skip("sentence-transformers is installed in this environment — nothing to test here")
    except ImportError:
        pass

    with pytest.raises(embeddings.EmbeddingsUnavailableError):
        embeddings._load_backend()


def test_fuse_boost_prefers_confident_classical_over_embedding():
    intent, confidence, source = embeddings.fuse_tier1_5(
        "bot_create", 0.9, "mcp_list", 0.8, fusion="boost",
    )
    assert (intent, confidence, source) == ("bot_create", 0.9, "classical")


def test_fuse_boost_falls_back_to_embedding_when_classical_unsure():
    intent, confidence, source = embeddings.fuse_tier1_5(
        "unknown", 0.1, "mcp_list", 0.8, fusion="boost",
    )
    assert (intent, confidence, source) == ("mcp_list", 0.8, "embedding")


def test_fuse_boost_stays_unknown_when_both_are_unsure():
    intent, confidence, source = embeddings.fuse_tier1_5(
        "unknown", 0.1, "unknown", 0.05, fusion="boost",
    )
    assert (intent, confidence, source) == ("unknown", 0.1, "classical")


def test_fuse_boost_never_overrides_a_confident_classical_even_if_embedding_disagrees():
    intent, confidence, source = embeddings.fuse_tier1_5(
        "bot_create", 0.5, "mcp_list", 0.99, fusion="boost", classical_threshold=0.22,
    )
    assert (intent, confidence, source) == ("bot_create", 0.5, "classical")


def test_fuse_weighted_blends_agreeing_results():
    intent, confidence, source = embeddings.fuse_tier1_5(
        "bot_create", 0.4, "bot_create", 0.8, fusion="weighted", weight=0.5,
    )
    assert intent == "bot_create"
    assert confidence == pytest.approx(0.6)
    assert source == "fused"


def test_fuse_weighted_disagreement_picks_higher_weighted_score():
    intent, confidence, source = embeddings.fuse_tier1_5(
        "bot_create", 0.9, "mcp_list", 0.9, fusion="weighted", weight=0.1,
    )
    # classical_weighted = 0.9*0.9=0.81, embedding_weighted = 0.1*0.9=0.09
    assert (intent, source) == ("bot_create", "classical")


def test_fuse_weighted_both_unknown_returns_unknown():
    intent, confidence, source = embeddings.fuse_tier1_5(
        "unknown", 0.0, "unknown", 0.0, fusion="weighted",
    )
    assert (intent, confidence, source) == ("unknown", 0.0, "classical")


def test_fuse_rejects_an_unknown_fusion_mode():
    with pytest.raises(ValueError):
        embeddings.fuse_tier1_5("a", 0.5, "b", 0.5, fusion="not_a_real_mode")
