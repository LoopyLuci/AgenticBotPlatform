"""bot/agent_runtime/subagent_registry.py + spawn_subagent's list/steer/stop
tools — mid-flight control over already-spawned children, the gap this
session's live audit found relative to Hermes's own delegate_task
action="list"/"steer"/"stop". Uses a real asyncio.Event-gated fake
backend so a child can be genuinely still-running when list/steer/stop
act on it, not just fully synchronous.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot import bot_instances, db
from bot.agent_runtime import subagent_registry, subagents
from bot.agent_runtime import tools as agent_tools
from bot.backends.base import BackendResult
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _create_instance(name="manager"):
    return bot_instances.create_instance(
        name=name, platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


class _GatedBackend:
    """Blocks inside ask() until released — lets a test observe/act on a
    child while it is genuinely still running, and confirms steer_queue
    injection actually reaches the backend's own context."""

    def __init__(self):
        self.model = "fake-model"
        self.name = "native_agent"
        self.release = asyncio.Event()
        self.received_context = None
        self.cancelled = False

    async def ask(self, prompt, *, context=None, timeout_s=30):
        self.received_context = context
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return BackendResult(text="done", tokens=None, raw=None)


def _patch_inherited_backend(monkeypatch, backend):
    monkeypatch.setattr(subagents, "_resolve_inherited_backend", lambda parent_instance_id: backend)


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    # subagent_registry._dispatches is a genuinely process-global dict by
    # design (a background dispatch must survive past the turn that
    # created it) — but that means it isn't reset by temp_db between
    # tests the way the database itself is, and instance_ids DO repeat
    # across tests (each test's own temp_db starts autoincrement at 1
    # again). Without clearing this, a later test's describe_all() can
    # see a finished dispatch left over from an earlier test that
    # happened to reuse the same instance_id.
    import collections

    monkeypatch.setattr(subagent_registry, "_dispatches", collections.OrderedDict())


def test_list_subagents_sees_a_still_running_child(temp_db, monkeypatch):
    instance_id = _create_instance()
    backend = _GatedBackend()
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        task = asyncio.create_task(subagents.run_batch([{"goal": "slow"}], parent_instance_id=instance_id))
        await asyncio.sleep(0.01)  # let the child actually start
        listed = subagent_registry.describe_all(instance_id)
        assert len(listed) == 1
        assert listed[0]["children"][0]["status"] == "running"
        backend.release.set()
        await task

    _run(scenario())


def test_steer_subagent_delivers_message_to_running_child(temp_db, monkeypatch):
    instance_id = _create_instance()
    backend = _GatedBackend()
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        task = asyncio.create_task(subagents.run_batch([{"goal": "slow"}], parent_instance_id=instance_id))
        await asyncio.sleep(0.01)
        dispatch_id = subagent_registry.describe_all(instance_id)[0]["dispatch_id"]

        subagent_registry.steer(dispatch_id, 0, "nudge", parent_instance_id=instance_id)
        queue = backend.received_context["steer_queue"]
        assert queue.get_nowait() == "nudge"

        backend.release.set()
        await task

    _run(scenario())


def test_stop_subagent_cancels_running_child_and_marks_stopped(temp_db, monkeypatch):
    instance_id = _create_instance()
    backend = _GatedBackend()
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        task = asyncio.create_task(subagents.run_batch([{"goal": "slow"}], parent_instance_id=instance_id))
        await asyncio.sleep(0.01)
        dispatch_id = subagent_registry.describe_all(instance_id)[0]["dispatch_id"]

        subagent_registry.stop(dispatch_id, 0, parent_instance_id=instance_id)
        result = await task
        return dispatch_id, result

    dispatch_id, result = _run(scenario())
    assert backend.cancelled
    rows = db.list_ephemeral_sessions(instance_id)
    assert rows[0]["status"] == "stopped"


def test_steer_and_stop_refuse_a_dispatch_belonging_to_another_instance(temp_db, monkeypatch):
    owner_id = _create_instance("owner")
    other_id = _create_instance("other")
    backend = _GatedBackend()
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        task = asyncio.create_task(subagents.run_batch([{"goal": "slow"}], parent_instance_id=owner_id))
        await asyncio.sleep(0.01)
        dispatch_id = subagent_registry.describe_all(owner_id)[0]["dispatch_id"]

        with pytest.raises(agent_tools.ToolError, match="does not belong"):
            subagent_registry.steer(dispatch_id, 0, "hi", parent_instance_id=other_id)
        with pytest.raises(agent_tools.ToolError, match="does not belong"):
            subagent_registry.stop(dispatch_id, 0, parent_instance_id=other_id)

        backend.release.set()
        await task

    _run(scenario())


def test_list_subagents_after_completion_still_returns_final_result(temp_db, monkeypatch):
    instance_id = _create_instance()
    backend = _GatedBackend()
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        task = asyncio.create_task(subagents.run_batch([{"goal": "slow"}], parent_instance_id=instance_id))
        await asyncio.sleep(0.01)
        dispatch_id = subagent_registry.describe_all(instance_id)[0]["dispatch_id"]
        backend.release.set()
        await task
        return dispatch_id

    dispatch_id = _run(scenario())
    described = subagent_registry.describe(dispatch_id, parent_instance_id=instance_id)
    assert described["children"][0]["status"] == "ok"
    assert described["children"][0]["result_excerpt"] == "done"


# ------------------------------------------------------------- tool wrapper


def test_list_subagents_tool_with_no_dispatch_id(temp_db, monkeypatch, tmp_path):
    instance_id = _create_instance()
    backend = _GatedBackend()
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        asyncio.create_task(subagents.run_batch([{"goal": "slow"}], parent_instance_id=instance_id, background=True))
        await asyncio.sleep(0.01)
        output = await agent_tools.execute_tool("list_subagents", {}, workspace=tmp_path, instance_id=instance_id)
        backend.release.set()
        await asyncio.sleep(0.01)
        return output

    output = _run(scenario())
    parsed = json.loads(output)
    assert len(parsed) == 1
    assert parsed[0]["children"][0]["status"] == "running"


def test_steer_subagent_tool_requires_all_fields(temp_db, monkeypatch, tmp_path):
    instance_id = _create_instance()
    with pytest.raises(agent_tools.ToolError):
        _run(agent_tools.execute_tool(
            "steer_subagent", {"dispatch_id": "x"}, workspace=tmp_path, instance_id=instance_id,
        ))
