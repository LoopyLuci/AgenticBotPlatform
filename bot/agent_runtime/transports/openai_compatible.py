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
import uuid
from typing import Optional

import httpx

from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, StreamEvent, ToolCall
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
        elif isinstance(content, list):
            # A multimodal user turn (see user_message(images=...)) —
            # already wire-ready chat-completions content blocks, passed
            # through verbatim.
            wire.append({"role": role, "content": content})
        else:
            wire.append({"role": role, "content": content if isinstance(content, str) else (content or {}).get("content", "")})
    return wire


class OpenAICompatibleTransport(ProviderTransport):
    def __init__(self, base_url: str, api_key: Optional[str] = None, catalog_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Used only to look up a per-vendor quirk profile (see
        # bot/agent_runtime/provider_quirks.py) — None is fine, it just
        # means no profile matches and every request behaves exactly as
        # it did before quirk handling existed.
        self.catalog_id = catalog_id
        # Generated once per transport instance (which itself lives for
        # one bot instance's/subagent's whole conversation — see
        # CustomModelBackend/NativeAgentBackend) rather than per request,
        # matching OpenCode's own "stable per-conversation id" contract
        # for its x-opencode-session header. Harmless to generate even
        # for providers that never use it — see provider_quirks.py.
        self._session_id = uuid.uuid4().hex

    supports_vision = True
    # No documents support — `document` blocks are an Anthropic-specific
    # shape with no standardized OpenAI-compatible equivalent (see the
    # Claude API/Claude Code parity plan's Phase C). `documents` is
    # accepted-and-ignored here purely so every transport's user_message()
    # keeps the same call signature — NativeAgentBackend.ask() never
    # actually passes it when supports_documents is False.
    supports_documents = False

    def user_message(
        self, text: str, *,
        images: Optional[list[dict[str, str]]] = None, documents: Optional[list[dict[str, str]]] = None,
    ) -> dict:
        if not images:
            return {"role": "user", "content": text}
        blocks: list[dict] = [{"type": "text", "text": text}]
        for img in images:
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{img['data_b64']}"}})
        return {"role": "user", "content": blocks}

    def prune_tool_results(self, history: list[dict], keep: int) -> tuple[list[dict], int]:
        from bot.agent_runtime import context_window

        holders = [i for i, e in enumerate(history) if e.get("role") == "tool"]
        old = set(holders[:-keep] if keep > 0 else holders)
        out, cleared = [], 0
        for i, entry in enumerate(history):
            payload = entry.get("content")
            if i in old and isinstance(payload, dict) and isinstance(payload.get("content"), str) \
                    and not payload["content"].startswith("[Output cleared"):
                entry = {**entry, "content": {**payload, "content": context_window.placeholder(len(payload["content"]), payload["content"])}}
                cleared += 1
            out.append(entry)
        return out, cleared

    def dangling_tool_calls(self, history: list[dict]) -> list[ToolCall]:
        if not history or history[-1].get("role") != "assistant":
            return []
        payload = history[-1].get("content")
        calls = payload.get("tool_calls") if isinstance(payload, dict) else None
        return [ToolCall(id=c.get("id"), name=(c.get("function") or {}).get("name", ""), arguments={})
                for c in (calls or []) if c.get("id")]

    def tool_result_messages(self, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [{"role": "tool", "content": {"tool_call_id": tc.id, "content": output}} for tc, output in results]

    def _build_request(
        self, *, model: str, history: list[dict], tool_schemas: list[dict], max_tokens: int,
        system_prompt: Optional[str], effort: Optional[str],
    ) -> tuple[dict, dict]:
        """(payload, headers) — shared by send() and send_stream()."""
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
        # Best-effort only: "reasoning_effort" is the real parameter name
        # OpenAI's o-series Chat Completions API accepts, and several
        # OpenRouter-routed models pass it straight through — but this is
        # genuinely provider/model-dependent, not a guarantee. An endpoint
        # that doesn't recognize the field is expected to just ignore it
        # (safe), not error — never asserted or verified for every
        # possible custom_model provider.
        from bot import effort as effort_module

        openai_effort = effort_module.to_openai_reasoning_effort(effort)
        if openai_effort is not None:
            payload["reasoning_effort"] = openai_effort

        # Real per-vendor wire quirks (confirmed against Hermes Agent's
        # own per-vendor adapters) — a provider with no matching profile
        # is completely untouched by this call.
        from bot.agent_runtime import provider_quirks

        quirk_profile = provider_quirks.profile_for(self.catalog_id, self.base_url)
        provider_quirks.apply(payload, profile=quirk_profile, effort=effort)
        headers.update(provider_quirks.extra_headers(profile=quirk_profile, session_id=self._session_id))
        return payload, headers

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
        payload, headers = self._build_request(
            model=model, history=history, tool_schemas=tool_schemas, max_tokens=max_tokens,
            system_prompt=system_prompt, effort=effort,
        )
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                self.capture(resp)
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

        return _normalize(data, self.base_url)

    supports_streaming = True

    async def send_stream(
        self,
        *,
        on_event,
        model: str,
        history: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
        timeout_s: float,
        system_prompt: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> NormalizedResponse:
        """Server-sent-events variant of send(): reply text goes to `on_event` as it
        arrives and the deltas are assembled into the same NormalizedResponse."""
        payload, headers = self._build_request(
            model=model, history=history, tool_schemas=tool_schemas, max_tokens=max_tokens,
            system_prompt=system_prompt, effort=effort,
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                try:
                    return await self._consume_stream(client, url, payload, headers, on_event)
                except _RetryWithoutUsage:
                    # A few OpenAI-compatible servers reject stream_options; usage is optional.
                    payload.pop("stream_options", None)
                    return await self._consume_stream(client, url, payload, headers, on_event)
            except httpx.TimeoutException as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) timed out after {timeout_s}s") from exc
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) error: {exc}") from exc

    async def _consume_stream(self, client, url, payload, headers, on_event) -> NormalizedResponse:
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        usage: dict = {}
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            self.capture(resp)
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                if resp.status_code in (400, 422) and "stream_options" in body and "stream_options" in payload:
                    raise _RetryWithoutUsage()
                raise BackendError(
                    f"openai-compatible transport ({self.base_url}) returned {resp.status_code}: {body[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                        await on_event(StreamEvent("text", delta["content"]))
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        message: dict = {"content": "".join(text_parts) or None}
        if calls:
            message["tool_calls"] = [
                {"id": c["id"] or f"call_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for i, c in sorted(calls.items())
            ]
        return _normalize({"usage": usage, "choices": [{"message": message}]}, self.base_url)


class _RetryWithoutUsage(Exception):
    pass


def _normalize(data: dict, base_url: str) -> NormalizedResponse:
    usage = data.get("usage") or {}
    tokens = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)

    choices = data.get("choices") or []
    if not choices:
        raise BackendError(f"openai-compatible transport ({base_url}) returned no choices")
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
        input_tokens=usage.get("prompt_tokens") or None,
        assistant_message={"role": "assistant", "content": assistant_payload},
    )
