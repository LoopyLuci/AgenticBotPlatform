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

    result = _run(subagents.run_batch(
        [{"goal": "task one"}, {"goal": "task two"}], parent_instance_id=instance_id,
    ))

    results = result["children"]
    assert result["dispatch_id"]
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

    results = _run(subagents.run_batch([{"goal": "  "}], parent_instance_id=instance_id))["children"]

    assert results[0]["status"] == "error"
    assert backend.calls == []


def test_child_exception_is_captured_not_raised(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    instance_id = _create_instance()
    backend = _FakeBackend([RuntimeError("boom")])
    _patch_inherited_backend(monkeypatch, backend)

    results = _run(subagents.run_batch([{"goal": "x"}], parent_instance_id=instance_id))["children"]

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

    results = _run(subagents.run_batch([{"goal": "top"}], parent_instance_id=instance_id))["children"]

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
    assert parsed["dispatch_id"]
    assert parsed["children"][0]["status"] == "ok"
    assert parsed["children"][0]["goal"] == "say hi"


class TestGlobalBackgroundCap:
    def test_refuses_a_new_child_once_the_global_ceiling_is_hit_across_calls(self, temp_db, monkeypatch):
        """The confirmed gap: each run_batch() call's own semaphore only
        bounds concurrency WITHIN that call — this must catch
        accumulation ACROSS separate dispatches sharing one event loop
        (real production: one long-running process, not a fresh loop per
        call — asyncio.run() per test call would tear down and orphan any
        unawaited background task, so both dispatches happen inside one
        async function here to reflect how the real server actually runs)."""
        monkeypatch.setattr(config, "_data", {
            "agent_runtime": {}, "native_agent": {"max_global_background_children": 1},
        })
        instance_id = _create_instance()

        class _NeverFinishingBackend(_FakeBackend):
            async def ask(self, prompt, *, context=None, timeout_s=30):
                self.calls.append({"prompt": prompt, "context": context})
                await asyncio.sleep(10)
                return BackendResult(text="never", tokens=None, raw=None)

        backend = _NeverFinishingBackend([])
        _patch_inherited_backend(monkeypatch, backend)

        async def scenario():
            first = await subagents.run_batch(
                [{"goal": "one"}], parent_instance_id=instance_id, background=True,
            )
            assert first["dispatch_id"]

            with pytest.raises(BackendError, match="global background-dispatch limit"):
                await subagents.run_batch(
                    [{"goal": "two"}], parent_instance_id=instance_id, background=True,
                )

            from bot.agent_runtime import subagent_registry
            dispatch = subagent_registry.get_dispatch(first["dispatch_id"])
            for handle in dispatch.children.values():
                handle.task.cancel()

        _run(scenario())

    def test_a_finished_child_frees_up_room_for_the_next_call(self, temp_db, monkeypatch):
        monkeypatch.setattr(config, "_data", {
            "agent_runtime": {}, "native_agent": {"max_global_background_children": 1},
        })
        instance_id = _create_instance()
        backend = _FakeBackend(["done one", "done two"])
        _patch_inherited_backend(monkeypatch, backend)

        _run(subagents.run_batch([{"goal": "one"}], parent_instance_id=instance_id))
        # First batch already finished (blocking) by the time run_batch
        # returns, so its child no longer counts as live.
        result = _run(subagents.run_batch([{"goal": "two"}], parent_instance_id=instance_id))

        assert result["children"][0]["status"] == "ok"


class TestAgentSettingsIntegration:
    """bot/agent_settings.py's unified settings surface must actually
    influence a real dispatch when the caller doesn't override
    explicitly — an explicit call-site arg always still wins."""

    def test_max_concurrent_children_falls_back_to_agent_settings(self, temp_db, monkeypatch):
        from bot import agent_settings

        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()
        agent_settings.set_settings(instance_id, max_concurrent_children=1)
        backend = _FakeBackend(["a", "b"])
        _patch_inherited_backend(monkeypatch, backend)

        semaphores_seen = []
        real_semaphore_cls = asyncio.Semaphore

        def spy(value):
            sem = real_semaphore_cls(value)
            semaphores_seen.append(value)
            return sem

        monkeypatch.setattr(subagents.asyncio, "Semaphore", spy)

        _run(subagents.run_batch([{"goal": "one"}, {"goal": "two"}], parent_instance_id=instance_id))

        assert semaphores_seen == [1]

    def test_explicit_max_children_is_still_capped_by_agent_settings(self, temp_db, monkeypatch):
        """Matches run_batch's existing min()-based cap logic (pre-dating
        agent_settings): a caller's explicit max_children is a ceiling
        request, not an override — the smaller of the two always wins,
        exactly as it already did against config.yaml's own cap."""
        from bot import agent_settings

        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()
        agent_settings.set_settings(instance_id, max_concurrent_children=1)
        backend = _FakeBackend(["a", "b"])
        _patch_inherited_backend(monkeypatch, backend)

        semaphores_seen = []
        real_semaphore_cls = asyncio.Semaphore

        def spy(value):
            semaphores_seen.append(value)
            return real_semaphore_cls(value)

        monkeypatch.setattr(subagents.asyncio, "Semaphore", spy)

        # min(explicit=5, settings' cfg_cap=1) = 1 — settings.py's own
        # process default still bounds an explicit call-site value higher
        # than it, matching run_batch's existing min()-based cap logic.
        _run(subagents.run_batch(
            [{"goal": "one"}, {"goal": "two"}], parent_instance_id=instance_id, max_children=5,
        ))

        assert semaphores_seen == [1]

    def test_worker_effort_reaches_child_context(self, temp_db, monkeypatch):
        from bot import agent_settings

        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()
        agent_settings.set_settings(instance_id, worker_effort="low")
        backend = _FakeBackend(["a"])
        _patch_inherited_backend(monkeypatch, backend)

        _run(subagents.run_batch([{"goal": "one"}], parent_instance_id=instance_id))

        assert backend.calls[0]["context"]["effort"] == "low"

    def test_worker_provider_model_fall_back_to_agent_settings(self, temp_db, monkeypatch):
        from bot import agent_settings, providers as provider_registry

        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()
        agent_settings.set_settings(instance_id, worker_provider="myprovider", worker_model="my/model")

        seen = {}

        def fake_named(provider, model):
            seen["provider"] = provider
            seen["model"] = model
            return _FakeBackend(["a"])

        monkeypatch.setattr(subagents, "_resolve_named_backend", fake_named)

        _run(subagents.run_batch([{"goal": "one"}], parent_instance_id=instance_id))

        assert seen == {"provider": "myprovider", "model": "my/model"}

    def test_per_task_effort_overrides_the_batch_level_effort(self, temp_db, monkeypatch):
        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()
        backend = _FakeBackend(["a", "b"])
        _patch_inherited_backend(monkeypatch, backend)

        _run(subagents.run_batch(
            [{"goal": "one", "effort": "max"}, {"goal": "two"}],
            parent_instance_id=instance_id, effort="low",
        ))

        assert backend.calls[0]["context"]["effort"] == "max"
        assert backend.calls[1]["context"]["effort"] == "low"

    def test_per_task_provider_model_overrides_the_batch_default(self, temp_db, monkeypatch):
        from bot import providers as provider_registry

        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()

        resolved = []

        def fake_named(provider, model):
            resolved.append((provider, model))
            return _FakeBackend(["a"])

        monkeypatch.setattr(subagents, "_resolve_named_backend", fake_named)

        _run(subagents.run_batch(
            [
                {"goal": "one", "provider": "task-provider", "model": "task/model"},
                {"goal": "two"},
            ],
            parent_instance_id=instance_id, provider="batch-provider", model="batch/model",
        ))

        assert ("task-provider", "task/model") in resolved
        # The batch default is resolved once up front (used by the
        # second, non-overriding task) — the override task additionally
        # resolves its own distinct provider/model.
        assert resolved.count(("batch-provider", "batch/model")) == 1
        assert resolved.count(("task-provider", "task/model")) == 1

    def test_per_task_provider_without_model_is_an_error(self, temp_db, monkeypatch):
        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()
        backend = _FakeBackend(["a"])
        _patch_inherited_backend(monkeypatch, backend)

        with pytest.raises(BackendError, match="both be given"):
            _run(subagents.run_batch(
                [{"goal": "one", "provider": "only-provider"}], parent_instance_id=instance_id,
            ))

    def test_explicit_provider_model_still_wins_over_agent_settings(self, temp_db, monkeypatch):
        from bot import agent_settings

        monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
        instance_id = _create_instance()
        agent_settings.set_settings(instance_id, worker_provider="settings-provider", worker_model="settings/model")

        seen = {}

        def fake_named(provider, model):
            seen["provider"] = provider
            seen["model"] = model
            return _FakeBackend(["a"])

        monkeypatch.setattr(subagents, "_resolve_named_backend", fake_named)

        _run(subagents.run_batch(
            [{"goal": "one"}], parent_instance_id=instance_id, provider="explicit-provider", model="explicit/model",
        ))

        assert seen == {"provider": "explicit-provider", "model": "explicit/model"}
