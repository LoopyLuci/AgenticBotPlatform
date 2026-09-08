"""The one tool-calling loop shared by every provider-agnostic backend —
replaces what used to be two separately hand-written, near-identical
loops in api_backend.py (Anthropic) and custom_model_backend.py (OpenAI-
compatible). See bot/agent_runtime/transports/base.py's module docstring
for why a Transport exists at all; this class is everything that stays
the same regardless of which one is plugged in: history loading/
persistence, steer-queue draining, the system prompt (memory+skills),
per-tool progress notifications, and the actual approval/execute/
checkpoint round trip via bot.agent_runtime.tool_loop.run_one_tool().

bot/backends/api_backend.py and custom_model_backend.py are now thin
compatibility shims around this class — their public constructors and
`Backend` contract are unchanged, so bot/router.py needs no changes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from bot.agent_runtime.transports.base import ProviderTransport
from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.native")

MAX_TOOL_ITERATIONS = 20


class NativeAgentBackend(Backend):
    """`session_prefix` keeps each caller's session-key namespace
    disjoint (e.g. "api-" vs "custom-") even though every native backend
    shares this one loop and the same agent_messages table — matching
    the existing convention those two backends already established."""

    def __init__(
        self,
        transport: ProviderTransport,
        model: str,
        *,
        max_tokens: int = 4096,
        session_prefix: str = "native",
        name: str = "native_agent",
    ):
        self.transport = transport
        self.model = model
        self.max_tokens = max_tokens
        self.session_prefix = session_prefix
        self.name = name

    async def create_session(self) -> str:
        return f"{self.session_prefix}-{uuid.uuid4().hex[:16]}"

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 30) -> BackendResult:
        from bot import db
        from bot.agent_runtime import approval as agent_approval
        from bot.agent_runtime import estop
        from bot.agent_runtime import tool_loop
        from bot.agent_runtime import tools as agent_tools

        # Checked once, at the very start of a new turn — never mid-turn:
        # an already-running ask() finishes rather than being killed.
        estop.check()

        context = context or {}
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
        # Optional per-call tool restriction — used by ephemeral sub-agents
        # (bot/agent_runtime/subagents.py) so a role="leaf" child can't
        # reach spawn_subagent/update_agent_config/etc. Absent for every
        # ordinary top-level ask() call, which offers the full tool list
        # exactly as before this hook existed.
        allowed_tools: Optional[frozenset] = context.get("allowed_tools")
        # Canonical bot.effort.EFFORT_LADDER value (or None) — each
        # transport maps it onto whatever its own wire protocol actually
        # supports (bot/effort.py's per-backend mapping functions),
        # silently doing nothing when there's no equivalent.
        effort = context.get("effort")

        # Image-understanding (Phase D of the native-parity plan) — see
        # bot/agent_runtime/vision.py's own docstring. Checked against
        # THIS call's primary transport (a mid-turn failover swap, if it
        # ever happens, only affects later iterations' text — the initial
        # image attach decision is made once, here). A transport with no
        # vision support gets every image dropped with one honest note
        # appended to the prompt, never a silent vanish.
        from bot.agent_runtime import vision

        raw_images = context.get("images")
        prompt_text = prompt
        image_blocks: list = []
        if raw_images:
            if self.transport.supports_vision:
                image_blocks, dropped = vision.prepare(raw_images)
            else:
                image_blocks, dropped = [], len(raw_images)
            note = vision.dropped_note(dropped)
            if note:
                prompt_text = f"{prompt}\n\n{note}"

        history = db.list_agent_messages(session_key)
        user_entry = self.transport.user_message(prompt_text, images=image_blocks or None)
        history.append(user_entry)
        db.append_agent_message(session_key, user_entry["role"], user_entry["content"])

        tool_schemas = agent_tools.all_tool_schemas()
        if allowed_tools is not None:
            tool_schemas = [s for s in tool_schemas if s["name"] in allowed_tools]

        system_prompt = _build_system_prompt(instance_id)

        # Resolved once per ask() call, not per iteration — a fallback
        # that kicks in on iteration N stays active for the rest of this
        # turn (a primary that just failed is likely still down a moment
        # later), rather than re-attempting the primary every iteration.
        active_transport = self.transport
        active_model = self.model

        total_tokens = 0
        for _ in range(MAX_TOOL_ITERATIONS):
            if steer_queue is not None:
                steered = []
                while not steer_queue.empty():
                    steered.append(steer_queue.get_nowait())
                if steered:
                    steer_text = "[The user sent this mid-turn — take it into account:]\n" + "\n".join(steered)
                    steer_entry = active_transport.user_message(steer_text)
                    history.append(steer_entry)
                    db.append_agent_message(session_key, steer_entry["role"], steer_entry["content"])

            try:
                response = await active_transport.send(
                    model=active_model,
                    history=history,
                    tool_schemas=tool_schemas,
                    max_tokens=self.max_tokens,
                    timeout_s=timeout_s,
                    system_prompt=system_prompt,
                    effort=effort,
                )
            except BackendError as exc:
                # One bounded retry against a configured fallback
                # provider/model (bot/agent_settings.py's fallback_provider/
                # fallback_model) — mirrors Hermes's own real
                # try_activate_fallback concept at a deliberately bounded
                # (one hop, not a multi-provider chain) scope, matching
                # this codebase's existing "one bounded retry" convention
                # (output_schema validation already works this way). Never
                # retries an EstopEngagedError — that's not a transport
                # failure a different provider would fix.
                from bot.agent_runtime import estop as estop_module

                if isinstance(exc, estop_module.EstopEngagedError) or active_transport is not self.transport:
                    raise
                fallback = _resolve_fallback_transport(instance_id)
                if fallback is None:
                    raise
                logger.warning("native backend: primary transport failed (%s) — retrying once against configured fallback", exc)
                active_transport, active_model = fallback
                response = await active_transport.send(
                    model=active_model,
                    history=history,
                    tool_schemas=tool_schemas,
                    max_tokens=self.max_tokens,
                    timeout_s=timeout_s,
                    system_prompt=system_prompt,
                    effort=effort,
                )
            if response.tokens:
                total_tokens += response.tokens

            history.append(response.assistant_message)
            db.append_agent_message(session_key, response.assistant_message["role"], response.assistant_message["content"])

            if response.stop:
                raw = {"total_tokens": total_tokens}
                if lazily_created:
                    raw["desktop_session_key"] = session_key
                return BackendResult(text=response.text, tokens=total_tokens or None, raw=raw)

            results = []
            for tc in response.tool_calls:
                if progress is not None:
                    try:
                        await progress(_progress_line(tc.name, tc.arguments))
                    except Exception:
                        logger.exception("progress_notify callback failed")
                output = await tool_loop.run_one_tool(
                    tc.name, tc.arguments, workspace=workspace,
                    instance_id=instance_id, chat_id=chat_id, session_key=session_key,
                    notify=notify, agent_tools=agent_tools, agent_approval=agent_approval,
                )
                results.append((tc, output))

            for entry in active_transport.tool_result_messages(results):
                history.append(entry)
                db.append_agent_message(session_key, entry["role"], entry["content"])

        raise BackendError(f"agent loop exceeded {MAX_TOOL_ITERATIONS} tool calls without a final answer")


def _resolve_fallback_transport(instance_id) -> Optional[tuple]:
    """(transport, model) for this instance's configured
    fallback_provider/fallback_model (bot/agent_settings.py), or None if
    neither is configured or the provider can't be resolved — a missing/
    broken fallback config must never itself raise, since the caller
    would then lose the ORIGINAL, more informative transport error."""
    if instance_id is None:
        return None
    try:
        from bot import agent_settings

        settings = agent_settings.get(instance_id)
        provider_name = settings.get("fallback_provider")
        model = settings.get("fallback_model")
        if not provider_name or not model:
            return None
        from bot import providers as provider_registry
        from bot.agent_runtime.transports import build_openai_transport

        provider_cfg = provider_registry.get_provider(provider_name)
        if provider_cfg is None:
            return None
        transport = build_openai_transport(
            protocol=provider_cfg.get("protocol", "openai"), base_url=provider_cfg["base_url"],
            api_key=provider_registry.get_api_key(provider_name), catalog_id=provider_cfg.get("catalog_id"),
        )
        return transport, model
    except Exception:
        logger.exception("native backend: failed to resolve fallback transport for instance %s", instance_id)
        return None


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
