"""bot/agent_runtime/tool_loop.py's PreToolUse/PostToolUse hook wiring
(Phase E of the Claude API/Claude Code parity plan) — a hook's deny/ask
decision must interact correctly with the existing approval gate.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from bot import db
from bot.agent_runtime import tool_loop


def _run(coro):
    return asyncio.run(coro)


def _hook_script(tmp_path, body: str) -> str:
    script = tmp_path / f"hook_{abs(hash(body)) % 100000}.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


class _FakeToolError(Exception):
    pass


class _FakeAgentTools:
    ToolError = _FakeToolError

    def __init__(self, dangerous_names=(), output="tool output"):
        self._dangerous = set(dangerous_names)
        self._output = output
        self.executed = []

    def is_dangerous(self, name):
        return name in self._dangerous

    async def execute_tool(self, name, tool_input, *, workspace, instance_id):
        self.executed.append((name, tool_input))
        return self._output


class _FakeApproval:
    def __init__(self, outcome="allow"):
        self.outcome = outcome
        self.calls = []

    async def request_approval(self, instance_id, chat_id, session_key, name, tool_input, notify):
        self.calls.append(name)
        return self.outcome


@pytest.fixture
def deny_hook(temp_db, tmp_path):
    body = """
    import json, sys
    payload = json.load(sys.stdin)
    json.dump({"decision": "deny", "reason": f"blocked {payload['tool_name']}"}, sys.stdout)
    """
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, body), matcher="run_shell")


@pytest.fixture
def ask_hook(temp_db, tmp_path):
    body = """
    import json, sys
    json.dump({"decision": "ask"}, sys.stdout)
    """
    db.add_agent_hook("PreToolUse", _hook_script(tmp_path, body), matcher="list_dir")


def test_pretool_deny_short_circuits_before_execution(deny_hook, tmp_path):
    agent_tools = _FakeAgentTools(dangerous_names=set())
    approval = _FakeApproval()

    result = _run(tool_loop.run_one_tool(
        "run_shell", {"command": "ls"}, workspace=tmp_path, instance_id=1, chat_id=1, session_key="s1",
        notify=None, agent_tools=agent_tools, agent_approval=approval,
    ))

    assert "Denied by hook" in result
    assert "blocked run_shell" in result
    assert agent_tools.executed == []
    assert approval.calls == []


def test_pretool_ask_escalates_into_approval_for_a_non_dangerous_tool(ask_hook, tmp_path):
    agent_tools = _FakeAgentTools(dangerous_names=set())  # list_dir is NOT dangerous on its own
    approval = _FakeApproval(outcome="allow")

    result = _run(tool_loop.run_one_tool(
        "list_dir", {"path": "."}, workspace=tmp_path, instance_id=1, chat_id=1, session_key="s1",
        notify=None, agent_tools=agent_tools, agent_approval=approval,
    ))

    assert approval.calls == ["list_dir"]
    assert agent_tools.executed == [("list_dir", {"path": "."})]
    assert result == "tool output"


def test_pretool_ask_that_gets_denied_by_approval_never_executes(ask_hook, tmp_path):
    agent_tools = _FakeAgentTools(dangerous_names=set())
    approval = _FakeApproval(outcome="deny")

    result = _run(tool_loop.run_one_tool(
        "list_dir", {"path": "."}, workspace=tmp_path, instance_id=1, chat_id=1, session_key="s1",
        notify=None, agent_tools=agent_tools, agent_approval=approval,
    ))

    assert result == "Denied by user."
    assert agent_tools.executed == []


def test_no_hooks_behaves_exactly_as_before(temp_db, tmp_path):
    agent_tools = _FakeAgentTools(dangerous_names=set())
    approval = _FakeApproval()

    result = _run(tool_loop.run_one_tool(
        "list_dir", {"path": "."}, workspace=tmp_path, instance_id=1, chat_id=1, session_key="s1",
        notify=None, agent_tools=agent_tools, agent_approval=approval,
    ))

    assert result == "tool output"
    assert approval.calls == []


def test_posttool_hook_fires_after_a_successful_execution(temp_db, tmp_path):
    captured_file = tmp_path / "captured.json"
    body = f"""
    import json, sys
    payload = json.load(sys.stdin)
    with open(r"{captured_file}", "w") as f:
        json.dump(payload, f)
    """
    db.add_agent_hook("PostToolUse", _hook_script(tmp_path, body), matcher="list_dir")
    agent_tools = _FakeAgentTools(dangerous_names=set(), output="here are the files")
    approval = _FakeApproval()

    result = _run(tool_loop.run_one_tool(
        "list_dir", {"path": "."}, workspace=tmp_path, instance_id=1, chat_id=1, session_key="s1",
        notify=None, agent_tools=agent_tools, agent_approval=approval,
    ))

    assert result == "here are the files"
    import json as _json

    captured = _json.loads(captured_file.read_text())
    assert captured["output"] == "here are the files"
