"""Phase C of the Native Hermes-parity plan: bot/agent_runtime/subagents.py
and the "spawn_subagent" tool in bot/agent_runtime/tools.py — ephemeral,
parallel sub-agent fan-out, the native equivalent of Hermes Agent's own
delegate_task. Backends are faked at bot.agent_runtime.subagents'
resolve functions (not at the transport/HTTP boundary) since this is
about the batching/role/depth/output_schema logic, not the wire protocol
— that's already covered by test_transports.py/test_api_backend.py.
"""

from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, db
from bot.agent_runtime import subagents
from bot.agent_runtime import tools as agent_tools
from bot.backends.base import BackendError, BackendResult
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _create_instance(name="manager", backend="api", model=None):
    return bot_instances.create_instance(
        name=name, platform="telegram", backend=backend,
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False, model=model,
    )


class _FakeBackend:
    """Stands in for a NativeAgentBackend — records every ask() call's
    (prompt, context) and returns queued replies in order."""

    def __init__(self, replies, model="fake-model", name="native_agent"):
        self._replies = list(replies)
        self.model = model
        self.name = name
        self.calls = []

    async def ask(self, prompt, *, context=None, timeout_s=30):
        self.calls.append({"prompt": prompt, "context": context})
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return BackendResult(text=reply, tokens=None, raw=None)


def _patch_inherited_backend(monkeypatch, backend):
    monkeypatch.setattr(subagents, "_resolve_inherited_backend", lambda parent_instance_id: backend)


