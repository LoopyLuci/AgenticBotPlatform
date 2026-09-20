"""Anthropic Messages API transport — extracted from what
bot/backends/api_backend.py used to do inline. Anthropic's own wire shape
already matches AgenticBotPlatform's TOOL_SCHEMAS (`{name, description,
input_schema}`) and its own stored-history convention (a plain string
for a simple text turn, a list of content blocks for anything richer),
so this transport does the least translation work of the two — its main
job is turning SDK response objects into the shared NormalizedResponse
shape.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, StreamEvent, ToolCall
from bot.backends.base import BackendError

API_MODE = "anthropic_messages"

DEFAULT_PROMPT_CACHING_ENABLED = True
DEFAULT_PROMPT_CACHING_TTL = "5m"
# A progress-line excerpt, not the full trace — real thinking blocks can
# run to thousands of characters, which would flood a Telegram status
# message far past anything readable as a live "what is it thinking" cue.
THINKING_SUMMARY_MAX_CHARS = 300


def _prompt_caching_config() -> dict:
    from bot.config import config

    return config.current.get("native_agent", {}).get("prompt_caching", {}) or {}


def _cache_control(ttl: str) -> dict:
    # Anthropic's own default TTL (5 minutes) is the bare {"type":
    # "ephemeral"} shape — "ttl" is only sent at all for the explicit 1h
    # opt-in, matching the API's own documented usage.
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


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

    supports_vision = True
    supports_documents = True

    def user_message(
        self, text: str, *,
        images: Optional[list[dict[str, str]]] = None, documents: Optional[list[dict[str, str]]] = None,
    ) -> dict:
        if not images and not documents:
            return {"role": "user", "content": text}
        blocks: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": img["mime_type"], "data": img["data_b64"]}}
            for img in (images or [])
        ]
        # citations on by default — free correctness/traceability once a
        # document is attached at all; see the Claude API/Claude Code
        # parity plan's Phase C for why the raw citation data itself
        # isn't surfaced in the plain-text chat reply (no agentic-bot-platform
        # platform renders structured citation markers today).
        blocks.extend(
            {
                "type": "document",
                "source": {"type": "base64", "media_type": doc["mime_type"], "data": doc["data_b64"]},
                "citations": {"enabled": True},
            }
            for doc in (documents or [])
        )
        blocks.append({"type": "text", "text": text})
        return {"role": "user", "content": blocks}

    def prune_tool_results(self, history: list[dict], keep: int) -> tuple[list[dict], int]:
        from bot.agent_runtime import context_window

        holders = [i for i, e in enumerate(history) if isinstance(e.get("content"), list)
                   and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in e["content"])]
        old = set(holders[:-keep] if keep > 0 else holders)
        out, cleared = [], 0
        for i, entry in enumerate(history):
            if i not in old:
                out.append(entry)
                continue
            blocks = []
            for b in entry["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    body = b.get("content")
                    text = body if isinstance(body, str) else json.dumps(body, default=str)
                    if not text.startswith("[Output cleared"):
                        b = {**b, "content": context_window.placeholder(len(text), text)}
                        cleared += 1
                blocks.append(b)
            out.append({**entry, "content": blocks})
        return out, cleared

    def dangling_tool_calls(self, history: list[dict]) -> list[ToolCall]:
        if not history or history[-1].get("role") != "assistant":
            return []
        content = history[-1].get("content")
        if not isinstance(content, list):
            return []
        return [ToolCall(id=b["id"], name=b.get("name", ""), arguments=b.get("input") or {})
                for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]

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

    def _build_kwargs(
        self, *, model: str, history: list[dict], tool_schemas: list[dict], max_tokens: int,
        system_prompt: Optional[str], effort: Optional[str],
    ) -> dict:
        """Everything about the request except how it is sent, so send() and
        send_stream() cannot drift apart."""
        # Anthropic's stored-history shape IS the wire shape already
        # (each entry is exactly {"role","content"} with content already
        # either a plain string or a list of content blocks) — no
        # conversion needed, unlike the OpenAI-compatible transport.
        caching_cfg = _prompt_caching_config()
        caching_enabled = caching_cfg.get("enabled", DEFAULT_PROMPT_CACHING_ENABLED)
        cache_control = _cache_control(caching_cfg.get("ttl", DEFAULT_PROMPT_CACHING_TTL)) if caching_enabled else None
        # A breakpoint on the LAST message caches the whole conversation so far; each turn it moves forward, so
        # the next call re-reads everything up to here from the cache instead of paying for it again.
        messages = _with_message_breakpoint(history, cache_control) if cache_control is not None else history
        create_kwargs = dict(model=model, max_tokens=max_tokens, messages=messages)

        # Anthropic server tools (web_search/web_fetch/code_execution/
        # tool_search — Phase D of the Claude API/Claude Code parity
        # plan) — appended alongside AgenticBotPlatform's own client tool schemas.
        # Anthropic executes these itself and returns results as a
        # "server_tool_use" block (structurally distinct from "tool_use"),
        # so they never reach tool_loop.run_one_tool() by construction —
        # no dispatch branch needed anywhere in native_backend.py.
        from bot.agent_runtime import anthropic_server_tools

        # strict: true (Phase H of the Claude API/Claude Code parity
        # plan) tightens schema conformance for free on AgenticBotPlatform's own
        # tool definitions only — never on Anthropic's own server tool
        # entries (web_search/web_fetch/... below, or mcp_toolset),
        # which the tool-reference page's own strict-support scoping
        # doesn't cover the same way a plain client tool does. Copy
        # rather than mutate: these dicts are the same shared objects
        # agent_tools.all_tool_schemas() returns on every call.
        all_tools = [{**schema, "strict": True} for schema in tool_schemas] if tool_schemas else []
        all_tools.extend(anthropic_server_tools.enabled_tool_entries())

        if all_tools:
            if cache_control is not None:
                # Breakpoint on the LAST tool entry only — Anthropic
                # caches everything up to and including a breakpoint, so
                # one entry at the end covers the whole (stable-for-the-
                # session) tools array. Copy rather than mutate: the
                # AgenticBotPlatform-schema dicts here are the same shared objects
                # agent_tools.all_tool_schemas() returns on every call
                # (TOOL_SCHEMAS is a module-level list) — mutating one in
                # place would leak cache_control into every other
                # transport/call that reuses it.
                tools_payload = list(all_tools)
                tools_payload[-1] = {**tools_payload[-1], "cache_control": cache_control}
                create_kwargs["tools"] = tools_payload
            else:
                create_kwargs["tools"] = all_tools
        if system_prompt:
            # A list-of-blocks system param is required to attach
            # cache_control at all (a bare string has nowhere to put it);
            # falls back to the plain string today's callers already send
            # when caching is off, so nothing changes for them.
            if cache_control is not None:
                create_kwargs["system"] = [{"type": "text", "text": system_prompt, "cache_control": cache_control}]
            else:
                create_kwargs["system"] = system_prompt
        # Confirmed live against platform.claude.com/docs (matching this
        # deployment's real model family — claude-sonnet-5, claude-opus-5,
        # etc.): output_config.effort is the current, correct control —
        # the older thinking.budget_tokens path is deprecated/rejected on
        # these exact models. Omitting the field entirely (effort=None or
        # an unrecognized level) preserves the API's own "high" default.
        from bot import effort as effort_module

        anthropic_effort = effort_module.to_anthropic(effort)
        if anthropic_effort is not None:
            create_kwargs["output_config"] = {"effort": anthropic_effort}
        return create_kwargs

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
        client = self._get_client()
        create_kwargs = self._build_kwargs(
            model=model, history=history, tool_schemas=tool_schemas, max_tokens=max_tokens,
            system_prompt=system_prompt, effort=effort,
        )
        try:
            resp = await asyncio.wait_for(client.messages.create(**create_kwargs), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise BackendError(f"anthropic transport timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise BackendError(f"anthropic transport error: {exc}") from exc
        return _normalize(resp)

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
        """Same request as send(), but reply text is handed to `on_event` as it
        arrives. The returned response is the SDK's assembled final message, so
        history, tool calls and usage are exactly what send() would have produced."""
        client = self._get_client()
        create_kwargs = self._build_kwargs(
            model=model, history=history, tool_schemas=tool_schemas, max_tokens=max_tokens,
            system_prompt=system_prompt, effort=effort,
        )

        async def _run():
            async with client.messages.stream(**create_kwargs) as stream:
                async for text in stream.text_stream:
                    if text:
                        await on_event(StreamEvent("text", text))
                return await stream.get_final_message()

        try:
            resp = await asyncio.wait_for(_run(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise BackendError(f"anthropic transport timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise BackendError(f"anthropic transport error: {exc}") from exc
        return _normalize(resp)

    async def count_tokens(
        self, *, model: str, history: list[dict], tool_schemas: Optional[list[dict]] = None,
        system_prompt: Optional[str] = None,
    ) -> int:
        """Exact, free (no completion generated) input-token count via the
        real `client.messages.count_tokens()` endpoint (Phase F of the
        Claude API/Claude Code parity plan) — tightens
        bot.swarm_budget's own cost estimate from a worst-case heuristic
        into a measured number for Anthropic-backed dispatches
        specifically. `history` is the same stored-history shape send()
        already accepts."""
        client = self._get_client()
        kwargs: dict = {"model": model, "messages": history}
        if tool_schemas:
            kwargs["tools"] = tool_schemas
        if system_prompt:
            kwargs["system"] = system_prompt
        try:
            result = await client.messages.count_tokens(**kwargs)
        except Exception as exc:
            raise BackendError(f"anthropic count_tokens error: {exc}") from exc
        return result.input_tokens


def _with_message_breakpoint(history: list[dict], cache_control: dict) -> list[dict]:
    """`history` with cache_control on the final block of the final message (copied, never mutated)."""
    if not history:
        return history
    last = history[-1]
    content = last.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content, "cache_control": cache_control}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict) \
            and content[-1].get("type") not in ("thinking", "redacted_thinking"):
        blocks = [*content[:-1], {**content[-1], "cache_control": cache_control}]
    else:
        return history
    return [*history[:-1], {**last, "content": blocks}]


def _normalize(resp) -> NormalizedResponse:
    tokens = None
    cache_creation_tokens = None
    cache_read_tokens = None
    input_tokens = None
    if resp.usage:
        tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
        cache_creation_tokens = getattr(resp.usage, "cache_creation_input_tokens", None)
        cache_read_tokens = getattr(resp.usage, "cache_read_input_tokens", None)
        input_tokens = ((resp.usage.input_tokens or 0) + (cache_creation_tokens or 0) + (cache_read_tokens or 0)) or None

    assistant_blocks = _serialize_blocks(resp.content)
    tool_calls = [
        ToolCall(id=b.id, name=b.name, arguments=b.input)
        for b in resp.content
        if getattr(b, "type", "") == "tool_use"
    ]
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    thinking_text = "".join(b.thinking for b in resp.content if getattr(b, "type", "") == "thinking")
    thinking_summary = thinking_text[:THINKING_SUMMARY_MAX_CHARS] if thinking_text else None
    return NormalizedResponse(
        text=text,
        tool_calls=tool_calls if resp.stop_reason == "tool_use" else [],
        tokens=tokens,
        assistant_message={"role": "assistant", "content": assistant_blocks},
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        thinking_summary=thinking_summary,
        input_tokens=input_tokens,
    )


def _serialize_blocks(content) -> list[dict]:
    out = []
    for block in content:
        btype = getattr(block, "type", "")
        if btype == "text":
            out.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        elif btype == "thinking":
            # Real fields confirmed against the installed anthropic SDK
            # (anthropic.types.ThinkingBlock: signature, thinking, type).
            # Both must round-trip verbatim — the API validates a replayed
            # thinking block's signature and rejects a mismatched/missing
            # one, so the previous `{"type": "thinking"}` stub silently
            # broke adaptive thinking + tool use across a second turn.
            out.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
        elif btype == "redacted_thinking":
            # anthropic.types.RedactedThinkingBlock: data, type.
            out.append({"type": "redacted_thinking", "data": block.data})
        elif hasattr(block, "model_dump"):
            # Covers server_tool_use and every *_tool_result block
            # (web_search_tool_result, web_fetch_tool_result,
            # code_execution_tool_result, tool_search_tool_result — Phase D
            # of the Claude API/Claude Code parity plan) without hand-
            # enumerating each one's own rich, evolving shape (encrypted
            # search-result content, citations, etc.) — these must round-
            # trip byte-for-byte on a later turn (the API rejects a
            # multi-turn request whose encrypted_content was dropped or
            # modified), so a full pydantic dump is the correct fix here,
            # not a hand-picked field subset the way thinking/tool_use
            # above are (those two have small, stable, well-known shapes).
            out.append(block.model_dump(mode="json", exclude_none=True))
        else:
            out.append({"type": btype})
    return out
