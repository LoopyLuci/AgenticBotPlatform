"""spawn_subagent's background=true mode — returns immediately with a
dispatch_id while children keep running on the same event loop; a later
list_subagents call (in the same turn, or conceptually a later one) can
then see them finish. See the plan that added this
(bot/agent_runtime/subagents.py's run_batch docstring) for the honest,
bounded scope of what "background" means here — not a claim of surviving
across a process restart, just "doesn't block the calling turn."
"""

from __future__ import annotations

import asyncio
import collections

import pytest

from bot import bot_instances
from bot.agent_runtime import subagent_registry, subagents
from bot.agent_runtime import tools as agent_tools
from bot.backends.base import BackendResult
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _create_instance():
    return bot_instances.create_instance(
        name="manager", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False,
    )


class _SlowBackend:
    def __init__(self, delay_s=0.05):
        self.model = "fake-model"
        self.name = "native_agent"
        self.delay_s = delay_s

    async def ask(self, prompt, *, context=None, timeout_s=30):
        await asyncio.sleep(self.delay_s)
        return BackendResult(text=f"done: {prompt}", tokens=None, raw=None)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    monkeypatch.setattr(subagent_registry, "_dispatches", collections.OrderedDict())


def _patch_inherited_backend(monkeypatch, backend):
    monkeypatch.setattr(subagents, "_resolve_inherited_backend", lambda parent_instance_id: backend)


def test_background_returns_immediately_without_waiting_for_children(temp_db, monkeypatch):
    instance_id = _create_instance()
    backend = _SlowBackend(delay_s=0.2)
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        start = asyncio.get_event_loop().time()
        result = await subagents.run_batch(
            [{"goal": "a"}, {"goal": "b"}], parent_instance_id=instance_id, background=True,
        )
        elapsed = asyncio.get_event_loop().time() - start
        return result, elapsed

    result, elapsed = _run(scenario())
    assert result["dispatch_id"]
    assert result["children"] == [{"index": 0, "goal": "a"}, {"index": 1, "goal": "b"}]
    assert elapsed < 0.1  # nowhere near the backend's own 0.2s delay per child


def test_background_children_are_visible_running_then_finished(temp_db, monkeypatch):
    instance_id = _create_instance()
    backend = _SlowBackend(delay_s=0.05)
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        result = await subagents.run_batch(
            [{"goal": "a"}], parent_instance_id=instance_id, background=True,
        )
        dispatch_id = result["dispatch_id"]
        immediately = subagent_registry.describe(dispatch_id, parent_instance_id=instance_id)
        await asyncio.sleep(0.15)
        later = subagent_registry.describe(dispatch_id, parent_instance_id=instance_id)
        return immediately, later

    immediately, later = _run(scenario())
    assert immediately["children"][0]["status"] == "running"
    assert later["children"][0]["status"] == "ok"
    assert later["children"][0]["result_excerpt"] == "done: a"


def test_spawn_subagent_tool_background_flag(temp_db, monkeypatch, tmp_path):
    import json

    instance_id = _create_instance()
    backend = _SlowBackend(delay_s=0.2)
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        return await agent_tools.execute_tool(
            "spawn_subagent", {"tasks": [{"goal": "x"}], "background": True},
            workspace=tmp_path, instance_id=instance_id,
        )

    output = _run(scenario())
    parsed = json.loads(output)
    assert parsed["dispatch_id"]
    assert parsed["children"] == [{"index": 0, "goal": "x"}]


def test_spawn_subagent_tool_defaults_to_blocking(temp_db, monkeypatch, tmp_path):
    import json

    instance_id = _create_instance()
    backend = _SlowBackend(delay_s=0.01)
    _patch_inherited_backend(monkeypatch, backend)

    async def scenario():
        return await agent_tools.execute_tool(
            "spawn_subagent", {"tasks": [{"goal": "x"}]}, workspace=tmp_path, instance_id=instance_id,
        )

    output = _run(scenario())
    parsed = json.loads(output)
    assert parsed["children"][0]["status"] == "ok"
    assert parsed["children"][0]["result_excerpt"] == "done: x"