def test_run_batch_runs_all_tasks_in_parallel(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend(["answer one", "answer two"])
    _patch_inherited_backend(monkeypatch, backend)

    results = _run(subagents.run_batch(
        [{"goal": "task one"}, {"goal": "task two"}], parent_instance_id=instance_id,
    ))

    assert len(results) == 2
    assert {r["goal"] for r in results} == {"task one", "task two"}
    assert all(r["status"] == "ok" for r in results)
    assert len(backend.calls) == 2


def test_run_batch_records_ephemeral_sessions(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend(["done"])
    _patch_inherited_backend(monkeypatch, backend)

    _run(subagents.run_batch([{"goal": "one task"}], parent_instance_id=instance_id))

    rows = db.list_ephemeral_sessions(instance_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["goal"] == "one task"
    assert rows[0]["result"] == "done"


def test_leaf_role_strips_dangerous_delegation_tools(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend(["ok"])
    _patch_inherited_backend(monkeypatch, backend)

    _run(subagents.run_batch([{"goal": "x"}], role="leaf", parent_instance_id=instance_id))

    allowed = backend.calls[0]["context"]["allowed_tools"]
    for blocked in subagents.LEAF_BLOCKED_TOOLS:
        assert blocked not in allowed
    assert "read_file" in allowed  # ordinary tools stay available


def test_orchestrator_role_keeps_full_tool_list(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend(["ok"])
    _patch_inherited_backend(monkeypatch, backend)

    _run(subagents.run_batch([{"goal": "x"}], role="orchestrator", parent_instance_id=instance_id))

    assert "allowed_tools" not in backend.calls[0]["context"]


def test_empty_goal_errors_without_calling_backend(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend([])
    _patch_inherited_backend(monkeypatch, backend)

    results = _run(subagents.run_batch([{"goal": "  "}], parent_instance_id=instance_id))

    assert results[0]["status"] == "error"
    assert backend.calls == []


def test_child_exception_is_captured_not_raised(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend([RuntimeError("boom")])
    _patch_inherited_backend(monkeypatch, backend)

    results = _run(subagents.run_batch([{"goal": "x"}], parent_instance_id=instance_id))

    assert results[0]["status"] == "error"
    assert "boom" in results[0]["result_excerpt"]


def test_max_concurrent_children_config_caps_semaphore(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {"max_concurrent_children": 2}})
    instance_id = _create_instance()

    in_flight = {"count": 0, "max": 0}

    class _TrackingBackend(_FakeBackend):
        async def ask(self, prompt, *, context=None, timeout_s=30):
            in_flight["count"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["count"])
            await asyncio.sleep(0.01)
            in_flight["count"] -= 1
            return await super().ask(prompt, context=context, timeout_s=timeout_s)

    backend = _TrackingBackend(["a", "b", "c", "d"])
    _patch_inherited_backend(monkeypatch, backend)

    _run(subagents.run_batch(
        [{"goal": f"t{i}"} for i in range(4)], parent_instance_id=instance_id,
    ))

    assert in_flight["max"] <= 2


def test_top_level_call_past_configured_depth_raises(temp_db, monkeypatch):
    # depth=0 (fresh call) vs. max_delegation_depth=0 means even the
    # FIRST call is already at the ceiling — the simplest way to exercise
    # the raise path directly, without relying on how a child's own
    # internal failure is reported (see the next test for that).
    monkeypatch.setattr(config, "_data", {"agent_runtime": {"max_delegation_depth": 0}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend([])
    _patch_inherited_backend(monkeypatch, backend)

    with pytest.raises(BackendError, match="depth limit"):
        _run(subagents.run_batch([{"goal": "top"}], parent_instance_id=instance_id))
    assert backend.calls == []


def test_grandchild_hitting_depth_limit_surfaces_as_a_failed_child_not_a_crash(temp_db, monkeypatch):
    # A child's own internal failure (including hitting the depth limit
    # one level deeper) must never crash the whole outer batch — it
    # surfaces as that one child's status="error" result, the same
    # error-isolation every other per-child exception gets.
    monkeypatch.setattr(config, "_data", {"agent_runtime": {"max_delegation_depth": 1}, "native_agent": {}})
    instance_id = _create_instance()

    async def nested_ask(prompt, *, context=None, timeout_s=30):
        # Simulate a child itself calling spawn_subagent again, nested
        # inside the same await chain the ContextVar tracks.
        return BackendResult(
            text=str(await subagents.run_batch([{"goal": "grandchild"}], parent_instance_id=instance_id)),
            tokens=None, raw=None,
        )

    backend = _FakeBackend([])
    backend.ask = nested_ask
    _patch_inherited_backend(monkeypatch, backend)

    results = _run(subagents.run_batch([{"goal": "top"}], parent_instance_id=instance_id))

    assert results[0]["status"] == "error"
    assert "depth limit" in results[0]["result_excerpt"]


def test_resolve_named_backend_unknown_provider_raises(temp_db):
    with pytest.raises(BackendError, match="no provider named"):
        subagents._resolve_named_backend("nope", "some-model")


def test_resolve_inherited_backend_requires_parent_instance():
    with pytest.raises(BackendError, match="parent instance context"):
        subagents._resolve_inherited_backend(None)


def test_resolve_inherited_backend_for_api_instance(temp_db):
    instance_id = _create_instance(backend="api", model="claude-opus-4.8")
    backend = subagents._resolve_inherited_backend(instance_id)
    assert backend.model == "claude-opus-4.8"


# ------------------------------------------------------------- tool wrapper


def test_spawn_subagent_tool_requires_nonempty_tasks(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(
            "spawn_subagent", {"tasks": []}, workspace=tmp_path, instance_id=instance_id,
        ))


def test_spawn_subagent_tool_rejects_invalid_role(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError, match="role"):
        _run(agent_tools.execute_tool(
            "spawn_subagent", {"tasks": [{"goal": "x"}], "role": "manager"},
            workspace=tmp_path, instance_id=instance_id,
        ))


def test_spawn_subagent_tool_requires_provider_and_model_together(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError, match="provider and model"):
        _run(agent_tools.execute_tool(
            "spawn_subagent", {"tasks": [{"goal": "x"}], "provider": "openrouter"},
            workspace=tmp_path, instance_id=instance_id,
        ))


def test_spawn_subagent_tool_returns_json_results(temp_db, monkeypatch, tmp_path):
    import json

    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend(["hello"])
    _patch_inherited_backend(monkeypatch, backend)

    output = _run(agent_tools.execute_tool(
        "spawn_subagent", {"tasks": [{"goal": "say hi"}]}, workspace=tmp_path, instance_id=instance_id,
    ))

    parsed = json.loads(output)
    assert parsed[0]["status"] == "ok"
    assert parsed[0]["goal"] == "say hi"
