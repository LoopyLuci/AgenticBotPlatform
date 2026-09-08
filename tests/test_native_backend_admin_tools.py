"""NativeAgentBackend.ask()'s admin-tool schema filtering — a non-admin
instance never sees ADMIN_TOOLS_STANDARD; ADMIN_TOOLS_ELEVATED additionally
requires context["device_tier"] to be "elevated" or "unrestricted". See
the "Admin control surface" plan and docs/adr/0008-single-instance-admin-tool-gate.md.
"""
from __future__ import annotations

import asyncio

from bot import agent_settings, bot_instances
from bot.agent_runtime import tools as agent_tools
from bot.agent_runtime.transports.base import NormalizedResponse
from bot.backends.native_backend import NativeAgentBackend


def _run(coro):
    return asyncio.run(coro)


def _make_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="native_agent",
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
        self.send_calls.append({"tool_schemas": tool_schemas})
        return self._responses.pop(0)


def _final_response():
    return NormalizedResponse(text="done", assistant_message={"role": "assistant", "content": "done"})


def _tool_names(send_call):
    return {s["name"] for s in send_call["tool_schemas"]}


def test_non_admin_instance_never_sees_admin_tools(temp_db, tmp_path):
    instance_id = _make_instance()
    transport = _ScriptedTransport([_final_response()])
    backend = NativeAgentBackend(transport, model="whatever")

    _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))

    names = _tool_names(transport.send_calls[0])
    assert not (names & agent_tools.ADMIN_TOOLS)


def test_admin_instance_sees_standard_tools_but_not_elevated(temp_db, tmp_path):
    instance_id = _make_instance()
    agent_settings.set_settings(instance_id, is_admin_instance=True)
    transport = _ScriptedTransport([_final_response()])
    backend = NativeAgentBackend(transport, model="whatever")

    _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))

    names = _tool_names(transport.send_calls[0])
    assert agent_tools.ADMIN_TOOLS_STANDARD <= names
    assert not (names & agent_tools.ADMIN_TOOLS_ELEVATED)


def test_admin_instance_with_elevated_device_tier_sees_everything(temp_db, tmp_path):
    instance_id = _make_instance()
    agent_settings.set_settings(instance_id, is_admin_instance=True)
    transport = _ScriptedTransport([_final_response()])
    backend = NativeAgentBackend(transport, model="whatever")

    _run(backend.ask("hi", context={
        "cwd": str(tmp_path / "ws"), "instance_id": instance_id, "device_tier": "elevated",
    }))

    names = _tool_names(transport.send_calls[0])
    assert agent_tools.ADMIN_TOOLS_STANDARD <= names
    assert agent_tools.ADMIN_TOOLS_ELEVATED <= names


def test_elevated_device_tier_alone_without_admin_instance_grants_nothing(temp_db, tmp_path):
    instance_id = _make_instance()
    transport = _ScriptedTransport([_final_response()])
    backend = NativeAgentBackend(transport, model="whatever")

    _run(backend.ask("hi", context={
        "cwd": str(tmp_path / "ws"), "instance_id": instance_id, "device_tier": "unrestricted",
    }))

    names = _tool_names(transport.send_calls[0])
    assert not (names & agent_tools.ADMIN_TOOLS)
