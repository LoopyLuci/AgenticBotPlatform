"""Generic OpenAI-compatible-endpoint backend — the "any model from
anywhere" backend. Talks to whatever base_url a named provider
(bot/providers.py, config/providers.yaml) points at via plain HTTP
(httpx), speaking the OpenAI chat-completions wire format that Ollama,
LM Studio, vLLM, llama.cpp's server, OpenRouter, and real OpenAI all
implement.

Reuses BotServer's own tool-use loop (bot/agent_runtime/tools.py's
execute_tool()/DANGEROUS_TOOLS, approval.py, checkpoints.py, via the
shared bot/agent_runtime/tool_loop.py helper) exactly like
bot/backends/api_backend.py does for Anthropic — a local model gets real
shell/file/git tool access, not just a passthrough chat. See
bot/backends/_openai_tool_adapter.py for the one translation layer this
needs (Anthropic-shaped tool schemas -> OpenAI function-calling shape).

Message history is stored in agent_messages under its own "custom-*"
session-key namespace, in OpenAI's own message shape — a disjoint
namespace from the api backend's "api-*" sessions (which store
Anthropic-shaped blocks), so there's no cross-format leakage even though
both share the same table.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

import httpx

from bot.backends._openai_tool_adapter import to_openai_tools
from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.custom_model")

MAX_TOOL_ITERATIONS = 20


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

    async def create_session(self) -> str:
        """Unlike ui/hermes_gateway there's no external app session to
        open — this key only ever means "a fresh row in agent_messages,"
        same as ApiBackend's own create_session()."""
        return f"custom-{uuid.uuid4().hex[:16]}"

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 30) -> BackendResult:
        from bot import db
        from bot.agent_runtime import approval as agent_approval
        from bot.agent_runtime import tool_loop
        from bot.agent_runtime import tools as agent_tools

        context = context or {}
        session_key = context.get("desktop_session_key")
        lazily_created = session_key is None
        if lazily_created:
            session_key = await self.create_session()

        instance_id = context.get("instance_id")
        chat_id = context.get("chat_id")
        workspace = agent_tools.resolve_workspace(instance_id or 0, context.get("cwd"))
        notify = context.get("approval_notify")
        progress = context.get("progress_notify")

        messages = db.list_agent_messages(session_key)
        messages.append({"role": "user", "content": prompt})
        db.append_agent_message(session_key, "user", prompt)

        openai_tools = to_openai_tools(agent_tools.all_tool_schemas())
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        total_tokens = 0
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            for _ in range(MAX_TOOL_ITERATIONS):
                payload = {
                    "model": self.model_id,
                    "messages": messages,
                    "tools": openai_tools,
                    "tool_choice": "auto",
                    "max_tokens": self.max_tokens,
                }
                try:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.TimeoutException as exc:
                    raise BackendError(
                        f"custom_model backend ({self.provider_name}) timed out after {timeout_s}s"
                    ) from exc
                except httpx.HTTPStatusError as exc:
                    raise BackendError(
                        f"custom_model backend ({self.provider_name}) returned "
                        f"{exc.response.status_code}: {exc.response.text[:500]}"
                    ) from exc
                except Exception as exc:
                    raise BackendError(f"custom_model backend ({self.provider_name}) error: {exc}") from exc

                usage = data.get("usage") or {}
                total_tokens += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)

                choices = data.get("choices") or []
                if not choices:
                    raise BackendError(f"custom_model backend ({self.provider_name}) returned no choices")
                message = choices[0].get("message") or {}
                tool_calls = message.get("tool_calls") or []

                assistant_msg: dict = {"role": "assistant", "content": message.get("content")}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)
                db.append_agent_message(session_key, "assistant", assistant_msg)

                if not tool_calls:
                    text = message.get("content") or ""
                    raw = {"total_tokens": total_tokens}
                    if lazily_created:
                        raw["desktop_session_key"] = session_key
                    return BackendResult(text=text, tokens=total_tokens or None, raw=raw)

                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    tool_name = fn.get("name", "")
                    try:
                        tool_input = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tool_input = {}
                    if progress is not None:
                        try:
                            await progress(f"🔧 {tool_name}")
                        except Exception:
                            logger.exception("progress_notify callback failed")
                    output = await tool_loop.run_one_tool(
                        tool_name, tool_input, workspace=workspace,
                        instance_id=instance_id, chat_id=chat_id, session_key=session_key,
                        notify=notify, agent_tools=agent_tools, agent_approval=agent_approval,
                    )
                    tool_msg = {"role": "tool", "tool_call_id": tc.get("id"), "content": output}
                    messages.append(tool_msg)
                    db.append_agent_message(session_key, "tool", tool_msg)

        raise BackendError(f"agent loop exceeded {MAX_TOOL_ITERATIONS} tool calls without a final answer")
