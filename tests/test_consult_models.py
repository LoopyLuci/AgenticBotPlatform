"""Phase D of the Native Hermes-parity plan: bot/agent_runtime/moa.py and
the "consult_models" tool — mixture-of-agents consensus as an on-demand
tool. _single_call is faked directly (per-reference/aggregator calls),
mirroring how test_spawn_subagent.py fakes backend.ask() — this is about
the fan-out/aggregation/error-isolation logic, not the wire protocol.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.agent_runtime import moa
from bot.agent_runtime import tools as agent_tools
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


def test_no_aggregator_returns_labeled_raw_answers(monkeypatch):
    async def fake_single_call(provider, model, prompt):
        return f"answer from {model}"

    monkeypatch.setattr(moa, "_single_call", fake_single_call)

    result = _run(moa.consult(
        "what's 2+2?",
        [{"provider": "openrouter", "model": "free-model"}, {"model": "claude-sonnet-5"}],
    ))

    assert "[openrouter/free-model]: answer from free-model" in result
    # provider omitted -> labeled "anthropic" (the built-in Claude API).
    assert "[anthropic/claude-sonnet-5]: answer from claude-sonnet-5" in result


def test_aggregator_synthesizes_one_final_answer(monkeypatch):
    calls = []

    async def fake_single_call(provider, model, prompt):
        calls.append((provider, model, prompt))
        if model == "aggregator-model":
            return "synthesized final answer"
        return f"raw answer from {model}"

    monkeypatch.setattr(moa, "_single_call", fake_single_call)

    result = _run(moa.consult(
        "what's 2+2?",
        [{"model": "model-a"}, {"model": "model-b"}],
        aggregator={"model": "aggregator-model"},
    ))

    assert result == "synthesized final answer"
    # The aggregator call's prompt must actually include both references'
    # labeled answers, proving real synthesis input, not a stub.
    aggregator_call = next(c for c in calls if c[1] == "aggregator-model")
    assert "raw answer from model-a" in aggregator_call[2]
    assert "raw answer from model-b" in aggregator_call[2]


def test_one_reference_failing_does_not_fail_the_whole_call(monkeypatch):
    async def fake_single_call(provider, model, prompt):
        if model == "broken-model":
            raise RuntimeError("connection refused")
        return "a good answer"

    monkeypatch.setattr(moa, "_single_call", fake_single_call)

    result = _run(moa.consult(
        "question", [{"model": "broken-model"}, {"model": "good-model"}],
    ))

    assert "failed: connection refused" in result
    assert "a good answer" in result


def test_requires_at_least_one_reference():
    with pytest.raises(BackendError, match="at least one reference"):
        _run(moa.consult("q", []))


def test_unknown_provider_reference_is_isolated_as_a_failed_reference():
    # _transport_for's unknown-provider check runs per-reference, inside
    # _run_reference's own try/except — so it's isolated exactly like any
    # other reference failure, not a whole-call raise. provider="nope"
    # fails on provider lookup, never touching the network.
    result = _run(moa.consult("q", [{"provider": "nope", "model": "m"}]))
    assert "failed:" in result
    assert "no provider named" in result


def test_unknown_provider_on_aggregator_itself_raises(monkeypatch):
    # The aggregator's own call isn't wrapped in per-reference error
    # isolation (there's only ever one aggregator, and its failure means
    # the whole consult has nothing to return) — an unknown provider
    # there propagates as a real BackendError. The one reference here
    # deliberately targets a provider that doesn't exist either, so this
    # test never depends on (or attempts) a real network call regardless
    # of whether a real ANTHROPIC_API_KEY happens to be set in this
    # environment.
    with pytest.raises(BackendError, match="no provider named"):
        _run(moa.consult("q", [{"provider": "also-nope", "model": "m"}], aggregator={"provider": "nope", "model": "agg"}))


# ------------------------------------------------------------- tool wrapper


def test_consult_models_tool_requires_question(temp_db, tmp_path):
    with pytest.raises(agent_tools.ToolError, match="question"):
        _run(agent_tools.execute_tool(
            "consult_models", {"question": "", "references": [{"model": "x"}]},
            workspace=tmp_path, instance_id=None,
        ))


def test_consult_models_tool_requires_references(temp_db, tmp_path):
    with pytest.raises(agent_tools.ToolError, match="references"):
        _run(agent_tools.execute_tool(
            "consult_models", {"question": "q", "references": []},
            workspace=tmp_path, instance_id=None,
        ))


def test_consult_models_tool_returns_text(temp_db, monkeypatch, tmp_path):
    async def fake_single_call(provider, model, prompt):
        return "answer"

    monkeypatch.setattr(moa, "_single_call", fake_single_call)

    result = _run(agent_tools.execute_tool(
        "consult_models", {"question": "q", "references": [{"model": "m"}]},
        workspace=tmp_path, instance_id=None,
    ))
    assert "answer" in result
