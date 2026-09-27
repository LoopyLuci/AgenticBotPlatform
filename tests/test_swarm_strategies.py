"""Every built-in swarm strategy against scripted members (no real backends):
success, partial failure, total failure, and malformed planner output."""
from __future__ import annotations

import asyncio

import pytest

from bot.backends.base import BackendError
from bot.swarm import strategies
from bot.swarm.base import SwarmStrategyError


@pytest.fixture
def members(monkeypatch):
    """script[instance_id] = callable(prompt) -> text, or an exception to raise."""
    script: dict = {}
    calls: list = []

    async def fake_run_member(instance_id, prompt, *, swarm_run_id, action_type="quick_question", user_id=0):
        calls.append((instance_id, prompt))
        behaviour = script.get(instance_id, lambda p: f"answer from {instance_id}")
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour(prompt)

    monkeypatch.setattr(strategies, "run_member", fake_run_member)
    monkeypatch.setattr(strategies, "instance_label", lambda i: f"bot{i}")
    return script, calls


def run(strategy, config, prompt="Q?"):
    return asyncio.run(strategies.STRATEGIES[strategy]().run({"config": config}, prompt, swarm_run_id="r1"))


def test_fanout_synthesizes_and_marks_partial_on_one_failure(members):
    script, calls = members
    script[2] = BackendError("down")
    r = run("fanout_synthesize", {"members": [1, 2, 3], "synthesizer": 9})
    assert r.status == "partial" and r.result == "answer from 9"
    synth_prompt = calls[-1][1]
    assert "bot1" in synth_prompt and "bot3" in synth_prompt and "bot2" not in synth_prompt


def test_fanout_without_synthesizer_concatenates(members):
    r = run("fanout_synthesize", {"members": [1, 2]})
    assert r.status == "success" and "— bot1 —" in r.result and "— bot2 —" in r.result


def test_any_member_exception_is_that_members_failure_not_the_swarms(members):
    script, _ = members
    script[1] = RuntimeError("unexpected")
    script[2] = asyncio.TimeoutError()
    r = run("fanout_synthesize", {"members": [1, 2, 3]})
    assert r.status == "partial"
    assert {s["instance_id"]: s["status"] for s in r.steps} == {1: "failed", 2: "failed", 3: "success"}


def test_everyone_failing_fails_cleanly(members):
    script, _ = members
    script[1] = script[2] = BackendError("x")
    r = run("leader_vote", {"members": [1, 2], "leader": 9})
    assert r.status == "failed" and r.error == "every member failed"


def test_leader_vote_and_leader_failure(members):
    script, _ = members
    assert run("leader_vote", {"members": [1, 2], "leader": 9}).result == "answer from 9"
    script[9] = BackendError("leader down")
    r = run("leader_vote", {"members": [1, 2], "leader": 9})
    assert r.status == "failed" and "leader failed" in r.error


def test_sequential_relay_passes_output_forward_and_stops_on_failure(members):
    script, calls = members
    script[1] = lambda p: "draft"
    script[2] = lambda p: "refined"
    r = run("sequential_relay", {"members": [1, {"instance_id": 2, "instruction": "Polish it."}]})
    assert r.result == "refined"
    assert "draft" in calls[1][1] and "Polish it." in calls[1][1]
    script[1] = BackendError("x")
    r = run("sequential_relay", {"members": [1, 2]})
    assert r.status == "failed" and len(r.steps) == 1


@pytest.mark.parametrize("plan", [
    '[{"subtask": "a"}, {"subtask": "b"}]',
    'Sure! Here you go: ["a", "b"] hope that helps',
])
def test_decompose_accepts_both_plan_shapes(members, plan):
    script, calls = members
    script[1] = lambda p: plan
    r = run("decompose_delegate", {"planner": 1, "members": [2, 3], "aggregator": 4})
    assert r.status == "success" and r.result == "answer from 4"
    assert sorted(c[1] for c in calls if c[0] in (2, 3)) == ["a", "b"]


@pytest.mark.parametrize("plan", ["no json here", "[]", "[1, 2]", '[{"nope": 1}]'])
def test_decompose_fails_cleanly_on_a_useless_plan(members, plan):
    script, _ = members
    script[1] = lambda p: plan
    r = run("decompose_delegate", {"planner": 1, "members": [2], "aggregator": 4})
    assert r.status == "failed" and "planner" in r.error


def test_decompose_caps_a_runaway_plan(members):
    script, calls = members
    script[1] = lambda p: "[" + ",".join(f'"t{i}"' for i in range(50)) + "]"
    run("decompose_delegate", {"planner": 1, "members": [2, 3], "aggregator": 4})
    assert len([c for c in calls if c[0] in (2, 3)]) == 4


def test_custom_graph_runs_in_dependency_order_with_inputs(members):
    script, calls = members
    script[1] = lambda p: "facts"
    script[2] = lambda p: "summary"
    steps = [{"id": "research", "instance_id": 1}, {"id": "write", "instance_id": 2, "depends_on": ["research"]}]
    r = run("custom", {"steps": steps})
    assert r.status == "success" and "— write —\nsummary" in r.result
    assert "facts" in calls[1][1]


@pytest.mark.parametrize("steps,msg", [
    ([], "at least one step"),
    ([{"id": "a", "instance_id": 1, "depends_on": ["ghost"]}], "unknown step"),
    ([{"id": "a", "instance_id": 1, "depends_on": ["b"]}, {"id": "b", "instance_id": 2, "depends_on": ["a"]}], "cycle"),
])
def test_custom_rejects_bad_graphs(members, steps, msg):
    with pytest.raises(SwarmStrategyError, match=msg):
        run("custom", {"steps": steps})


@pytest.mark.parametrize("strategy,config", [
    ("fanout_synthesize", {}), ("leader_vote", {"members": [1]}), ("sequential_relay", {}),
    ("decompose_delegate", {"planner": 1, "members": [2]}),
])
def test_missing_required_config_is_a_clear_error(members, strategy, config):
    with pytest.raises(SwarmStrategyError):
        run(strategy, config)
