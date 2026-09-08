"""Ephemeral, disposable sub-agent fan-out — grows
bot.agent_runtime.tools.delegate_to_instance's proven single-hop
mechanism (an LLM decides mid-turn, a nested await, a contextvars-based
depth guard) into the real Hermes Agent delegate_task shape: a BATCH of
children, run concurrently, with role-based tool stripping and optional
structured-output contracts. No subprocess and no thread pool — BotServer
is asyncio-native throughout, so `asyncio.gather` under an
`asyncio.Semaphore` replaces what Hermes's own DaemonThreadPoolExecutor
does for the same reason.

A child is NOT a bot_instances row — it's a throwaway
NativeAgentBackend turn tracked only in the ephemeral_sessions table
(bot/db.py), pruned by the existing retention mechanism. This keeps a
dispatch of N children from cluttering the dashboard's Bots tab with N
disposable rows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from bot.agent_runtime import subagent_registry
from bot.agent_runtime.tools import ADMIN_TOOLS
from bot.agent_runtime.transports import build_openai_transport
from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.backends.base import BackendError
from bot.backends.native_backend import NativeAgentBackend

logger = logging.getLogger("bot.agent_runtime.subagents")

# Mirrors Hermes's own DELEGATE_BLOCKED_TOOLS (tools/delegate_tool.py) —
# always stripped from a role="leaf" child regardless of what the
# parent's own tool list allows, so a leaf worker can never recursively
# delegate, reconfigure another instance's identity, write shared
# project context, or save memory in the parent's name.
LEAF_BLOCKED_TOOLS = frozenset({
    "spawn_subagent", "delegate_to_instance", "update_agent_config",
    "write_project_context", "save_memory",
    # A leaf worker shouldn't schedule recurring commands or install new
    # skills in the parent's name — same reasoning as save_memory/
    # write_project_context above, just for two tools added later.
    "schedule_command", "install_skill", "create_skill",
    # New code-execution-authoring tools (added alongside create_skill) —
    # a leaf worker must never author or activate new unsandboxed,
    # full-privilege plugin code in the parent's name.
    "create_plugin", "enable_plugin",
    # Admin control surface (see docs/adr/0008-single-instance-admin-tool-gate.md)
    # — a leaf child must NEVER inherit admin tools, even one spawned by
    # the admin instance itself. This computation has no instance_id/
    # is_admin_instance check of its own (it only ever subtracts from
    # TOOL_SCHEMA_NAMES), so this frozenset is the ONLY enforcement point
    # stopping any instance's leaf child from seeing these tools.
} | ADMIN_TOOLS)

DEFAULT_MAX_CONCURRENT_CHILDREN = 6
CHILD_TIMEOUT_S = 300.0


def _resolve_named_backend(provider: str, model: str) -> NativeAgentBackend:
    from bot import providers as provider_registry

    provider_cfg = provider_registry.get_provider(provider)
    if provider_cfg is None:
        raise BackendError(f"no provider named {provider!r} configured in config/providers.yaml")
    transport = build_openai_transport(
        protocol=provider_cfg.get("protocol", "openai"), base_url=provider_cfg["base_url"],
        api_key=provider_registry.get_api_key(provider), catalog_id=provider_cfg.get("catalog_id"),
    )
    return NativeAgentBackend(transport, model=model, session_prefix="ephemeral", name="native_agent")


def _resolve_inherited_backend(parent_instance_id: Optional[int]) -> NativeAgentBackend:
    """No provider/model given — the child inherits the PARENT bot
    instance's own resolved backend/model, matching Hermes's own
    _resolve_delegation_credentials() inheritance behavior. Only
    api/custom_model parents make sense here: those are the only two
    backends that run BotServer's own tool loop at all (and therefore
    the only ones spawn_subagent is ever called from)."""
    if parent_instance_id is None:
        raise BackendError(
            "spawn_subagent needs a parent instance context to inherit a model from — "
            "pass provider/model explicitly instead"
        )
    from bot import bot_instances

    instance = bot_instances.get_instance(parent_instance_id)
    if not instance:
        raise BackendError(f"instance {parent_instance_id} not found")

    backend_name = instance.get("backend")
    model_override = instance.get("model")
    if backend_name == "api":
        from bot.models import DEFAULT_API_MODEL

        return NativeAgentBackend(
            AnthropicTransport(), model=model_override or DEFAULT_API_MODEL,
            session_prefix="ephemeral", name="native_agent",
        )
    if backend_name == "custom_model":
        if not model_override:
            raise BackendError(f"instance {parent_instance_id} has no model configured to inherit")
        from bot import providers as provider_registry

        provider_name, model_id = provider_registry.parse_model_ref(model_override)
        return _resolve_named_backend(provider_name, model_id)

    raise BackendError(
        f"spawn_subagent has no default provider/model to inherit from backend {backend_name!r} "
        "— pass provider/model explicitly"
    )


async def run_batch(
    tasks: list[dict[str, Any]],
    *,
    role: str = "leaf",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    max_children: Optional[int] = None,
    parent_instance_id: Optional[int] = None,
    background: bool = False,
) -> dict[str, Any]:
    """`tasks`: [{"goal": str, "output_schema": dict|None, "provider":
    str|None, "model": str|None, "effort": str|None}, ...] — a task's own
    provider+model/effort override the batch-level ones given here, which
    themselves override bot/agent_settings.py's worker_provider/
    worker_model/worker_effort, which fall back to inheriting the
    calling instance's own backend. This per-TASK granularity is real
    capability Hermes's own delegate_task tool does not have (confirmed
    against its real source — model/provider/reasoning_effort there are
    entirely config.yaml-driven, one shared value for every child in
    every call) — an orchestrator here can give different subtasks
    different models/effort in the same batch based on how hard each one
    actually is.

    Default (background=False): blocks until every child finishes, same
    as always, and returns
    {"dispatch_id", "children": [{"index", "goal", "model", "status": "ok"|"error", "result_excerpt"}]}
    — the `children` shape is exactly what bot/swarm/child_parser.py
    already parses from a Hermes-external dispatch's final reply, so
    dashboard observability code understands both kinds of fan-out
    identically. `dispatch_id` is returned even though the dispatch is
    already finished by the time this returns, so a caller can still
    look results up again later via list_subagents().

    background=True: registers every child (so list_subagents/
    steer_subagent/stop_subagent can reach them) and returns immediately
    with {"dispatch_id", "children": [{"index", "goal"}]} — no waiting.
    The children keep running on this same process's event loop; see
    this module's own docstring and the plan that added this for why
    that's an honest, bounded claim rather than a persistent-process
    guarantee Hermes's own delegate_task(background=true) makes."""
    from bot import agent_settings
    from bot.agent_runtime import estop
    from bot.agent_runtime.tools import _delegation_depth
    from bot.config import config

    estop.check()

    if not tasks:
        return {"dispatch_id": None, "children": []}

    max_depth = config.current.get("agent_runtime", {}).get("max_delegation_depth", 2)
    depth = _delegation_depth.get()
    if depth >= max_depth:
        raise BackendError(
            f"delegation depth limit reached (depth={depth}, max_delegation_depth={max_depth}) — "
            "raise agent_runtime.max_delegation_depth in config/backends.yaml if deeper nesting is required"
        )

    # A caller's own explicit args always win; absent that, fall back to
    # the unified agent_settings surface (bot/agent_settings.py) before
    # today's hardcoded/config defaults — purely additive, no behavior
    # change for any existing call that already passes explicit args.
    settings = agent_settings.get(parent_instance_id)
    cfg_cap = settings["max_concurrent_children"]
    effective_cap = min(max_children, cfg_cap) if max_children else cfg_cap
    semaphore = asyncio.Semaphore(max(1, effective_cap))

    if not provider and not model and settings["worker_provider"] and settings["worker_model"]:
        provider, model = settings["worker_provider"], settings["worker_model"]
    default_backend = _resolve_named_backend(provider, model) if (provider and model) else _resolve_inherited_backend(parent_instance_id)
    batch_effort = effort if effort is not None else settings["worker_effort"]

    allowed_tools = None
    if role == "leaf":
        from bot.agent_runtime.tools import TOOL_SCHEMA_NAMES

        allowed_tools = frozenset(TOOL_SCHEMA_NAMES) - LEAF_BLOCKED_TOOLS

    dispatch = subagent_registry.new_dispatch(parent_instance_id)
    token = _delegation_depth.set(depth + 1)
    try:
        for i, task in enumerate(tasks):
            task_provider = task.get("provider")
            task_model = task.get("model")
            if bool(task_provider) != bool(task_model):
                raise BackendError(f"task {i}: provider and model must both be given, or both omitted")
            task_backend = _resolve_named_backend(task_provider, task_model) if (task_provider and task_model) else default_backend
            task_effort = task.get("effort") or batch_effort
            handle = await _start_child(
                dispatch, i, task, task_backend, semaphore, allowed_tools, parent_instance_id, task_effort
            )
            subagent_registry.register_child(dispatch, i, handle)
    finally:
        _delegation_depth.reset(token)

    if background:
        return {
            "dispatch_id": dispatch.dispatch_id,
            "children": [{"index": i, "goal": h.goal} for i, h in dispatch.children.items()],
        }

    # Deliberately NOT removed from the registry once gather() returns —
    # see subagent_registry.describe()'s docstring: a caller should still
    # be able to list_subagents(dispatch_id) this dispatch afterward and
    # get its real final results, not a "no such dispatch" error just
    # because it already finished.
    #
    # return_exceptions=True matters here specifically for
    # stop_subagent(): cancelling one child raises CancelledError out of
    # its task, and without this, a plain gather() would propagate that
    # straight out of run_batch — crashing the WHOLE blocking dispatch
    # (every sibling's real results lost) just because one child was
    # deliberately stopped. Each raised exception is converted below into
    # the same per-child dict shape a normal result already has, so a
    # caller never needs to know whether a given entry came back via a
    # return or an exception.
    raw_results = await asyncio.gather(*(h.task for h in dispatch.children.values()), return_exceptions=True)
    children = []
    for (index, handle), outcome in zip(dispatch.children.items(), raw_results):
        if isinstance(outcome, BaseException):
            status = "stopped" if isinstance(outcome, asyncio.CancelledError) else "error"
            children.append({
                # default_backend.model here is only an informational
                # label for this exceptional (cancelled/errored-before-
                # returning) path — a task-level model override, if any,
                # was already recorded in the handle's own goal/session
                # data, not tracked redundantly on ChildHandle itself.
                "index": index, "goal": handle.goal, "model": default_backend.model,
                "status": status, "result_excerpt": str(outcome)[:500] or status,
            })
        else:
            children.append(outcome)
    return {"dispatch_id": dispatch.dispatch_id, "children": children}


