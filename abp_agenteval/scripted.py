"""A transport that replays a golden trajectory instead of calling a model."""
from __future__ import annotations

import itertools

from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, ToolCall

from .task import Call, Say


class ScriptedTransport(ProviderTransport):
    supports_vision = False
    supports_documents = False

    def __init__(self, script: list):
        self._steps = list(script)
        self._ids = itertools.count(1)
        self.calls = 0

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return [{"role": "user", "content": f"tool result: {output}"} for _, output in results]

    async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
        self.calls += 1
        if not self._steps:
            # The script ran out but the agent asked for more: end the turn so the
            # graders (not an exception) report what was missing.
            return NormalizedResponse(text="", assistant_message={"role": "assistant", "content": ""}, tokens=0)
        step = self._steps.pop(0)
        if isinstance(step, Say):
            return NormalizedResponse(text=step.text, tokens=len(step.text) // 4,
                                      assistant_message={"role": "assistant", "content": step.text})
        assert isinstance(step, Call), f"unknown script step {step!r}"
        pairs = [(step.tool, step.args), *step.more]
        calls = [ToolCall(id=f"call_{next(self._ids)}", name=n, arguments=a) for n, a in pairs]
        return NormalizedResponse(
            text="", tool_calls=calls, tokens=20,
            assistant_message={"role": "assistant", "content": [{"name": c.name, "id": c.id} for c in calls]},
        )
