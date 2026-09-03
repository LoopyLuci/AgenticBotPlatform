"""Mixture-of-Agents as an on-demand tool — fans a question out to N
reference models in parallel via asyncio.gather, then either returns
their labeled raw answers (no aggregator given) or makes one more call
synthesizing them (aggregator given), mirroring the real Hermes Agent's
own aggregate_moa_context() labeled-block prompt shape. Unlike
spawn_subagent, this is single-shot per reference — a plain completion,
not an agentic tool-use turn — so it talks to the Phase A transport
layer (bot/agent_runtime/transports/) directly rather than going through
NativeAgentBackend's tool loop at all.

Standing, always-on MoA (Hermes's own moa.fanout: user_turn/per_iteration
config) is deliberately NOT built here — this on-demand tool captures
most of the real value (the agent decides WHEN a second opinion is worth
the extra cost) without the state-signature caching machinery an
always-on mode needs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.agent_runtime.transports.base import ProviderTransport
from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport
from bot.backends.base import BackendError

logger = logging.getLogger("bot.agent_runtime.moa")

MAX_TOKENS = 4096
CALL_TIMEOUT_S = 120.0


def _transport_for(provider: Optional[str], model: str) -> ProviderTransport:
    """provider=None (or "anthropic") means the built-in Claude API;
    anything else must be a named config/providers.yaml entry."""
    if not provider or provider == "anthropic":
        return AnthropicTransport()
    from bot import providers as provider_registry

    provider_cfg = provider_registry.get_provider(provider)
    if provider_cfg is None:
        raise BackendError(f"no provider named {provider!r} configured in config/providers.yaml")
    return OpenAICompatibleTransport(base_url=provider_cfg["base_url"], api_key=provider_registry.get_api_key(provider))


async def _single_call(provider: Optional[str], model: str, prompt: str) -> str:
    transport = _transport_for(provider, model)
    history = [transport.user_message(prompt)]
    response = await transport.send(
        model=model, history=history, tool_schemas=[], max_tokens=MAX_TOKENS, timeout_s=CALL_TIMEOUT_S
    )
    return response.text


async def _run_reference(ref: dict) -> dict[str, Any]:
    label_provider = ref.get("provider") or "anthropic"
    try:
        text = await _single_call(ref.get("provider"), ref["model"], ref["_question"])
        return {"provider": label_provider, "model": ref["model"], "text": text, "error": None}
    except Exception as exc:
        logger.warning("consult_models reference %s/%s failed: %s", label_provider, ref.get("model"), exc)
        return {"provider": label_provider, "model": ref["model"], "text": None, "error": str(exc)}


async def consult(question: str, references: list[dict], aggregator: Optional[dict] = None) -> str:
    """`references`: [{"provider": str|None, "model": str}, ...].
    `aggregator`: {"provider": str|None, "model": str} or None. Raises
    BackendError only for a request-shape problem resolvable before any
    call is made (e.g. an unknown provider on the aggregator itself) —
    an individual reference failing never fails the whole call, its
    failure is just noted in that reference's own labeled block."""
    if not references:
        raise BackendError("at least one reference model is required")

    tasks = [dict(ref, _question=question) for ref in references]
    results = await asyncio.gather(*(_run_reference(r) for r in tasks))

    if not aggregator:
        return "\n\n".join(
            f"[{r['provider']}/{r['model']}]: " + (r["text"] if r["error"] is None else f"(failed: {r['error']})")
            for r in results
        )

    labeled_blocks = "\n\n".join(
        f"Reference {i + 1} ({r['provider']}/{r['model']}):\n"
        + (r["text"] if r["error"] is None else f"(this reference failed: {r['error']})")
        for i, r in enumerate(results)
    )
    aggregator_prompt = (
        "You are synthesizing answers from multiple reference models that were each asked the same "
        "question independently. Produce one clear, correct final answer, resolving any disagreements "
        "using your own judgment. Note if a reference failed rather than pretending it answered.\n\n"
        f"Question: {question}\n\n{labeled_blocks}"
    )
    return await _single_call(aggregator.get("provider"), aggregator["model"], aggregator_prompt)
