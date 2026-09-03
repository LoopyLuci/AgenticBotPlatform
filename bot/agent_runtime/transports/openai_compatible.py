"""OpenAI-compatible chat-completions transport — extracted from what
bot/backends/custom_model_backend.py used to do inline. Covers Ollama,
LM Studio, vLLM, llama.cpp's server, OpenRouter, and real OpenAI, all of
which speak this same wire format.

Fixes a real latent bug found while extracting this: the old
custom_model_backend.py stored a FULL message dict (including its own
redundant "role" key) as the `content` value passed to
bot.db.append_agent_message() for assistant/tool turns — since
list_agent_messages() re-wraps whatever was stored as
`{"role": <db column>, "content": <stored value>}`, reloading a
multi-turn custom_model conversation on a later /ask call would double-
wrap those entries (e.g. `{"role":"assistant","content":{"role":"assistant",
"content":...,"tool_calls":...}}`) before resending them, which is not
valid chat-completions shape. This was never caught because every
existing test only ever calls .ask() once per session. Fixed here by
storing just the meaningful payload (no redundant "role") and having
`_to_wire_messages()` reconstruct the real wire shape on the way out —
tolerant of old rows that still have the stray inner "role" key, since
it's simply never read.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, ToolCall
from bot.backends.base import BackendError

logger = logging.getLogger("bot.agent_runtime.transports.openai_compatible")

API_MODE = "chat_completions"


def to_openai_tools(anthropic_tool_schemas: list[dict]) -> list[dict]:
    """Anthropic-shaped {name, description, input_schema} -> OpenAI's
    function-calling {"type": "function", "function": {...}} shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": schema.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for schema in anthropic_tool_schemas
    ]


def _to_wire_messages(history: list[dict]) -> list[dict]:
    wire: list[dict] = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content")
        if role == "assistant":
            payload = content if isinstance(content, dict) else {"content": content}
            msg: dict = {"role": "assistant", "content": payload.get("content")}
            if payload.get("tool_calls"):
                msg["tool_calls"] = payload["tool_calls"]
            wire.append(msg)
        elif role == "tool":
            payload = content if isinstance(content, dict) else {}
            wire.append({"role": "tool", "tool_call_id": payload.get("tool_call_id"), "content": payload.get("content")})
        else:
            wire.append({"role": role, "content": content if isinstance(content, str) else (content or {}).get("content", "")})
    return wire


class OpenAICompatibleTransport(ProviderTransport):
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def tool_result_messages(self, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [{"role": "tool", "content": {"tool_call_id": tc.id, "content": output}} for tc, output in results]

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
        wire_messages = _to_wire_messages(history)
        if system_prompt:
            wire_messages = [{"role": "system", "content": system_prompt}] + wire_messages

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": model, "messages": wire_messages, "max_tokens": max_tokens}
        if tool_schemas:
            payload["tools"] = to_openai_tools(tool_schemas)
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except httpx.TimeoutException as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) timed out after {timeout_s}s") from exc
            except httpx.HTTPStatusError as exc:
                raise BackendError(
                    f"openai-compatible transport ({self.base_url}) returned "
                    f"{exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            except Exception as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) error: {exc}") from exc

        usage = data.get("usage") or {}
        tokens = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)

        choices = data.get("choices") or []
        if not choices:
            raise BackendError(f"openai-compatible transport ({self.base_url}) returned no choices")
        message = choices[0].get("message") or {}
        tool_calls_raw = message.get("tool_calls") or []

        tool_calls = []
        for tc in tool_calls_raw:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.get("id"), name=fn.get("name", ""), arguments=args))

        assistant_payload: dict = {"content": message.get("content")}
        if tool_calls_raw:
            assistant_payload["tool_calls"] = tool_calls_raw

        return NormalizedResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            tokens=tokens or None,
            assistant_message={"role": "assistant", "content": assistant_payload},
        )
