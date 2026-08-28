"""Direct Anthropic API backend — no dependency on any local app being
open, and the one backend where BotServer itself runs a real tool-use
loop (shell/file/git tools — see bot/agent_runtime/tools.py) instead of
delegating to an external program's own agent loop. That's a deliberate
architectural choice, not an oversight: for cli/ui/hermes_cli/
hermes_gateway, the actual tool execution happens inside Claude Code,
Claude Desktop, or Hermes Agent's own process, which BotServer doesn't
control and can't safely intercept mid-turn — only here, where BotServer
itself decides what runs, can /approve, /deny, /steer, and mid-turn
tool-call-granularity control be fully real rather than simulated.

Conversation history is real too: unlike a single stateless request/
response, each session (see create_session()) accumulates a genuine
multi-turn transcript in bot/db.py's agent_messages table, keyed by the
same per-chat session_key bot/router.py's chat_sessions already track —
so /new, /resume, and /sessions all work for an api-backend bot exactly
like they do for ui/hermes_gateway, not as separate machinery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.api")

MAX_TOOL_ITERATIONS = 20


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

    async def create_session(self) -> str:
        """Unlike ui/hermes_gateway there's no external app session to
        open — this session key only ever means "a fresh row in
        agent_messages," so it's just a random id. See db.link_chat_session()
        (called by Router.create_session()) for where this actually gets
        wired up as this chat's active session."""
        return f"api-{uuid.uuid4().hex[:16]}"

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 30) -> BackendResult:
        from bot import db
        from bot.agent_runtime import approval as agent_approval
        from bot.agent_runtime import tool_loop
        from bot.agent_runtime import tools as agent_tools

        context = context or {}
        client = self._get_client()

        session_key = context.get("desktop_session_key")
        lazily_created = session_key is None
        if lazily_created:
            session_key = await self.create_session()

        instance_id = context.get("instance_id")
        chat_id = context.get("chat_id")
        workspace = agent_tools.resolve_workspace(instance_id or 0, context.get("cwd"))
        steer_queue = context.get("steer_queue")
        notify = context.get("approval_notify")
        progress = context.get("progress_notify")

        messages = db.list_agent_messages(session_key)
        messages.append({"role": "user", "content": prompt})
        db.append_agent_message(session_key, "user", prompt)

        system_prompt = _build_system_prompt(instance_id)

        total_tokens = 0
        for _ in range(MAX_TOOL_ITERATIONS):
            if steer_queue is not None:
                steered = []
                while not steer_queue.empty():
                    steered.append(steer_queue.get_nowait())
                if steered:
                    steer_text = "[The user sent this mid-turn — take it into account:]\n" + "\n".join(steered)
                    messages.append({"role": "user", "content": steer_text})
                    db.append_agent_message(session_key, "user", steer_text)

            try:
                create_kwargs = dict(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    tools=agent_tools.TOOL_SCHEMAS,
                    messages=messages,
                )
                if system_prompt:
                    create_kwargs["system"] = system_prompt
                resp = await asyncio.wait_for(client.messages.create(**create_kwargs), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise BackendError(f"api backend timed out after {timeout_s}s") from exc
            except Exception as exc:
                raise BackendError(f"api backend error: {exc}") from exc

            if resp.usage:
                total_tokens += (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)

            assistant_blocks = _serialize_blocks(resp.content)
            messages.append({"role": "assistant", "content": assistant_blocks})
            db.append_agent_message(session_key, "assistant", assistant_blocks)

            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                raw = {"total_tokens": total_tokens}
                if lazily_created:
                    raw["desktop_session_key"] = session_key
                return BackendResult(text=text, tokens=total_tokens or None, raw=raw)

            tool_results = []
            for tu in tool_uses:
                if progress is not None:
                    try:
                        await progress(_progress_line(tu.name, tu.input))
                    except Exception:
                        logger.exception("progress_notify callback failed")
                output = await tool_loop.run_one_tool(
                    tu.name, tu.input, workspace=workspace,
                    instance_id=instance_id, chat_id=chat_id, session_key=session_key,
                    notify=notify, agent_tools=agent_tools, agent_approval=agent_approval,
                )
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output})

            messages.append({"role": "user", "content": tool_results})
            db.append_agent_message(session_key, "user", tool_results)

        raise BackendError(f"agent loop exceeded {MAX_TOOL_ITERATIONS} tool calls without a final answer")


def _progress_line(tool_name: str, tool_input: dict) -> str:
    if tool_name == "run_shell":
        return f"🔧 Running: {tool_input.get('command', '')[:200]}"
    if tool_name in ("read_file", "write_file"):
        return f"🔧 {tool_name}: {tool_input.get('path', '')}"
    if tool_name == "list_dir":
        return f"🔧 Listing: {tool_input.get('path', '.')}"
    if tool_name in ("git_status", "git_diff"):
        return f"🔧 {tool_name.replace('_', ' ')}"
    if tool_name == "save_memory":
        return "🔧 Saving a memory…"
    if tool_name == "read_skill":
        return f"🔧 Loading skill: {tool_input.get('name', '')}"
    return f"🔧 {tool_name}"


def _build_system_prompt(instance_id) -> str:
    if instance_id is None:
        return ""
    from bot import memory as bot_memory
    from bot import skills as bot_skills

    parts = [p for p in (bot_memory.approved_summary(instance_id), bot_skills.summary(instance_id)) if p]
    return "\n\n".join(parts)


def _serialize_blocks(content) -> list[dict]:
    out = []
    for block in content:
        btype = getattr(block, "type", "")
        if btype == "text":
            out.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        else:
            out.append({"type": btype})
    return out
