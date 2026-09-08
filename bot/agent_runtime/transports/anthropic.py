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
        # isn't surfaced in the plain-text chat reply (no bot-server
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
        effort: Optional[str] = None,
    ) -> NormalizedResponse:
        client = self._get_client()
        # Anthropic's stored-history shape IS the wire shape already
        # (each entry is exactly {"role","content"} with content already
        # either a plain string or a list of content blocks) — no
        # conversion needed, unlike the OpenAI-compatible transport.
        create_kwargs = dict(model=model, max_tokens=max_tokens, messages=history)
        caching_cfg = _prompt_caching_config()
        caching_enabled = caching_cfg.get("enabled", DEFAULT_PROMPT_CACHING_ENABLED)
        cache_control = _cache_control(caching_cfg.get("ttl", DEFAULT_PROMPT_CACHING_TTL)) if caching_enabled else None
        if tool_schemas:
            if cache_control is not None:
                # Breakpoint on the LAST tool schema only — Anthropic
                # caches everything up to and including a breakpoint, so
                # one entry at the end covers the whole (stable-for-the-
                # session) tools array. Copy rather than mutate: these
                # dicts are the same shared objects agent_tools.all_tool_schemas()
                # returns on every call (TOOL_SCHEMAS is a module-level
                # list) — mutating one in place would leak cache_control
                # into every other transport/call that reuses it.
                tools_payload = list(tool_schemas)
                tools_payload[-1] = {**tools_payload[-1], "cache_control": cache_control}
                create_kwargs["tools"] = tools_payload
            else:
                create_kwargs["tools"] = tool_schemas
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
        try:
            resp = await asyncio.wait_for(client.messages.create(**create_kwargs), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise BackendError(f"anthropic transport timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise BackendError(f"anthropic transport error: {exc}") from exc

        tokens = None
        cache_creation_tokens = None
        cache_read_tokens = None
        if resp.usage:
            tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
            cache_creation_tokens = getattr(resp.usage, "cache_creation_input_tokens", None)
            cache_read_tokens = getattr(resp.usage, "cache_read_input_tokens", None)

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
        else:
            out.append({"type": btype})
    return out
