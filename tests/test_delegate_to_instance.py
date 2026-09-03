"""Phase 4 of the Hermes-swarm plan: giving an `api`-backend agent's own
tool loop the same "manager delegates to a worker" lever Hermes agents
already have via delegate_task — bot.agent_runtime.tools.execute_tool's
new "delegate_to_instance" branch.
"""

from __future__ import annotations

import asyncio

from bot import bot_instances
from bot.agent_runtime import tools as agent_tools
from bot.backends.base import BackendResult
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _create_instance(name, can_target=None):
    return bot_instances.create_instance(
        name=name, platform="telegram", backend="cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False, can_target=can_target or [],
    )


def test_delegate_to_instance_calls_router_ask_against_target(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})
    source_id = _create_instance("manager")
    target_id = _create_instance("worker")

    calls = []

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        calls.append({"prompt": prompt, "action_type": action_type, "instance_id": instance_id})
        return BackendResult(text="worker's answer", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    result = _run(agent_tools.execute_tool(
        "delegate_to_instance", {"target_instance": str(target_id), "prompt": "do the thing"},
        workspace=tmp_path, instance_id=source_id,
    ))

    assert result == "worker's answer"
    assert calls == [{"prompt": "do the thing", "action_type": "agent_delegate", "instance_id": target_id}]


def test_delegate_to_instance_logs_to_audit_log(temp_db, monkeypatch, tmp_path):
    # Found missing while building the delegation-activity dashboard
    # panel — ask_instance and dispatch_swarm_goal both wrote a
    # "source -> target: prompt" audit_log row, this one didn't, making
    # it invisible in that panel's audit-log-backed data.
    from bot import db

    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})
    source_id = _create_instance("manager")
    target_id = _create_instance("worker")

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        return BackendResult(text="ok", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    _run(agent_tools.execute_tool(
        "delegate_to_instance", {"target_instance": str(target_id), "prompt": "do the thing"},
        workspace=tmp_path, instance_id=source_id,
    ))

    rows = db.list_audit_log(actions=["agent_delegate"])
    assert len(rows) == 1
    assert rows[0]["actor"] == "agent:manager"
    assert rows[0]["detail"] == "-> worker: do the thing"


def test_delegate_to_instance_blocked_by_allowlist(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "allowlist"}, "agent_runtime": {}})
    source_id = _create_instance("manager", can_target=[])
    target_id = _create_instance("worker")

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        raise AssertionError("router.ask should never be called when the allowlist denies the target")

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    try:
        _run(agent_tools.execute_tool(
            "delegate_to_instance", {"target_instance": str(target_id), "prompt": "do the thing"},
            workspace=tmp_path, instance_id=source_id,
        ))
        assert False, "expected ToolError"
    except agent_tools.ToolError as exc:
        assert "not permitted" in str(exc)


def test_delegate_to_instance_allowed_when_target_in_can_target(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "allowlist"}, "agent_runtime": {}})
    target_id = _create_instance("worker")
    source_id = _create_instance("manager", can_target=[target_id])

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        return BackendResult(text="ok", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    result = _run(agent_tools.execute_tool(
        "delegate_to_instance", {"target_instance": str(target_id), "prompt": "do the thing"},
        workspace=tmp_path, instance_id=source_id,
    ))
    assert result == "ok"


def test_delegate_to_instance_enforces_max_depth(temp_db, monkeypatch, tmp_path):
    # max_delegation_depth=1: the first hop (depth 0 -> 1) is allowed; a
    # second hop nested directly inside it (simulating B's own tool loop
    # immediately delegating again, within the same await chain the
    # ContextVar tracks) must be refused.
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {"max_delegation_depth": 1}})
    a_id = _create_instance("a")
    b_id = _create_instance("b")

    async def fake_ask(prompt, *, action_type=None, instance_id=None, **kwargs):
        # Simulate B's own tool loop immediately delegating back to A,
        # nested inside the same await chain as the first hop.
        return BackendResult(
            text=await agent_tools.execute_tool(
                "delegate_to_instance", {"target_instance": str(a_id), "prompt": "loop back"},
                workspace=tmp_path, instance_id=b_id,
            ),
            tokens=None, raw=None,
        )

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)

    try:
        _run(agent_tools.execute_tool(
            "delegate_to_instance", {"target_instance": str(b_id), "prompt": "start"},
            workspace=tmp_path, instance_id=a_id,
        ))
        assert False, "expected a ToolError from the depth limit"
    except agent_tools.ToolError as exc:
        assert "depth limit" in str(exc)


def test_delegate_to_instance_requires_target_and_prompt(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})
    source_id = _create_instance("manager")

    try:
        _run(agent_tools.execute_tool(
            "delegate_to_instance", {"target_instance": "", "prompt": "x"},
            workspace=tmp_path, instance_id=source_id,
        ))
        assert False, "expected ToolError"
    except agent_tools.ToolError:
        pass


def test_delegate_to_instance_unknown_target(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})
    source_id = _create_instance("manager")

    try:
        _run(agent_tools.execute_tool(
            "delegate_to_instance", {"target_instance": "does-not-exist", "prompt": "x"},
            workspace=tmp_path, instance_id=source_id,
        ))
        assert False, "expected ToolError"
    except agent_tools.ToolError as exc:
        assert "no bot instance found" in str(exc)