async def _start_child(
    dispatch,
    index: int,
    task: dict[str, Any],
    backend: NativeAgentBackend,
    semaphore: asyncio.Semaphore,
    allowed_tools: Optional[frozenset],
    parent_instance_id: Optional[int],
    effort: Optional[str] = None,
):
    """Creates the child's ephemeral_sessions row and steer queue up
    front (before the task starts running), then wraps _run_one_child in
    a real asyncio.Task so it has a handle steer_subagent/stop_subagent
    can act on for as long as it's alive."""
    from bot import db
    from bot.agent_runtime import subagent_registry
    from bot.config import config

    # Distinct from the per-call asyncio.Semaphore above: this is a
    # process-wide ceiling across EVERY dispatch, closing the confirmed
    # gap where repeated spawn_subagent(background=true) calls, each
    # self-limited only within its own batch, could accumulate unbounded
    # live tasks over time with nothing tracking the running total.
    max_global = config.current.get("native_agent", {}).get("max_global_background_children", 20)
    if subagent_registry.count_live_children() >= max_global:
        raise BackendError(
            f"global background-dispatch limit reached ({max_global} live children across all dispatches) — "
            "wait for some to finish, or raise native_agent.max_global_background_children in config/backends.yaml"
        )

    from bot.agent_runtime.subagent_registry import ChildHandle

    goal = (task.get("goal") or "").strip()
    session_id = db.create_ephemeral_session(parent_instance_id, backend.name, backend.model, goal)
    steer_queue: "asyncio.Queue[str]" = asyncio.Queue()
    coro = _run_one_child(
        index, task, goal, session_id, backend, semaphore, allowed_tools, parent_instance_id, steer_queue, effort
    )
    aio_task = asyncio.create_task(coro)
    return ChildHandle(
        task=aio_task, steer_queue=steer_queue, ephemeral_session_id=session_id,
        parent_instance_id=parent_instance_id, goal=goal,
    )


