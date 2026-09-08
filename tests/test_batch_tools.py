"""dispatch_batch_completions/check_batch_status/get_batch_results agent
tool wrappers (Phase F of the Claude API/Claude Code parity plan) —
bot.agent_runtime.batches faked, matching the shared pattern this file's
sibling test_agent_tools_kanban_scheduler_skills.py already uses for
other thin agent-tool wrappers.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bot.agent_runtime import tools as agent_tools
from bot.backends.base import BackendError


def _run(coro):
    return asyncio.run(coro)


def _exec(name, tool_input, *, tmp_path):
    return _run(agent_tools.execute_tool(name, tool_input, workspace=tmp_path, instance_id=None))


def test_dispatch_batch_completions_requires_tasks(tmp_path):
    with pytest.raises(agent_tools.ToolError, match="tasks must be"):
        _exec("dispatch_batch_completions", {"model": "claude-sonnet-5"}, tmp_path=tmp_path)


def test_dispatch_batch_completions_requires_a_model(tmp_path):
    with pytest.raises(agent_tools.ToolError, match="model is required"):
        _exec("dispatch_batch_completions", {"tasks": [{"custom_id": "t1", "goal": "x"}]}, tmp_path=tmp_path)


def test_dispatch_batch_completions_returns_the_batch_id(tmp_path, monkeypatch):
    async def _fake_submit(tasks, *, model, system_prompt=None):
        return "msgbatch_123"

    monkeypatch.setattr("bot.agent_runtime.batches.submit", _fake_submit)

    result = _exec(
        "dispatch_batch_completions",
        {"tasks": [{"custom_id": "t1", "goal": "summarize"}], "model": "claude-sonnet-5"},
        tmp_path=tmp_path,
    )

    assert json.loads(result) == {"batch_id": "msgbatch_123"}


def test_dispatch_batch_completions_wraps_backend_error(tmp_path, monkeypatch):
    async def _failing_submit(tasks, *, model, system_prompt=None):
        raise BackendError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr("bot.agent_runtime.batches.submit", _failing_submit)

    with pytest.raises(agent_tools.ToolError, match="ANTHROPIC_API_KEY"):
        _exec(
            "dispatch_batch_completions",
            {"tasks": [{"custom_id": "t1", "goal": "x"}], "model": "claude-sonnet-5"},
            tmp_path=tmp_path,
        )


def test_check_batch_status_requires_batch_id(tmp_path):
    with pytest.raises(agent_tools.ToolError, match="batch_id is required"):
        _exec("check_batch_status", {}, tmp_path=tmp_path)


def test_check_batch_status_returns_the_real_status(tmp_path, monkeypatch):
    async def _fake_status(batch_id):
        return {"id": batch_id, "processing_status": "ended", "request_counts": {"succeeded": 1}}

    monkeypatch.setattr("bot.agent_runtime.batches.status", _fake_status)

    result = _exec("check_batch_status", {"batch_id": "msgbatch_123"}, tmp_path=tmp_path)

    assert json.loads(result)["processing_status"] == "ended"


def test_get_batch_results_requires_batch_id(tmp_path):
    with pytest.raises(agent_tools.ToolError, match="batch_id is required"):
        _exec("get_batch_results", {}, tmp_path=tmp_path)


def test_get_batch_results_returns_the_real_results(tmp_path, monkeypatch):
    async def _fake_results(batch_id):
        return [{"custom_id": "t1", "status": "succeeded", "text": "the summary", "error": None}]

    monkeypatch.setattr("bot.agent_runtime.batches.results", _fake_results)

    result = _exec("get_batch_results", {"batch_id": "msgbatch_123"}, tmp_path=tmp_path)

    assert json.loads(result) == [{"custom_id": "t1", "status": "succeeded", "text": "the summary", "error": None}]


def test_batch_tools_are_registered_in_all_tool_schemas():
    names = {s["name"] for s in agent_tools.all_tool_schemas()}
    assert {"dispatch_batch_completions", "check_batch_status", "get_batch_results"} <= names
