"""Generic OpenAI-compatible-endpoint backend — the "any model from
anywhere" backend. Talks to whatever base_url a named provider
(bot/providers.py, config/providers.yaml) points at via plain HTTP,
speaking the OpenAI chat-completions wire format that Ollama, LM Studio,
vLLM, llama.cpp's server, OpenRouter, and real OpenAI all implement.

Reuses BotServer's own tool-use loop (bot/agent_runtime/tools.py's
execute_tool()/DANGEROUS_TOOLS, approval.py, checkpoints.py) exactly like
bot/backends/api_backend.py does for Anthropic — a local model gets real
shell/file/git tool access, not just a passthrough chat. Both backends
now share one loop implementation, bot/backends/native_backend.py's
NativeAgentBackend, plugged in here with an OpenAICompatibleTransport
(bot/agent_runtime/transports/openai_compatible.py) — this class is kept
as its own name/constructor so bot/router.py needs no changes.

Message history is stored in agent_messages under its own "custom-*"
session-key namespace, in OpenAI's own message shape — a disjoint
namespace from the api backend's "api-*" sessions (which store
Anthropic-shaped blocks), so there's no cross-format leakage even though
both share the same table.
"""

from __future__ import annotations

from typing import Optional

from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport
from bot.backends.base import Backend, BackendResult
from bot.backends.native_backend import NativeAgentBackend


class CustomModelBackend(Backend):
    name = "custom_model"

    def __init__(
        self,
        provider_name: str,
        model_id: str,
        base_url: str,
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
    ):
        self.provider_name = provider_name
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self._inner = NativeAgentBackend(
            OpenAICompatibleTransport(base_url=self.base_url, api_key=api_key),
            model=model_id, max_tokens=max_tokens,
            session_prefix="custom", name="custom_model",
        )

    async def create_session(self) -> str:
        return await self._inner.create_session()

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 30) -> BackendResult:
        return await self._inner.ask(prompt, context=context, timeout_s=timeout_s)
