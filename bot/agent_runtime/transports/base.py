"""The one abstraction that lets bot/backends/native_backend.py run a
single tool-calling loop against any LLM wire protocol, instead of
bot/backends/api_backend.py and custom_model_backend.py each hand-rolling
their own near-identical 20-iteration loop (which is what they did before
this module existed — see the Native Hermes-parity plan's Phase A).

A `ProviderTransport` owns everything protocol-specific: how a stored
history entry becomes a wire-format message, how tool schemas are shaped
for that protocol, how the actual HTTP/SDK call is made, and how the raw
response comes back out as one shared `NormalizedResponse` shape the loop
never has to special-case. Mirrors (in spirit, not code) the real Hermes
Agent's own `agent/transports/` package, which solves exactly this
problem for the same reason.

Stored history entries are the generic `{"role": str, "content": Any}`
shape `bot.db.list_agent_messages()`/`append_agent_message()` already use
— `content` is whatever a transport chooses to put there (a plain string,
a list of Anthropic content blocks, a small dict), never assumed to
already be wire-ready by the loop itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class NormalizedResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: Optional[int] = None
    # The stored-history entry (transport-native shape) representing this
    # assistant turn — appended to `messages` and persisted verbatim by
    # the loop, never constructed by it.
    assistant_message: dict = field(default_factory=dict)
    # Prompt-caching telemetry (AnthropicTransport only — see its own
    # send() for how these are populated; every other transport leaves
    # both None, which NativeAgentBackend.ask() treats as "nothing to
    # report" rather than "zero," since 0 is itself a meaningful value
    # for a transport that DOES support caching but had a full cache miss).
    cache_creation_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    # A short excerpt of this turn's real thinking-block text (AnthropicTransport
    # only, when adaptive thinking actually produced one) — never the reply
    # itself, purely an optional "what is it thinking" progress signal a
    # caller may surface (see native_backend.py's show_thinking_summary
    # config gate). None whenever no thinking block was present.
    thinking_summary: Optional[str] = None

    @property
    def stop(self) -> bool:
        return not self.tool_calls


class ProviderTransport:
    """One wire protocol. Concrete implementations: AnthropicTransport
    (anthropic.py, `anthropic_messages`) and OpenAICompatibleTransport
    (openai_compatible.py, `chat_completions` — OpenAI, OpenRouter,
    Ollama, LM Studio, vLLM, llama.cpp's server, etc.)."""

    # True on transports that actually serialize images into the wire
    # request (AnthropicTransport, OpenAICompatibleTransport,
    # ResponsesApiTransport as of Phase D of the native-parity plan) —
    # NativeAgentBackend.ask() checks this before ever calling
    # user_message(images=...), so a transport with no vision support
    # gets exactly today's plain-text call, no images param involved.
    supports_vision: bool = False
    # True only on AnthropicTransport (Phase C of the Claude API/Claude
    # Code parity plan) — PDF/text `document` content blocks have no
    # standardized equivalent across the OpenAI-compatible/Responses API
    # surface, unlike images, which every transport already serializes.
    supports_documents: bool = False

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
        """`history` is exactly what `bot.db.list_agent_messages()`
        returns (oldest-first `{"role","content"}` entries) plus whatever
        this turn's loop has appended so far in that same shape.
        `tool_schemas` is BotServer's own Anthropic-shaped schema list
        (`bot.agent_runtime.tools.all_tool_schemas()`, optionally
        filtered) — this method converts both into wire format, makes
        the real call, and returns one NormalizedResponse. `effort` is a
        canonical bot.effort.EFFORT_LADDER value (or None to use the
        transport's/provider's own default) — each concrete transport
        maps it onto whatever its own wire protocol actually supports
        (see bot/effort.py's per-backend mapping functions), silently
        doing nothing when the protocol has no equivalent rather than
        raising. Raises bot.backends.base.BackendError on any
        transport-level failure (timeout, HTTP error, malformed
        response)."""
        raise NotImplementedError

    def user_message(
        self, text: str, *,
        images: Optional[list[dict[str, str]]] = None, documents: Optional[list[dict[str, str]]] = None,
    ) -> dict:
        """A user turn (the initial prompt, or a mid-turn /steer
        injection) in this transport's stored-history shape. `images`/
        `documents`, when given, are bot.agent_runtime.vision.prepare()/
        prepare_documents()'s own output — a list of {"mime_type",
        "data_b64"} entries already validated and base64-encoded; a
        transport with no vision/document support simply ignores the
        corresponding parameter (ask() only ever passes one when the
        transport declares support — see NativeAgentBackend.ask())."""
        raise NotImplementedError

    def tool_result_messages(self, results: list[tuple[ToolCall, str]]) -> list[dict]:
        """One or more stored-history entries carrying this iteration's
        tool outputs back to the model — some protocols (Anthropic) batch
        every result from one turn into a single message, others (OpenAI-
        compatible) emit one message per tool call. Either way the loop
        just appends whatever this returns, in order, and persists each
        one via bot.db.append_agent_message(session_key, entry["role"], entry)."""
        raise NotImplementedError
