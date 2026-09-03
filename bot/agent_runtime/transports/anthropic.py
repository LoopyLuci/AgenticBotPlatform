"""Anthropic Messages API transport — extracted from what
bot/backends/api_backend.py used to do inline. Anthropic's own wire shape
already matches BotServer's TOOL_SCHEMAS (`{name, description,
input_schema}`) and its own stored-history convention (a plain string
for a simple text turn, a list of content blocks for anything richer),
so this transport does the least translation work of the two — its main
job is turning SDK response objects into the shared NormalizedResponse
shape.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, ToolCall
from bot.backends.base import BackendError

API_MODE = "anthropic_messages"


class AnthropicTransport(ProviderTransport):
    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            api_key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise BackendError("ANTHROPIC_API_KEY is not set")
            self._client = AsyncAnthropic(api_key=api_key)
        return self._client

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def tool_result_messages(self, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc.id, "content": output}
                    for tc, output in results
                ],
            }
        ]

    async def send(
        self,
        *,
        model: str,
        history: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
        timeout_s: float,
        system_prompt: Optional[str] = None,
    ) -> NormalizedResponse:
        client = self._get_client()
        # Anthropic's stored-history shape IS the wire shape already
        # (each entry is exactly {"role","content"} with content already
        # either a plain string or a list of content blocks) — no
        # conversion needed, unlike the OpenAI-compatible transport.
        create_kwargs = dict(model=model, max_tokens=max_tokens, messages=history)
        if tool_schemas:
            create_kwargs["tools"] = tool_schemas
        if system_prompt:
            create_kwargs["system"] = system_prompt
        try:
            resp = await asyncio.wait_for(client.messages.create(**create_kwargs), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise BackendError(f"anthropic transport timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise BackendError(f"anthropic transport error: {exc}") from exc

        tokens = None
        if resp.usage:
            tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)

        assistant_blocks = _serialize_blocks(resp.content)
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=b.input)
            for b in resp.content
            if getattr(b, "type", "") == "tool_use"
        ]
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return NormalizedResponse(
            text=text,
            tool_calls=tool_calls if resp.stop_reason == "tool_use" else [],
            tokens=tokens,
            assistant_message={"role": "assistant", "content": assistant_blocks},
        )


def _serialize_blocks(content) -> list[dict]:
    out = []
    for block in content:
        btype = getattr(block, "type", "")
        if btype == "text":
            out.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        else:
            out.append({"type": btype})
    return out