async def _run_one_child(
    index: int,
    task: dict[str, Any],
    goal: str,
    session_id: int,
    backend: NativeAgentBackend,
    semaphore: asyncio.Semaphore,
    allowed_tools: Optional[frozenset],
    parent_instance_id: Optional[int],
    steer_queue: "asyncio.Queue[str]",
    effort: Optional[str] = None,
) -> dict[str, Any]:
    from bot import db
    from bot.agent_runtime.output_schema import validate_or_retry

    if not goal:
        db.finish_ephemeral_session(session_id, status="error", result="empty goal")
        return {"index": index, "goal": "", "model": backend.model, "status": "error", "result_excerpt": "empty goal"}

    output_schema = task.get("output_schema")
    async with semaphore:
        context: dict[str, Any] = {"instance_id": parent_instance_id, "steer_queue": steer_queue}
        if allowed_tools is not None:
            context["allowed_tools"] = allowed_tools
        if effort is not None:
            context["effort"] = effort
        try:
            result = await backend.ask(goal, context=context, timeout_s=CHILD_TIMEOUT_S)
            text = result.text
            status = "ok"
            if output_schema:
                ok, text_or_error = await validate_or_retry(
                    backend, text, output_schema, context=context, timeout_s=CHILD_TIMEOUT_S
                )
                text = text_or_error
                status = "ok" if ok else "error"
            db.finish_ephemeral_session(session_id, status=status, result=text)
            return {"index": index, "goal": goal, "model": backend.model, "status": status, "result_excerpt": text[:500]}
        except asyncio.CancelledError:
            # stop_subagent() already wrote the "stopped" status/result —
            # don't overwrite it with a generic cancellation message.
            raise
        except Exception as exc:
            logger.warning("spawn_subagent child %d failed: %s", index, exc)
            db.finish_ephemeral_session(session_id, status="error", result=str(exc))
            return {"index": index, "goal": goal, "model": backend.model, "status": "error", "result_excerpt": str(exc)[:500]}
