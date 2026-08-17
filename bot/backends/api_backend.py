"""Direct Anthropic API backend — no dependency on any local app being open."""

from __future__ import annotations

import asyncio
import logging
import os

from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.api")


class ApiBackend(Backend):
    name = "api"

    def __init__(self, model: str = "claude-sonnet-5", max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise BackendError("ANTHROPIC_API_KEY is not set")
            self._client = AsyncAnthropic(api_key=api_key)
        return self._client

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 30) -> BackendResult:
        client = self._get_client()
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise BackendError(f"api backend timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise BackendError(f"api backend error: {exc}") from exc

        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0) if resp.usage else None
        return BackendResult(text=text, tokens=tokens, raw=resp)
