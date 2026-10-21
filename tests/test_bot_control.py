"""Agents in the workspace controlling registered bots: list, @mention, sessions, files, fan-out."""

from __future__ import annotations

import asyncio
import json

from bot import bot_instances
from bot.agent_runtime import tools as agent_tools
from bot.backends.base import BackendResult
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


def _create_instance(name):
    return bot_instances.create_instance(
        name=name, platform="telegram", backend="cli",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False, can_target=[],
    )


def _fake_router(monkeypatch, calls):
    async def fake_ask(prompt, *, action_type=None, instance_id=None, chat_id=None, **kwargs):
        calls.append({"prompt": prompt, "instance_id": instance_id, "chat_id": chat_id})
        return BackendResult(text=f"reply-{instance_id}", tokens=None, raw=None)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)


def _trust(monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_control": {"mode": "trust_all"}, "agent_runtime": {}})


def test_list_bot_instances_hides_self_and_credentials(temp_db, monkeypatch, tmp_path):
    _trust(monkeypatch)
    me = _create_instance("manager")
    other = _create_instance("worker")
    out = _run(agent_tools.execute_tool("list_bot_instances", {}, workspace=tmp_path, instance_id=me))
    assert '"worker"' in out and '"manager"' not in out and "credentials" not in out
    assert str(other) in out


def test_delegate_keeps_session_files_and_save_to(temp_db, monkeypatch, tmp_path):
    _trust(monkeypatch)
    me = _create_instance("manager")
    target = _create_instance("worker")
    calls = []
    _fake_router(monkeypatch, calls)
    (tmp_path / "in.txt").write_text("FILEBODY", encoding="utf-8")
    args = {"target_instance": "@worker", "prompt": "go", "conversation": "c1", "files": ["in.txt"], "save_to": "out/r.txt"}
    _run(agent_tools.execute_tool("delegate_to_instance", args, workspace=tmp_path, instance_id=me))
    _run(agent_tools.execute_tool("delegate_to_instance", args, workspace=tmp_path, instance_id=me))
    assert calls[0]["chat_id"] == calls[1]["chat_id"] == f"agentconv:{me}:c1:{target}"
    assert "FILEBODY" in calls[0]["prompt"]
    assert (tmp_path / "out" / "r.txt").read_text(encoding="utf-8") == f"reply-{target}"


def test_delegate_to_bots_fans_out_and_isolates_errors(temp_db, monkeypatch, tmp_path):
    _trust(monkeypatch)
    me = _create_instance("manager")
    a = _create_instance("alpha")
    _fake_router(monkeypatch, [])
    out = json.loads(_run(agent_tools.execute_tool(
        "delegate_to_bots", {"tasks": [{"target_instance": "alpha", "prompt": "p1"},
                                       {"target_instance": "ghost", "prompt": "p2"}]},
        workspace=tmp_path, instance_id=me)))
    assert out[0]["reply"] == f"reply-{a}" and "no bot instance" in out[1]["error"]


def test_mentioned_bots_and_discovery(temp_db):
    from bot import agent_control
    from bot.agent_runtime import tool_discovery, toolspec

    ops = _create_instance("ops")
    _create_instance("ops-2")
    assert [b["name"] for b in agent_control.mentioned_bots("ask @ops-2 and @ops please")] == ["ops-2", "ops"]
    assert agent_control.mentioned_bots("ask @ops", exclude_id=ops) == []
    assert agent_control.mentioned_bots("mail a@ops.com") == []
    tok = tool_discovery.begin()
    try:
        wanted = ("list_bot_instances", "delegate_to_instance", "delegate_to_bots", "read_file")
        tool_discovery.attach([s for s in agent_tools.TOOL_SCHEMAS if s["name"] in wanted], [])
        found = [s["name"] for s in tool_discovery.search("delegate a task to a remote bot")]
        assert "delegate_to_instance" in found and "list_bot_instances" in found
    finally:
        tool_discovery.end(tok)
    assert toolspec.spec_for("delegate_to_bots").permission == "agent"
