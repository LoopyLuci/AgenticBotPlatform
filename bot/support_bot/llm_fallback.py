"""Tier 2 of the Support Bot NLU cascade (next-generation modular hybrid
plan): a real LLM call, invoked only when both classical tiers (Tier
0/on-device, Tier 1/server, and Tier 1.5/embeddings if enabled) come back
"unknown" — the genuinely hard/novel-phrasing cases the classical
subsystem can't resolve. Deliberately not on the hot path for every
message: an LLM call, even a free-tier one, has real latency and a real
(if $0) dispatch cost that bot/swarm_budget.py's own guard exists to
cap — this tier only ever runs after the much cheaper classical tiers
have already said they can't help.

Free-model-first, same hard constraint as
bot/support_bot/synthetic_gen.py — reuses
bot/support_bot/model_providers.py's shared provider-selection helper
and bot/support_bot/synthetic_gen.py's own paid_model_overrides config
key (one "never dispatch to a paid model" rule for the whole subsystem,
not two independently-configured ones).

A confident result here answers the user immediately AND feeds back into
the same pending-review queue synthetic_gen.py writes to
(support_bot_pending_examples), tagged source_kind="llm_fallback_live"
so the dashboard's pending list can visually distinguish "a real
production miss, now labeled" from "a synthetic phrasing" — closing the
active-learning loop on real traffic, not just generated examples.
"""

from __future__ import annotations

import asyncio
from typing import Optional

# Kept short and cheap: this is a closed-set classification, not a
# generative task — a handful of tokens is plenty for "reply with one
# intent name or 'unknown'."
_MAX_TOKENS = 32
_TIMEOUT_S = 15.0
_WAIT_FOR_BUFFER_S = 5.0
_MAX_ATTEMPTS = 3


def _build_prompt(text: str, candidate_intents: list[str]) -> str:
    intents_block = "\n".join(f"- {intent}" for intent in candidate_intents)
    return (
        "You are classifying a short user message into exactly ONE intent "
        "from a fixed list, for a server-management assistant. Reply with "
        "ONLY the intent name, exactly as written below, and nothing else. "
        "If none of them genuinely fit, reply with exactly: unknown\n\n"
        f"Intents:\n{intents_block}\n\n"
        f"Message: {text!r}\n\n"
        "Intent:"
    )


def _parse_reply(raw: str, candidate_intents: list[str]) -> Optional[str]:
    """Returns the matched intent name (normalized casing restored) or
    "unknown", or None if the reply didn't cleanly match anything in the
    closed set at all — never guesses, never returns a hallucinated
    intent name outside the registry."""
    cleaned = raw.strip().strip("\"'.").lower()
    valid = {intent.lower(): intent for intent in candidate_intents}
    valid["unknown"] = "unknown"
    return valid.get(cleaned)


async def classify_via_llm(text: str, candidate_intents: list[str]) -> Optional[tuple[str, str, str]]:
    """Returns (intent, provider, model) — intent may legitimately be
    "unknown" (the model's own honest answer, still worth trusting over
    guessing). Returns None only when no answer could be obtained at all
    (no eligible free provider, budget refused, or every candidate
    model's call failed/timed out/replied outside the closed set) —
    callers must treat None as "Tier 2 unavailable," never as a
    classification result."""
    from bot.config import config
    from bot.support_bot.model_providers import free_provider_models

    paid_overrides = dict(
        config.current.get("support_bot", {}).get("synthetic_gen", {}).get("paid_model_overrides") or {}
    )
    candidates = await free_provider_models(paid_overrides)
    if not candidates:
        return None

    from bot import swarm_budget

    cfg = config.current.get("swarm_budget", {})
    decision = swarm_budget.check_budget(pricing_row={"free": True}, max_children=1, confirm=True, cfg=cfg)
    if not decision.allowed:
        return None

    from bot.agent_runtime.moa import _single_call
    from bot.backends.base import BackendError

    prompt = _build_prompt(text, candidate_intents)
    for provider, model in candidates[:_MAX_ATTEMPTS]:
        try:
            raw = await asyncio.wait_for(
                _single_call(provider, model, prompt, max_tokens=_MAX_TOKENS, timeout_s=_TIMEOUT_S),
                timeout=_TIMEOUT_S + _WAIT_FOR_BUFFER_S,
            )
        except (asyncio.TimeoutError, BackendError):
            continue
        intent = _parse_reply(raw, candidate_intents)
        if intent is not None:
            return intent, provider, model
        # A reply outside the closed set is treated the same as a failed
        # call — try the next candidate rather than trusting a
        # hallucinated intent name.
    return None


async def classify_and_record(text: str, candidate_intents: list[str]) -> Optional[tuple[str, float]]:
    """The full Tier 2 flow: classify, then — only for a confident (non-
    "unknown") result — feed it back into the pending-review queue.
    Returns (intent, confidence) for the caller to use as the answer;
    confidence is a fixed 0.5 (an LLM's own textual "yes I'm sure" isn't
    a calibrated probability the way a softmax/cosine score is, so this
    deliberately doesn't pretend to one) — high enough to be preferred
    over a classical "unknown", explicitly not implying more precision
    than a single free-model opinion actually has. Returns None if Tier
    2 itself was unavailable (see classify_via_llm's own contract)."""
    result = await classify_via_llm(text, candidate_intents)
    if result is None:
        return None
    intent, provider, model = result
    if intent == "unknown":
        return "unknown", 0.0

    from bot import db

    try:
        db.add_support_bot_pending_example(
            text, intent, source_provider=provider, source_model=model, source_kind="llm_fallback_live",
        )
    except Exception:
        # Recording the miss for later review must never block returning
        # the answer the user is actually waiting on.
        pass
    return intent, 0.5
