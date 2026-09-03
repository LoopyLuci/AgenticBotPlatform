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

from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport
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
})

DEFAULT_MAX_CONCURRENT_CHILDREN = 6
CHILD_TIMEOUT_S = 300.0


def _resolve_named_backend(provider: str, model: str) -> NativeAgentBackend:
    from bot import providers as provider_registry

    provider_cfg = provider_registry.get_provider(provider)
    if provider_cfg is None:
        raise BackendError(f"no provider named {provider!r} configured in config/providers.yaml")
    transport = OpenAICompatibleTransport(
        base_url=provider_cfg["base_url"], api_key=provider_registry.get_api_key(provider)
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
    max_children: Optional[int] = None,
    parent_instance_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """`tasks`: [{"goal": str, "output_schema": dict|None}, ...]. Returns
    [{"index", "goal", "model", "status": "ok"|"error", "result_excerpt"}]
    — the exact shape bot/swarm/child_parser.py already parses from a
    Hermes-external dispatch's final reply, so dashboard observability
    code understands both kinds of fan-out identically."""
    from bot.agent_runtime.tools import _delegation_depth
    from bot.config import config

    if not tasks:
        return []

    max_depth = config.current.get("agent_runtime", {}).get("max_delegation_depth", 2)
    depth = _delegation_depth.get()
    if depth >= max_depth:
        raise BackendError(
            f"delegation depth limit reached (depth={depth}, max_delegation_depth={max_depth}) — "
            "raise agent_runtime.max_delegation_depth in config/backends.yaml if deeper nesting is required"
        )

    cfg_cap = config.current.get("native_agent", {}).get("max_concurrent_children", DEFAULT_MAX_CONCURRENT_CHILDREN)
    effective_cap = min(max_children, cfg_cap) if max_children else cfg_cap
    semaphore = asyncio.Semaphore(max(1, effective_cap))

    backend = _resolve_named_backend(provider, model) if (provider and model) else _resolve_inherited_backend(parent_instance_id)

    allowed_tools = None
    if role == "leaf":
        from bot.agent_runtime.tools import TOOL_SCHEMA_NAMES

        allowed_tools = frozenset(TOOL_SCHEMA_NAMES) - LEAF_BLOCKED_TOOLS

    token = _delegation_depth.set(depth + 1)
    try:
        results = await asyncio.gather(*(
            _run_one_child(i, task, backend, semaphore, allowed_tools, parent_instance_id)
            for i, task in enumerate(tasks)
        ))
    finally:
        _delegation_depth.reset(token)
    return list(results)


async def _run_one_child(
    index: int,
    task: dict[str, Any],
    backend: NativeAgentBackend,
    semaphore: asyncio.Semaphore,
    allowed_tools: Optional[frozenset],
    parent_instance_id: Optional[int],
) -> dict[str, Any]:
    from bot import db
    from bot.agent_runtime.output_schema import validate_or_retry

    goal = (task.get("goal") or "").strip()
    if not goal:
        return {"index": index, "goal": "", "model": backend.model, "status": "error", "result_excerpt": "empty goal"}

    output_schema = task.get("output_schema")
    async with semaphore:
        session_id = db.create_ephemeral_session(parent_instance_id, backend.name, backend.model, goal)
        context: dict[str, Any] = {"instance_id": parent_instance_id}
        if allowed_tools is not None:
            context["allowed_tools"] = allowed_tools
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
        except Exception as exc:
            logger.warning("spawn_subagent child %d failed: %s", index, exc)
            db.finish_ephemeral_session(session_id, status="error", result=str(exc))
            return {"index": index, "goal": goal, "model": backend.model, "status": "error", "result_excerpt": str(exc)[:500]}
