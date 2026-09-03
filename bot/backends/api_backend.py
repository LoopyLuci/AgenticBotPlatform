"""Direct Anthropic API backend — no dependency on any local app being
open, and the one backend family where BotServer itself runs a real
tool-use loop (shell/file/git tools — see bot/agent_runtime/tools.py)
instead of delegating to an external program's own agent loop. That's a
deliberate architectural choice, not an oversight: for cli/ui/hermes_cli/
hermes_gateway, the actual tool execution happens inside Claude Code,
Claude Desktop, or Hermes Agent's own process, which BotServer doesn't
control and can't safely intercept mid-turn — only here (and in
custom_model_backend.py, which shares this exact loop via
bot/backends/native_backend.py), where BotServer itself decides what
runs, can /approve, /deny, /steer, and mid-turn tool-call-granularity
control be fully real rather than simulated.

Conversation history is real too: unlike a single stateless request/
response, each session (see create_session()) accumulates a genuine
multi-turn transcript in bot/db.py's agent_messages table, keyed by the
same per-chat session_key bot/router.py's chat_sessions already track —
so /new, /resume, and /sessions all work for an api-backend bot exactly
like they do for ui/hermes_gateway, not as separate machinery.

This class is now a thin wrapper around bot/backends/native_backend.py's
NativeAgentBackend (the actual loop, shared with custom_model_backend.py)
plugged in with an AnthropicTransport — kept as its own class, with its
own name/constructor, so bot/router.py and every existing caller need no
changes.
"""

from __future__ import annotations

from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.backends.base import Backend, BackendResult
from bot.backends.native_backend import NativeAgentBackend


class ApiBackend(Backend):
    name = "api"

    def __init__(self, model: str = "claude-sonnet-5", max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens
        self._inner = NativeAgentBackend(
            AnthropicTransport(), model=model, max_tokens=max_tokens,
            session_prefix="api", name="api",
        )

    async def create_session(self) -> str:
        return await self._inner.create_session()

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 30) -> BackendResult:
        return await self._inner.ask(prompt, context=context, timeout_s=timeout_s)
