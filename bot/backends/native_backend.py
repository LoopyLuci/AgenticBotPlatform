"""The one tool-calling loop shared by every provider-agnostic backend —
replaces what used to be two separately hand-written, near-identical
loops in api_backend.py (Anthropic) and custom_model_backend.py (OpenAI-
compatible). See bot/agent_runtime/transports/base.py's module docstring
for why a Transport exists at all; this class is everything that stays
the same regardless of which one is plugged in: history loading/
persistence, steer-queue draining, the system prompt (bot/agent_runtime/prompt.py),
per-tool progress notifications, and the actual approval/execute/
checkpoint round trip via bot.agent_runtime.tool_loop.run_one_tool().

bot/backends/api_backend.py and custom_model_backend.py are now thin
compatibility shims around this class — their public constructors and
`Backend` contract are unchanged, so bot/router.py needs no changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from bot.agent_runtime import context_window, loop_guard, toolspec, usage_limits
from bot.agent_runtime.transports.base import ProviderTransport
from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.native")

# Kept for anything that imported it; the real limit is native_agent.limits (see loop_guard.py).
MAX_TOOL_ITERATIONS = loop_guard.DEFAULT_MAX_ITERATIONS


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
        # Every run is traced (bot/agent_runtime/trace.py) — shape only, never
        # content — so the eval harness and dashboards can read what happened.
        from bot.agent_runtime import trace

        ctx = context or {}
        from bot.agent_runtime import permissions

        # A caller may set the permission mode for this run ("plan" makes it read-only).
        mode_token = permissions.mode_var.set(ctx.get("permission_mode"))
        run = trace.begin(
            agent=self.name, model=self.model, transport=type(self.transport).__name__,
            instance_id=ctx.get("instance_id"), session=ctx.get("desktop_session_key") or "",
            effort=ctx.get("effort") or "", source=ctx.get("source") or "",
        )
        with run:
            try:
                result = await self._ask(prompt, context=context, timeout_s=timeout_s)
            except asyncio.CancelledError:
                run.end("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 — recorded, then re-raised unchanged
                run.end("failed", error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                permissions.mode_var.reset(mode_token)
            run.end("ok")
            if run.run_id and isinstance(getattr(result, "raw", None), dict):
                result.raw["trace_run"] = run.run_id
            return result

    async def _ask(self, prompt: str, *, context=None, timeout_s: float = 30) -> BackendResult:
        from bot import db
        from bot.agent_runtime import approval as agent_approval
        from bot.agent_runtime import estop
        from bot.agent_runtime import tool_loop
        from bot.agent_runtime import tools as agent_tools
        from bot.agent_runtime import trace

        # Checked once, at the very start of a new turn — never mid-turn:
        # an already-running ask() finishes rather than being killed.
        estop.check()

        from bot.agent_runtime import hooks

        context = context or {}
        session_key = context.get("desktop_session_key")
        lazily_created = session_key is None
        if lazily_created:
            session_key = await self.create_session()

        instance_id = context.get("instance_id")
        if lazily_created:
            # SessionStart (Phase E of the Claude API/Claude Code parity
            # plan) — fires once per real new session, not on every turn
            # of an existing one.
            session_start_context = await hooks.run_session_start(instance_id=instance_id)
        else:
            session_start_context = None
        user_prompt_context = await hooks.run_user_prompt_submit(prompt, instance_id=instance_id)
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
        # Per-device permission tier (Server Chat/Support Bot only — see
        # the "Admin control surface" plan) — None for every ordinary
        # Telegram-driven turn, which never carries a device identity.
        device_tier: Optional[str] = context.get("device_tier")
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
        raw_documents = context.get("documents")
        prompt_text = prompt
        image_blocks: list = []
        document_blocks: list = []
        notes: list[str] = []
        if raw_images:
            if self.transport.supports_vision:
                image_blocks, dropped = vision.prepare(raw_images)
            else:
                image_blocks, dropped = [], len(raw_images)
            note = vision.dropped_note(dropped)
            if note:
                notes.append(note)
        if raw_documents:
            # PDF/document support (Phase C of the Claude API/Claude Code
            # parity plan) — scoped to AnthropicTransport only (see
            # supports_documents on ProviderTransport); every other
            # transport gets every document dropped with the same honest
            # note images already get on a non-vision transport.
            if self.transport.supports_documents:
                document_blocks, dropped = vision.prepare_documents(raw_documents)
            else:
                document_blocks, dropped = [], len(raw_documents)
            note = vision.dropped_note(dropped, kind="document")
            if note:
                notes.append(note)
        if user_prompt_context:
            # UserPromptSubmit's additionalContext (Phase E) — a hook-
            # injected note distinct from the vision/document drop notes
            # above, so kept as its own clearly-labeled block rather than
            # mixed in with them.
            notes.insert(0, f"[Context from a UserPromptSubmit hook: {user_prompt_context}]")
        if notes:
            prompt_text = prompt + "\n\n" + "\n".join(notes)

        # Context compression (Phase F of the native-parity plan) — fires
        # at most once per ask() call, before this turn's own new prompt
        # is appended, so a fresh compression digest is never itself
        # swept back into the next compression's summarized range. See
        # bot/agent_runtime/compression.py's own docstring.
        from bot.agent_runtime import compression

        if await compression.maybe_compress(session_key, self.transport, model=self.model, instance_id=instance_id):
            trace.active().note("history compacted")

        history = db.list_agent_messages(session_key)
        dangling =self.transport.dangling_tool_calls(history) if hasattr(self.transport, "dangling_tool_calls") else []
        if dangling:
            # A previous turn was cut off after the model asked for tools; answer those calls so the
            # provider accepts the conversation again.
            for entry in self.transport.tool_result_messages([(tc, "Cancelled: this call did not finish.") for tc in dangling]):
                history.append(entry)
                db.append_agent_message(session_key, entry["role"], entry["content"])
        user_entry = self.transport.user_message(prompt_text, images=image_blocks or None, documents=document_blocks or None)
        history.append(user_entry)
        db.append_agent_message(session_key, user_entry["role"], user_entry["content"])

        tool_schemas = agent_tools.all_tool_schemas()
        # Admin control surface (see docs/adr/0008-single-instance-admin-tool-gate.md):
        # ADMIN_TOOLS_STANDARD is offered only to the one instance flagged
        # agent_settings.is_admin_instance=True; ADMIN_TOOLS_ELEVATED
        # additionally requires an elevated-or-higher device_tier — never
        # true for a Telegram-driven turn, which carries no device_tier at
        # all, so those tools are structurally unreachable from Telegram.
        from bot import agent_settings as _agent_settings

        is_admin_instance = bool(_agent_settings.get(instance_id)["is_admin_instance"])
        if not is_admin_instance:
            tool_schemas = [s for s in tool_schemas if s["name"] not in agent_tools.ADMIN_TOOLS_STANDARD]
        # ADMIN_TOOLS_ELEVATED requires BOTH conditions — being the admin
        # instance AND an elevated-or-higher device_tier — never device
        # tier alone, which would otherwise let a non-admin instance's
        # turn see device/destructive-op tools just by inheriting a
        # device_tier from context.
        if not (is_admin_instance and device_tier in ("elevated", "unrestricted")):
            tool_schemas = [s for s in tool_schemas if s["name"] not in agent_tools.ADMIN_TOOLS_ELEVATED]
        if allowed_tools is not None:
            tool_schemas = [s for s in tool_schemas if s["name"] in allowed_tools]

        from bot.agent_runtime import prompt as prompt_builder

        system_prompt = prompt_builder.build(
            instance_id, workspace=workspace, session_context=session_start_context,
            agent_prompt=context.get("agent_prompt"),
            include_agents=(allowed_tools is None or "spawn_subagent" in allowed_tools),
            model_line=_model_line(self.transport, self.model),
        )

        # Plan mode (Phase G of the Claude API/Claude Code parity plan) —
        # "propose a plan, get human sign-off, then execute." context["plan_first"]
        # is set either directly by a caller (spawn_subagent(plan_first=True))
        # or by bot/router.py from bot/agent_settings.py's
        # require_plan_approval field for a top-level instance. Enforced
        # by literally omitting tool_schemas on this one extra turn, not
        # just a prompt instruction — a stronger guarantee than asking
        # nicely, since it's then structurally impossible for that turn
        # to contain a real tool_use block regardless of what the model
        # tries to do.
        if context.get("plan_first"):
            plan_denial = await self._run_plan_gate(
                history=history, system_prompt=system_prompt, effort=effort, timeout_s=timeout_s,
                session_key=session_key, instance_id=instance_id, chat_id=chat_id, notify=notify,
            )
            if plan_denial is not None:
                return plan_denial

        # Resolved once per ask() call, not per iteration — a fallback
        # that kicks in on iteration N stays active for the rest of this
        # turn (a primary that just failed is likely still down a moment
        # later), rather than re-attempting the primary every iteration.
        active_transport = self.transport
        active_model = self.model

        # Streaming (P0 of docs/agents/ROADMAP.md): a caller that wants reply text as
        # it is generated passes context["stream_notify"], an async callable taking a
        # StreamEvent. A transport that cannot stream simply is not asked to; a
        # callback that raises must never break the turn.
        stream_notify = context.get("stream_notify")
        streamed = False
        # What is actually sent may be a shortened view of `history` (old tool outputs cleared); the stored
        # conversation is never shortened by that.
        view = {"history": history, "chars": 0}
        summaries = 0

        async def _emit(event) -> None:
            try:
                await stream_notify(event)
            except Exception:  # noqa: BLE001
                logger.exception("stream_notify callback failed")

        async def _send(transport, model):
            nonlocal streamed
            streamed = False
            usage_limits.current_model.set((getattr(transport, "provider_key", ""), model))
            kwargs = dict(
                model=model, history=view["history"], tool_schemas=tool_schemas, max_tokens=self.max_tokens,
                timeout_s=timeout_s, system_prompt=system_prompt, effort=effort,
            )
            if stream_notify is not None and getattr(transport, "supports_streaming", False):
                streamed = True
                return await transport.send_stream(on_event=_emit, **kwargs)
            return await transport.send(**kwargs)

        total_tokens = 0
        # Prompt-caching telemetry (AnthropicTransport only — see its
        # own send()) — None means "this transport doesn't report it,"
        # 0 means "it does, and this turn had a full cache miss," so
        # these stay None until at least one response actually reports
        # a value, rather than starting at 0.
        cache_creation_tokens: Optional[int] = None
        cache_read_tokens: Optional[int] = None
        watch = loop_guard.Watchdog(loop_guard.limits())
        stop_hook_blocks = 0
        tool_calls_made = 0
        while True:
            stop_reason = watch.before_call(total_tokens)
            if stop_reason:
                trace.active().note(f"stopped: {stop_reason}", level="warn")
                await hooks.run_notification("turn_stopped", f"The turn was stopped because {stop_reason}.",
                                             instance_id=instance_id)
                return await self._wrap_up(
                    stop_reason, history=history, transport=active_transport, model=active_model,
                    system_prompt=system_prompt, effort=effort, timeout_s=timeout_s, session_key=session_key,
                    total_tokens=total_tokens, lazily_created=lazily_created,
                )
            if steer_queue is not None:
                steered = []
                while not steer_queue.empty():
                    steered.append(steer_queue.get_nowait())
                if steered:
                    steer_text = "[The user sent this mid-turn — take it into account:]\n" + "\n".join(steered)
                    steer_entry = active_transport.user_message(steer_text)
                    history.append(steer_entry)
                    db.append_agent_message(session_key, steer_entry["role"], steer_entry["content"])

            report = context_window.manage(history, active_transport, active_model, system_prompt, tool_schemas)
            if report.needs_summary and summaries < 2:
                # Still too full after clearing old tool outputs: summarise the older conversation.
                if await compression.maybe_compress(session_key, active_transport, model=active_model,
                                                    instance_id=instance_id, force=True):
                    summaries += 1
                    history[:] = db.list_agent_messages(session_key)
                    report = context_window.manage(history, active_transport, active_model, system_prompt, tool_schemas)
                    trace.active().note("context summarised mid-turn")
            if report.notes:
                trace.active().note("; ".join(report.notes))
            view["history"] = report.history
            view["chars"] = context_window.measure_chars(report.history, system_prompt, tool_schemas)
            _sent_at = time.monotonic()
            try:
                response = await _send(active_transport, active_model)
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
                if stream_notify is not None:
                    # Whatever the failed attempt already showed is about to be repeated.
                    from bot.agent_runtime.transports.base import StreamEvent

                    await _emit(StreamEvent("reset"))
                response = await _send(active_transport, active_model)
            context_window.observe(active_model, view["chars"], response.input_tokens)
            trace.active().llm_call(
                model=active_model, duration_ms=int((time.monotonic() - _sent_at) * 1000), tokens=response.tokens,
                tool_calls=len(response.tool_calls), cache_read=response.cache_read_tokens,
                cache_create=response.cache_creation_tokens, streamed=streamed,
            )
            if response.tokens:
                total_tokens += response.tokens
            if response.cache_creation_tokens is not None:
                cache_creation_tokens = (cache_creation_tokens or 0) + response.cache_creation_tokens
            if response.cache_read_tokens is not None:
                cache_read_tokens = (cache_read_tokens or 0) + response.cache_read_tokens
            if response.thinking_summary and progress is not None and _show_thinking_summary_enabled():
                try:
                    await progress(f"🧠 {response.thinking_summary}")
                except Exception:
                    logger.exception("progress_notify callback failed")

            history.append(response.assistant_message)
            db.append_agent_message(session_key, response.assistant_message["role"], response.assistant_message["content"])

            if response.stop:
                # A Stop hook may ask the agent to carry on (twice at most, so a hook cannot loop it forever).
                if stop_hook_blocks < 2:
                    follow = await hooks.run_stop(response.text, instance_id=instance_id)
                    if follow:
                        stop_hook_blocks += 1
                        entry = active_transport.user_message(f"[A Stop hook asks you to keep going: {follow}]")
                        history.append(entry)
                        db.append_agent_message(session_key, entry["role"], entry["content"])
                        continue
                from bot.agent_runtime import skill_learning

                if skill_learning.enabled():
                    # A long task may be worth a skill draft (never installed without a person's approval).
                    await skill_learning.maybe_draft(
                        transport=active_transport, model=active_model, history=history, session=session_key,
                        tool_calls=tool_calls_made, run_id=trace.active().run_id or "")
                raw = {"total_tokens": total_tokens}
                if lazily_created:
                    raw["desktop_session_key"] = session_key
                if cache_creation_tokens is not None:
                    raw["cache_creation_tokens"] = cache_creation_tokens
                if cache_read_tokens is not None:
                    raw["cache_read_tokens"] = cache_read_tokens
                return BackendResult(text=response.text, tokens=total_tokens or None, raw=raw)

            results: list = []

            async def _one(tc):
                if progress is not None:
                    try:
                        await progress(_progress_line(tc.name, tc.arguments))
                    except Exception:
                        logger.exception("progress_notify callback failed")
                return await tool_loop.run_one_tool(
                    tc.name, tc.arguments, workspace=workspace,
                    instance_id=instance_id, chat_id=chat_id, session_key=session_key,
                    notify=notify, agent_tools=agent_tools, agent_approval=agent_approval,
                    device_tier=device_tier,
                )

            try:
                await loop_guard.run_calls(response.tool_calls, _one, toolspec.is_concurrency_safe, results)
            except BaseException:
                # Cancelled (or failed) part-way: every tool call the model made still needs an
                # answer in the history, or the next turn is rejected by the provider.
                done = {id(tc) for tc, _ in results}
                for tc in response.tool_calls:
                    if id(tc) not in done:
                        results.append((tc, "Cancelled: this call did not finish."))
                for entry in active_transport.tool_result_messages(results):
                    history.append(entry)
                    db.append_agent_message(session_key, entry["role"], entry["content"])
                raise

            tool_calls_made += len(results)
            notes = watch.after_round(results)
            results = [(tc, out + note) for (tc, out), note in zip(results, notes)]
            for entry in active_transport.tool_result_messages(results):
                history.append(entry)
                db.append_agent_message(session_key, entry["role"], entry["content"])

    async def _wrap_up(self, reason: str, *, history: list, transport, model: str, system_prompt: str, effort,
                       timeout_s: float, session_key: str, total_tokens: int, lazily_created: bool) -> BackendResult:
        """A turn hit a limit or got stuck. Ask for a short summary with no tools, and return it as the
        reply. Nothing is lost: the session keeps every step, so "continue" resumes."""
        from bot import db

        raw: dict = {"total_tokens": total_tokens, "stopped": reason}
        if lazily_created:
            raw["desktop_session_key"] = session_key
        prompt = (f"[The turn was stopped because {reason}. In a few sentences, say what you have done so far and "
                  "what remains. Do not call tools.]")
        try:
            entry = transport.user_message(prompt)
            history.append(entry)
            db.append_agent_message(session_key, entry["role"], entry["content"])
            response = await transport.send(
                model=model, history=history, tool_schemas=[], max_tokens=self.max_tokens, timeout_s=timeout_s,
                system_prompt=system_prompt, effort=effort,
            )
            db.append_agent_message(session_key, response.assistant_message["role"], response.assistant_message["content"])
            text = response.text.strip()
            if response.tokens:
                raw["total_tokens"] = total_tokens + response.tokens
        except Exception:  # noqa: BLE001 - the summary is a courtesy; failing to get one must not lose the turn
            logger.exception("native backend: could not get a wrap-up summary")
            text = ""
        notice = f"(Stopped: {reason}. Say \"continue\" to carry on.)"
        return BackendResult(text=f"{text}\n\n{notice}" if text else notice, tokens=raw["total_tokens"] or None, raw=raw)

    async def _run_plan_gate(
        self, *, history: list, system_prompt: str, effort, timeout_s: float,
        session_key: str, instance_id, chat_id, notify,
    ) -> Optional[BackendResult]:
        """Plan mode's own extra turn, run before the real tool-enabled
        loop starts. Mutates `history`/persists to db.agent_messages
        exactly like a normal iteration would (the plan proposal and the
        human's "approved" ack both need to survive into the real turns
        that follow). Returns a BackendResult only when the plan was
        denied (the caller should return it immediately, unchanged);
        returns None when approved, meaning "continue as normal.\""""
        from bot import db
        from bot.agent_runtime import approval as agent_approval

        plan_instruction = (
            "Before doing anything else, propose a short, numbered plan of exactly what you intend "
            "to do to accomplish this request. Do not take any action yet — just describe the plan "
            "in your reply; you will get to act on it once it's approved."
        )
        plan_system_prompt = f"{system_prompt}\n\n{plan_instruction}" if system_prompt else plan_instruction
        plan_response = await self.transport.send(
            model=self.model, history=history, tool_schemas=[], max_tokens=self.max_tokens,
            timeout_s=timeout_s, system_prompt=plan_system_prompt, effort=effort,
        )
        history.append(plan_response.assistant_message)
        db.append_agent_message(session_key, plan_response.assistant_message["role"], plan_response.assistant_message["content"])

        if notify is None:
            # No chat to ask — mirrors tool_loop.run_one_tool()'s own
            # "no notify channel" fallback: request_plan_approval still
            # waits out its timeout and denies, rather than silently
            # granting tool access nobody could actually review.
            async def _no_notify(_id, _name, _input):
                logger.warning("plan approval needed but no notify channel is set — will time out and deny")

            notify_fn = _no_notify
        else:
            notify_fn = notify

        approved = await agent_approval.request_plan_approval(
            instance_id, chat_id, session_key, plan_response.text, notify=notify_fn,
            # Read at call time, not relied on as request_plan_approval()'s
            # own default parameter (which would bind to whatever
            # DEFAULT_TIMEOUT_S was at module-import time forever) — this
            # is what actually lets tests/an operator override it.
            timeout_s=agent_approval.DEFAULT_TIMEOUT_S,
        )
        if not approved:
            return BackendResult(text="Plan denied.", tokens=plan_response.tokens, raw={"plan_denied": True})

        approval_entry = self.transport.user_message("Plan approved — proceed.")
        history.append(approval_entry)
        db.append_agent_message(session_key, approval_entry["role"], approval_entry["content"])
        return None


def _show_thinking_summary_enabled() -> bool:
    from bot.config import config

    return (config.current.get("native_agent") or {}).get("show_thinking_summary", False)


def _model_line(transport, model) -> Optional[str]:
    """One line telling the agent what it is running on. Never raises."""
    try:
        from bot import model_catalog

        return model_catalog.prompt_line(getattr(transport, "provider_key", ""), model)
    except Exception:  # noqa: BLE001
        return None


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
