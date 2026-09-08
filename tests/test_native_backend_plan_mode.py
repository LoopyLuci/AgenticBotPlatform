"""NativeAgentBackend.ask()'s plan-mode gate (Phase G of the Claude API/
Claude Code parity plan) — context["plan_first"] forces one extra,
tools-disabled turn (enforced by omitting tool_schemas entirely, not
just a prompt instruction) before the real tool-enabled loop runs, gated
on a human approving the proposed plan via the same approval.py
machinery a dangerous tool call already uses.
"""
from __future__ import annotations

import asyncio

from bot import bot_instances
from bot.agent_runtime import approval
from bot.agent_runtime.transports.base import NormalizedResponse, ToolCall
from bot.backends.native_backend import NativeAgentBackend


def _run(coro):
    return asyncio.run(coro)


def _make_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1],
    )


class _ScriptedTransport:
    supports_vision = False
    supports_documents = False

    def __init__(self, responses):
        self._responses = list(responses)
        self.send_calls: list[dict] = []

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return [{"role": "user", "content": f"tool result: {output}"} for _, output in results]

    async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
        self.send_calls.append({"tool_schemas": tool_schemas, "system_prompt": system_prompt, "history": list(history)})
        return self._responses.pop(0)


def _auto_approve_notify_factory(outcome="once"):
    async def notify(approval_id, tool_name, tool_input):
        assert tool_name == approval.PLAN_APPROVAL_TOOL_NAME
        approval.resolve(approval_id, outcome, actor="tester")

    return notify


def test_plan_turn_omits_tool_schemas_even_when_the_backend_has_tools(temp_db, tmp_path):
    plan_response = NormalizedResponse(text="1. list files\n2. read config", assistant_message={"role": "assistant", "content": "the plan"})
    final_response = NormalizedResponse(text="all done", assistant_message={"role": "assistant", "content": "all done"})
    transport = _ScriptedTransport([plan_response, final_response])
    backend = NativeAgentBackend(transport, model="whatever")

    result = _run(backend.ask("do the thing", context={
        "cwd": str(tmp_path / "ws"), "plan_first": True, "instance_id": 1, "chat_id": 1,
        "approval_notify": _auto_approve_notify_factory(),
    }))

    assert result.text == "all done"
    assert transport.send_calls[0]["tool_schemas"] == []  # the plan turn itself
    # The real turn afterward gets tools again (all_tool_schemas() has
    # real entries — this test's fake backend has no tool_schemas override,
    # so the second call's tool_schemas come from the real registry, which
    # is non-empty).
    assert transport.send_calls[1]["tool_schemas"] != []


def test_denied_plan_never_reaches_the_real_turn(temp_db, tmp_path):
    plan_response = NormalizedResponse(text="1. delete everything", assistant_message={"role": "assistant", "content": "the plan"})
    transport = _ScriptedTransport([plan_response])
    backend = NativeAgentBackend(transport, model="whatever")

    result = _run(backend.ask("do the thing", context={
        "cwd": str(tmp_path / "ws"), "plan_first": True, "instance_id": 1, "chat_id": 1,
        "approval_notify": _auto_approve_notify_factory(outcome="deny"),
    }))

    assert result.text == "Plan denied."
    assert result.raw["plan_denied"] is True
    assert len(transport.send_calls) == 1  # never reached a second, tool-enabled turn


def test_no_notify_channel_times_out_and_denies(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(approval, "DEFAULT_TIMEOUT_S", 0.05)
    plan_response = NormalizedResponse(text="a plan", assistant_message={"role": "assistant", "content": "the plan"})
    transport = _ScriptedTransport([plan_response])
    backend = NativeAgentBackend(transport, model="whatever")

    result = _run(backend.ask("do the thing", context={
        "cwd": str(tmp_path / "ws"), "plan_first": True, "instance_id": 1, "chat_id": 1,
    }))

    assert result.text == "Plan denied."


def test_no_plan_first_behaves_exactly_as_before(temp_db, tmp_path):
    final_response = NormalizedResponse(text="done, no plan needed", assistant_message={"role": "assistant", "content": "done"})
    transport = _ScriptedTransport([final_response])
    backend = NativeAgentBackend(transport, model="whatever")

    result = _run(backend.ask("do the thing", context={"cwd": str(tmp_path / "ws")}))

    assert result.text == "done, no plan needed"
    assert len(transport.send_calls) == 1


def test_approved_plan_persists_into_history(temp_db, tmp_path):
    from bot import db as botdb

    plan_response = NormalizedResponse(text="1. step one", assistant_message={"role": "assistant", "content": "the plan text"})
    final_response = NormalizedResponse(text="all done", assistant_message={"role": "assistant", "content": "all done"})
    transport = _ScriptedTransport([plan_response, final_response])
    backend = NativeAgentBackend(transport, model="whatever")

    _run(backend.ask("do the thing", context={
        "cwd": str(tmp_path / "ws"), "plan_first": True, "instance_id": 1, "chat_id": 1,
        "approval_notify": _auto_approve_notify_factory(),
    }))

    # The second (real) turn's history must include the plan proposal and
    # the "approved" ack, not just the original prompt.
    second_turn_history = transport.send_calls[1]["history"]
    contents = [str(entry.get("content")) for entry in second_turn_history]
    assert any("the plan text" in c for c in contents)
    assert any("approved" in c.lower() for c in contents)
