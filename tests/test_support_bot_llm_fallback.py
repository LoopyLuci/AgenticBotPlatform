"""bot/support_bot/llm_fallback.py — Tier 2 of the Support Bot NLU
cascade. Every test mocks the free-model provider list, swarm_budget,
and moa._single_call — never a real LLM call.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import db, swarm_budget
from bot.agent_runtime import moa as moa_module
from bot.support_bot import llm_fallback


def _run(coro):
    return asyncio.run(coro)


def _fake_pricing(monkeypatch, grouped):
    async def _fake(*a, **kw):
        return grouped, "live"

    monkeypatch.setattr("bot.models.custom_models_with_pricing", _fake)


def _fake_providers(monkeypatch, names):
    monkeypatch.setattr("bot.providers.list_providers", lambda: {n: {} for n in names})


def _one_free_provider(monkeypatch, name="alpha", model="alpha-cheap"):
    _fake_providers(monkeypatch, [name])
    _fake_pricing(monkeypatch, {name: [{"id": model, "free": True}]})


CANDIDATE_INTENTS = ["bot_restart", "mcp_list", "unknown"]


def test_classify_via_llm_returns_none_with_no_eligible_provider(monkeypatch):
    _fake_providers(monkeypatch, ["beta"])
    _fake_pricing(monkeypatch, {"beta": [{"id": "beta-pro", "free": False}]})

    result = _run(llm_fallback.classify_via_llm("restart the bot", CANDIDATE_INTENTS))
    assert result is None


def test_classify_via_llm_returns_none_when_budget_refuses(monkeypatch):
    _one_free_provider(monkeypatch)
    monkeypatch.setattr(swarm_budget, "check_budget", lambda **kw: swarm_budget.BudgetDecision(False, "over budget", None))

    result = _run(llm_fallback.classify_via_llm("restart the bot", CANDIDATE_INTENTS))
    assert result is None


def test_classify_via_llm_returns_the_matched_intent(monkeypatch):
    _one_free_provider(monkeypatch)

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        return "bot_restart"

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_via_llm("restart the bot", CANDIDATE_INTENTS))
    assert result == ("bot_restart", "alpha", "alpha-cheap")


def test_classify_via_llm_is_case_and_whitespace_insensitive(monkeypatch):
    _one_free_provider(monkeypatch)

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        return '  "Bot_Restart"  \n'

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_via_llm("restart the bot", CANDIDATE_INTENTS))
    assert result == ("bot_restart", "alpha", "alpha-cheap")


def test_classify_via_llm_honors_a_genuine_unknown_reply(monkeypatch):
    _one_free_provider(monkeypatch)

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        return "unknown"

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_via_llm("what is the meaning of life", CANDIDATE_INTENTS))
    assert result == ("unknown", "alpha", "alpha-cheap")


def test_classify_via_llm_falls_through_to_the_next_candidate_on_timeout(monkeypatch):
    _fake_providers(monkeypatch, ["alpha", "beta"])
    _fake_pricing(monkeypatch, {
        "alpha": [{"id": "alpha-cheap", "free": True}],
        "beta": [{"id": "beta-cheap", "free": True}],
    })

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        if provider == "alpha":
            raise asyncio.TimeoutError()
        return "mcp_list"

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_via_llm("list the mcp servers", CANDIDATE_INTENTS))
    assert result == ("mcp_list", "beta", "beta-cheap")


def test_classify_via_llm_falls_through_on_a_reply_outside_the_closed_set(monkeypatch):
    _fake_providers(monkeypatch, ["alpha", "beta"])
    _fake_pricing(monkeypatch, {
        "alpha": [{"id": "alpha-cheap", "free": True}],
        "beta": [{"id": "beta-cheap", "free": True}],
    })

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        if provider == "alpha":
            return "some hallucinated garbage answer"
        return "bot_restart"

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_via_llm("restart it", CANDIDATE_INTENTS))
    assert result == ("bot_restart", "beta", "beta-cheap")


def test_classify_via_llm_returns_none_when_every_candidate_fails(monkeypatch):
    _one_free_provider(monkeypatch)

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_via_llm("restart it", CANDIDATE_INTENTS))
    assert result is None


def test_classify_and_record_writes_a_pending_example_for_a_confident_result(temp_db, monkeypatch):
    _one_free_provider(monkeypatch)

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        return "bot_restart"

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_and_record("restart it please", CANDIDATE_INTENTS))
    assert result == ("bot_restart", 0.5)

    pending = db.list_support_bot_pending_examples()
    assert len(pending) == 1
    assert pending[0]["phrase"] == "restart it please"
    assert pending[0]["intent"] == "bot_restart"
    assert pending[0]["source_kind"] == "llm_fallback_live"
    assert pending[0]["status"] == "pending"  # never auto-approved, even from Tier 2


def test_classify_and_record_does_not_record_a_genuine_unknown(temp_db, monkeypatch):
    _one_free_provider(monkeypatch)

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        return "unknown"

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    result = _run(llm_fallback.classify_and_record("gibberish nonsense", CANDIDATE_INTENTS))
    assert result == ("unknown", 0.0)
    assert db.list_support_bot_pending_examples() == []


def test_classify_and_record_returns_none_when_tier2_is_unavailable(temp_db, monkeypatch):
    _fake_providers(monkeypatch, ["beta"])
    _fake_pricing(monkeypatch, {"beta": [{"id": "beta-pro", "free": False}]})

    result = _run(llm_fallback.classify_and_record("restart it", CANDIDATE_INTENTS))
    assert result is None
    assert db.list_support_bot_pending_examples() == []
