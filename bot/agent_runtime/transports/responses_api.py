"""OpenAI Responses API transport (`/v1/responses`) — a structurally
different wire shape from chat-completions (openai_compatible.py):
`input` instead of `messages`, flat function-tool schemas instead of
`{"type":"function","function":{...}}`, `max_output_tokens` instead of
`max_tokens`, and tool results submitted as `function_call_output` input
items rather than `role:"tool"` messages. Selected per-provider via
config/providers.yaml's existing `protocol` field (`protocol:
"responses"`, alongside the default `"openai"` chat-completions value) —
see bot/providers.py::set_provider and
bot/agent_runtime/transports/__init__.py::build_openai_transport, the
one place that picks between this transport and OpenAICompatibleTransport.

Real OpenAI reasoning models (the o-series and beyond) increasingly
require this endpoint rather than chat-completions — this transport
exists to reach those, and any other endpoint that has adopted the same
Responses API shape.

This transport owns its own stored-history shape (not the generic
{"role","content"} chat shape openai_compatible.py reconstructs from) —
an assistant turn's `content` is `{"output": [...]}`, the verbatim
`output` array the API returned, replayed back as `input` items on the
next call (this endpoint is otherwise stateless per-request unless a
caller opts into `previous_response_id` chaining, which this transport
does not use, keeping every call self-contained and simple to retry).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, ToolCall
from bot.backends.base import BackendError

logger = logging.getLogger("bot.agent_runtime.transports.responses_api")

API_MODE = "responses"


def to_responses_tools(anthropic_tool_schemas: list[dict]) -> list[dict]:
    """Anthropic-shaped {name, description, input_schema} -> the
    Responses API's flat function-tool shape (no nested "function" key,
    unlike chat-completions)."""
    return [
        {
            "type": "function",
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("input_schema") or {"type": "object", "properties": {}},
        }
        for schema in anthropic_tool_schemas
    ]


class ResponsesApiTransport(ProviderTransport):
    def __init__(self, base_url: str, api_key: Optional[str] = None, catalog_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Same per-vendor quirk-profile lookup key as OpenAICompatibleTransport
        # — see bot/agent_runtime/provider_quirks.py.
        self.catalog_id = catalog_id

    supports_vision = True

    def user_message(self, text: str, *, images: Optional[list[dict[str, str]]] = None) -> dict:
        if not images:
            return {"role": "user", "content": text}
        blocks: list[dict] = [{"type": "input_text", "text": text}]
        for img in images:
            blocks.append({"type": "input_image", "image_url": f"data:{img['mime_type']};base64,{img['data_b64']}"})
        return {"role": "user", "content": blocks}

    def tool_result_messages(self, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [{"role": "tool", "content": {"call_id": tc.id, "output": output}} for tc, output in results]

    async def send(
        self,
        *,
        model: str,
        history: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
        timeout_s: float,
        system_prompt: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> NormalizedResponse:
        input_items = _to_input_items(history)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict = {"model": model, "input": input_items, "max_output_tokens": max_tokens}
        if system_prompt:
            payload["instructions"] = system_prompt
        if tool_schemas:
            payload["tools"] = to_responses_tools(tool_schemas)
            payload["tool_choice"] = "auto"

        # Best-effort, same honest stance as openai_compatible.py's
        # reasoning_effort passthrough — real for OpenAI's o-series
        # reasoning models on this endpoint, ignored by anything else.
        from bot import effort as effort_module

        reasoning_effort = effort_module.to_openai_reasoning_effort(effort)
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}

        from bot.agent_runtime import provider_quirks

        quirk_profile = provider_quirks.profile_for(self.catalog_id, self.base_url)
        provider_quirks.apply(payload, profile=quirk_profile, effort=effort)

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                resp = await client.post(f"{self.base_url}/responses", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except httpx.TimeoutException as exc:
                raise BackendError(f"responses-api transport ({self.base_url}) timed out after {timeout_s}s") from exc
            except httpx.HTTPStatusError as exc:
                raise BackendError(
                    f"responses-api transport ({self.base_url}) returned "
                    f"{exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            except Exception as exc:
                raise BackendError(f"responses-api transport ({self.base_url}) error: {exc}") from exc

        usage = data.get("usage") or {}
        tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)

        output = data.get("output") or []
        if not output:
            raise BackendError(f"responses-api transport ({self.base_url}) returned no output")

        text_parts = []
        tool_calls: list[ToolCall] = []
        for item in output:
            item_type = item.get("type")
            if item_type == "message":
                for block in item.get("content") or []:
                    if block.get("type") == "output_text":
                        text_parts.append(block.get("text", ""))
            elif item_type == "function_call":
                try:
                    import json

                    args = json.loads(item.get("arguments") or "{}")
                except ValueError:
                    args = {}
                tool_calls.append(ToolCall(id=item.get("call_id"), name=item.get("name", ""), arguments=args))

        return NormalizedResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            tokens=tokens or None,
            assistant_message={"role": "assistant", "content": {"output": output}},
        )


def _to_input_items(history: list[dict]) -> list[dict]:
    items: list[dict] = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content")
        if role == "assistant":
            payload = content if isinstance(content, dict) else {}
            items.extend(payload.get("output") or [])
        elif role == "tool":
            payload = content if isinstance(content, dict) else {}
            items.append({"type": "function_call_output", "call_id": payload.get("call_id"), "output": payload.get("output")})
        elif isinstance(content, list):
            # A multimodal user turn (see user_message(images=...)) —
            # already wire-ready input blocks, passed through verbatim.
            items.append({"type": "message", "role": role or "user", "content": content})
        else:
            text = content if isinstance(content, str) else (content or {}).get("content", "")
            items.append({"type": "message", "role": role or "user", "content": [{"type": "input_text", "text": text}]})
    return items
