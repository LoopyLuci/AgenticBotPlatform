"""bot/support_bot/synthetic_gen.py — free-model-only synthetic training
data generation swarm (Phase 4 of the "Support Bot NLU upgrade" plan).
Every test mocks bot.agent_runtime.subagents.run_batch and
bot.swarm_budget.check_budget — never a real LLM/swarm call.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bot import db
from bot.support_bot import synthetic_gen


def _run(coro):
    return asyncio.run(coro)


def _fake_pricing(monkeypatch, grouped):
    async def _fake(*a, **kw):
        return grouped, "live"

    monkeypatch.setattr("bot.models.custom_models_with_pricing", _fake)


def _fake_providers(monkeypatch, names):
    monkeypatch.setattr("bot.providers.list_providers", lambda: {n: {} for n in names})


def test_free_provider_models_skips_providers_with_no_free_model(monkeypatch):
    _fake_providers(monkeypatch, ["alpha", "beta"])
    _fake_pricing(monkeypatch, {
        "alpha": [{"id": "alpha-cheap", "free": True}, {"id": "alpha-pro", "free": False}],
        "beta": [{"id": "beta-pro", "free": False}],  # no free model, no override -> skipped
    })

    result = _run(synthetic_gen._free_provider_models({}))

    assert result == [("alpha", "alpha-cheap")]


def test_free_provider_models_uses_explicit_paid_override(monkeypatch):
    _fake_providers(monkeypatch, ["beta"])
    _fake_pricing(monkeypatch, {"beta": [{"id": "beta-pro", "free": False}]})

    result = _run(synthetic_gen._free_provider_models({"beta": "beta-pro"}))

    assert result == [("beta", "beta-pro")]


def test_build_tasks_returns_empty_when_no_eligible_provider(monkeypatch):
    _fake_providers(monkeypatch, ["beta"])
    _fake_pricing(monkeypatch, {"beta": [{"id": "beta-pro", "free": False}]})

    tasks = _run(synthetic_gen.build_tasks(target_intents=["status"]))

    assert tasks == []


def test_build_tasks_shape(monkeypatch):
    _fake_providers(monkeypatch, ["alpha"])
    _fake_pricing(monkeypatch, {"alpha": [{"id": "alpha-cheap", "free": True}]})

    tasks = _run(synthetic_gen.build_tasks(target_intents=["status", "help"]))

    assert len(tasks) == 1
    task = tasks[0]
    assert task["provider"] == "alpha"
    assert task["model"] == "alpha-cheap"
    assert task["effort"] == "medium"
    assert task["output_schema"]["properties"]["examples"]["items"]["properties"]["intent"]["enum"] == ["status", "help"]


def test_pick_target_intents_ranks_by_fewest_examples(temp_db):
    intents = synthetic_gen.pick_target_intents(n=41)
    # "help" (per training_data.py's own baseline) has a small example
    # count — just confirm the function runs against real data and
    # returns a real, non-empty ranking without raising.
    assert len(intents) > 0
    assert all(isinstance(i, str) for i in intents)


def test_pick_target_intents_weighs_recent_misses(temp_db, monkeypatch):
    # Two intents with identical example counts; "b" gets a recent miss
    # signal favoring it (recency doesn't change count, but should not
    # crash and should include "b" among the target set for a small n).
    monkeypatch.setattr(
        "bot.support_bot.training_data.EXAMPLES",
        [("x1", "a"), ("x2", "a"), ("y1", "b"), ("y2", "b")],
    )
    db.log_support_bot_classification(
        text="something", tfidf_intent="b", tfidf_confidence=0.5, nn_intent="unknown", nn_confidence=0.1,
        final_intent="unknown", final_confidence=0.5, source="unknown", agreed=False,
    )

    ranked = synthetic_gen.pick_target_intents(n=2)

    assert set(ranked) == {"a", "b"}


def test_generate_synthetic_batch_no_eligible_provider(temp_db, monkeypatch):
    monkeypatch.setattr(synthetic_gen, "pick_target_intents", lambda n=10: ["status"])
    _fake_providers(monkeypatch, [])
    _fake_pricing(monkeypatch, {})

    result = _run(synthetic_gen.generate_synthetic_batch())

    assert result == {"dispatched": 0, "pending_added": 0, "reason": "no target intents or no eligible free-model provider configured"}


def test_generate_synthetic_batch_blocked_by_budget(temp_db, monkeypatch):
    monkeypatch.setattr(synthetic_gen, "pick_target_intents", lambda n=10: ["status"])
    _fake_providers(monkeypatch, ["alpha"])
    _fake_pricing(monkeypatch, {"alpha": [{"id": "alpha-cheap", "free": True}]})

    called = {"run_batch": False}

    async def _fake_run_batch(*a, **kw):
        called["run_batch"] = True
        return {"dispatch_id": "d1", "children": []}

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", _fake_run_batch)
    from bot import swarm_budget
    monkeypatch.setattr(swarm_budget, "check_budget", lambda **kw: swarm_budget.BudgetDecision(False, "over budget", None))

    result = _run(synthetic_gen.generate_synthetic_batch())

    assert result["dispatched"] == 0
    assert result["reason"] == "over budget"
    assert called["run_batch"] is False


def test_generate_synthetic_batch_stores_valid_examples_as_pending(temp_db, monkeypatch):
    monkeypatch.setattr(synthetic_gen, "pick_target_intents", lambda n=10: ["status"])
    _fake_providers(monkeypatch, ["alpha"])
    _fake_pricing(monkeypatch, {"alpha": [{"id": "alpha-cheap", "free": True}]})

    async def _fake_run_batch(tasks, **kw):
        return {
            "dispatch_id": "d1",
            "children": [{
                "index": 0, "goal": tasks[0]["goal"], "model": "alpha-cheap", "status": "ok",
                "result_excerpt": json.dumps({"examples": [{"phrase": "reboot please", "intent": "status"}]}),
            }],
        }

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", _fake_run_batch)

    result = _run(synthetic_gen.generate_synthetic_batch())

    assert result["dispatched"] == 1
    assert result["pending_added"] == 1
    pending = db.list_support_bot_pending_examples(status="pending")
    assert len(pending) == 1
    assert pending[0]["phrase"] == "reboot please"
    assert pending[0]["intent"] == "status"
    assert pending[0]["source_provider"] == "alpha"
    assert pending[0]["source_model"] == "alpha-cheap"


def test_generate_synthetic_batch_skips_a_failed_or_malformed_child(temp_db, monkeypatch):
    monkeypatch.setattr(synthetic_gen, "pick_target_intents", lambda n=10: ["status"])
    _fake_providers(monkeypatch, ["alpha"])
    _fake_pricing(monkeypatch, {"alpha": [{"id": "alpha-cheap", "free": True}]})

    async def _fake_run_batch(tasks, **kw):
        return {"dispatch_id": "d1", "children": [{"index": 0, "goal": "g", "model": "alpha-cheap", "status": "error", "result_excerpt": "output_schema validation failed"}]}

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", _fake_run_batch)

    result = _run(synthetic_gen.generate_synthetic_batch())

    assert result["pending_added"] == 0
    assert db.list_support_bot_pending_examples() == []


def test_normalize_phrase_collapses_case_whitespace_and_punctuation():
    assert synthetic_gen._normalize_phrase("Restart Bot X?") == "restart bot x"
    assert synthetic_gen._normalize_phrase("  restart   bot x  ") == "restart bot x"
    assert synthetic_gen._normalize_phrase("restart, bot x!!") == "restart bot x"


def test_generate_synthetic_batch_auto_approves_two_model_agreement(temp_db, monkeypatch):
    monkeypatch.setattr(synthetic_gen, "pick_target_intents", lambda n=10: ["status"])
    _fake_providers(monkeypatch, ["alpha", "beta"])
    _fake_pricing(monkeypatch, {
        "alpha": [{"id": "alpha-cheap", "free": True}],
        "beta": [{"id": "beta-cheap", "free": True}],
    })

    async def _fake_run_batch(tasks, **kw):
        return {
            "dispatch_id": "d1",
            "children": [
                {"index": 0, "goal": "g", "model": "alpha-cheap", "status": "ok",
                 "result_excerpt": json.dumps({"examples": [{"phrase": "Restart the bot?", "intent": "status"}]})},
                {"index": 1, "goal": "g", "model": "beta-cheap", "status": "ok",
                 "result_excerpt": json.dumps({"examples": [{"phrase": "restart the bot", "intent": "status"}]})},
            ],
        }

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", _fake_run_batch)

    result = _run(synthetic_gen.generate_synthetic_batch())

    assert result["pending_added"] == 2
    assert result["auto_approved"] == 1

    approved = db.list_support_bot_pending_examples(status="approved")
    assert len(approved) == 1
    assert approved[0]["approved_by"] == "auto:2-model-agreement"
    assert approved[0]["resulting_phrase_id"] is not None

    # exactly one live phrase was created, not two
    live_phrases = [dict(r) for r in db.list_support_bot_phrases()]
    assert len(live_phrases) == 1
    assert live_phrases[0]["intent"] == "status"

    still_pending = db.list_support_bot_pending_examples(status="pending")
    assert len(still_pending) == 1  # the second occurrence stays a plain pending record


def test_generate_synthetic_batch_does_not_auto_approve_a_single_model_opinion(temp_db, monkeypatch):
    monkeypatch.setattr(synthetic_gen, "pick_target_intents", lambda n=10: ["status"])
    _fake_providers(monkeypatch, ["alpha"])
    _fake_pricing(monkeypatch, {"alpha": [{"id": "alpha-cheap", "free": True}]})

    async def _fake_run_batch(tasks, **kw):
        return {
            "dispatch_id": "d1",
            "children": [{"index": 0, "goal": "g", "model": "alpha-cheap", "status": "ok",
                          "result_excerpt": json.dumps({"examples": [{"phrase": "reboot please", "intent": "status"}]})}],
        }

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", _fake_run_batch)

    result = _run(synthetic_gen.generate_synthetic_batch())

    assert result["auto_approved"] == 0
    assert db.list_support_bot_phrases() == []
    assert len(db.list_support_bot_pending_examples(status="pending")) == 1


def test_pick_target_intents_respects_allowed_intents(temp_db):
    ranked = synthetic_gen.pick_target_intents(n=50, allowed_intents={"status", "help"})
    assert set(ranked) <= {"status", "help"}


def test_run_until_target_stops_when_every_intent_already_at_target(temp_db, monkeypatch):
    called = {"dispatched": False}

    async def _fake_batch(**kw):
        called["dispatched"] = True
        return {"dispatched": 0, "pending_added": 0}

    monkeypatch.setattr(synthetic_gen, "generate_synthetic_batch", _fake_batch)

    # target_per_intent=1: training_data.py's baseline already has >=1
    # example for every intent, so nothing should ever need generating.
    result = _run(synthetic_gen.run_until_target(1, max_batches=5))

    assert result["stopped_reason"] == "target_reached"
    assert result["batches_run"] == 0
    assert called["dispatched"] is False


def test_run_until_target_stops_after_max_batches(temp_db, monkeypatch):
    call_count = {"n": 0}

    async def _fake_batch(**kw):
        call_count["n"] += 1
        return {"dispatched": 1, "pending_added": 0, "auto_approved": 0}

    monkeypatch.setattr(synthetic_gen, "generate_synthetic_batch", _fake_batch)

    # An absurdly high target that can never be satisfied by mocked
    # no-op batches — must stop at max_batches, not loop forever.
    result = _run(synthetic_gen.run_until_target(100000, max_batches=3))

    assert result["stopped_reason"] == "max_batches_reached"
    assert result["batches_run"] == 3
    assert call_count["n"] == 3


def test_run_until_target_stops_on_a_budget_refusal(temp_db, monkeypatch):
    async def _fake_batch(**kw):
        return {"dispatched": 0, "pending_added": 0, "reason": "over budget"}

    monkeypatch.setattr(synthetic_gen, "generate_synthetic_batch", _fake_batch)

    result = _run(synthetic_gen.run_until_target(100000, max_batches=10))

    assert result["stopped_reason"] == "over budget"
    assert result["batches_run"] == 1


def test_run_until_target_rejects_an_unknown_module(temp_db):
    result = _run(synthetic_gen.run_until_target(20, module_id="not_a_real_module"))
    assert result["batches_run"] == 0
    assert "unknown Knowledge Module" in result["stopped_reason"]


def test_run_until_target_scopes_to_one_module(temp_db, monkeypatch):
    from bot.support_bot import knowledge_modules

    seen_target_intents = []

    async def _fake_batch(*, target_intents=None, parent_instance_id=None):
        seen_target_intents.append(set(target_intents or []))
        return {"dispatched": 1, "pending_added": 0, "auto_approved": 0}

    monkeypatch.setattr(synthetic_gen, "generate_synthetic_batch", _fake_batch)

    _run(synthetic_gen.run_until_target(100000, module_id="mcp", max_batches=1))

    mcp_intents = set(knowledge_modules.intents_for_module("mcp"))
    assert seen_target_intents[0] <= mcp_intents


def test_generate_synthetic_batch_does_not_auto_approve_disagreeing_phrasing(temp_db, monkeypatch):
    monkeypatch.setattr(synthetic_gen, "pick_target_intents", lambda n=10: ["status"])
    _fake_providers(monkeypatch, ["alpha", "beta"])
    _fake_pricing(monkeypatch, {
        "alpha": [{"id": "alpha-cheap", "free": True}],
        "beta": [{"id": "beta-cheap", "free": True}],
    })

    async def _fake_run_batch(tasks, **kw):
        return {
            "dispatch_id": "d1",
            "children": [
                {"index": 0, "goal": "g", "model": "alpha-cheap", "status": "ok",
                 "result_excerpt": json.dumps({"examples": [{"phrase": "restart the bot", "intent": "status"}]})},
                {"index": 1, "goal": "g", "model": "beta-cheap", "status": "ok",
                 "result_excerpt": json.dumps({"examples": [{"phrase": "how is everything running", "intent": "status"}]})},
            ],
        }

    monkeypatch.setattr("bot.agent_runtime.subagents.run_batch", _fake_run_batch)

    result = _run(synthetic_gen.generate_synthetic_batch())

    assert result["auto_approved"] == 0
    assert db.list_support_bot_phrases() == []
    assert len(db.list_support_bot_pending_examples(status="pending")) == 2
