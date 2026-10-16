"""The conductor's model calls: local models only.

The planner and the model review ask for text through `complete()`. It offers the local models ABP can see (ModelMistress first,
then Ollama, bot/capacity/routing.local_candidates) and refuses any other ref, so the conductor can never reach a cloud model by
accident, whatever the capacity state. Tests replace it with `set_completer()`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("bot.conductor.llm")

Completer = Callable[..., Awaitable[tuple[str, str]]]      # (prompt, system=, max_tokens=, model=, avoid=) -> (text, model ref)
_completer: Optional[Completer] = None


class NoLocalModel(RuntimeError):
    """No local model is available (or the one asked for is not local)."""

    release_task = True      # not the task's fault: the board task is handed back (bounded) instead of failed


class WorkerModelError(RuntimeError):
    """The agent's model could not be reached or failed before it did anything (no tool call): not the task's fault."""

    release_task = True


def transport_for_ref(ref: str):
    """(transport, model name) for a local model ref. Never the Anthropic transport: abp_run's split_model/transport_for fall back to it
    when the provider is unknown, which would silently send local work to a cloud model, so an unknown provider is refused here."""
    head, _, tail = str(ref).partition("/")
    if not tail or head in ("", "anthropic"):
        raise NoLocalModel(f"{ref!r} is not a provider/model reference to a local model")
    from bot import providers
    from bot.capacity import routing

    if not routing.is_local_ref(ref):
        raise NoLocalModel(f"{ref} is not a local model; the conductor uses local models only")
    if head in providers.list_providers():
        from abp_run.core import transport_for

        return transport_for(head, tail), tail
    if head == "modelmistress":                      # a hub found by ABP_MODELMISTRESS_URL / the config rather than the module registry
        from bot.agent_runtime.transports import build_openai_transport
        from bot.modelmistress import client as mm

        hub = mm.find()
        if hub is not None:
            return build_openai_transport(protocol="openai", base_url=hub.openai_base, api_key=hub.token or "unused",
                                          name="modelmistress"), tail
    raise NoLocalModel(f"no provider {head!r} is available (is its hub running?)")


def set_completer(fn: Optional[Completer]) -> None:
    global _completer
    _completer = fn


def candidates(preferred: str = "", avoid: str = "") -> list[str]:
    """Local model refs to try, in order. `preferred` must itself be local; `avoid` goes last (a reviewer other than the planner)."""
    from bot.capacity import routing

    if preferred and not routing.is_local_ref(preferred):
        raise NoLocalModel(f"{preferred} is not a local model; the conductor uses local models only")
    refs = ([preferred] if preferred else []) + [r for r in routing.local_candidates() if r != preferred]
    if avoid and avoid in refs and len(refs) > 1:
        refs = [r for r in refs if r != avoid] + [avoid]
    return refs[:3]


async def complete(prompt: str, *, system: str = "", max_tokens: int = 2000, model: str = "", avoid: str = "",
                   timeout_s: float = 180.0) -> tuple[str, str]:
    """(text, the model that answered). Raises NoLocalModel, or the last model error when every candidate failed."""
    if _completer is not None:
        return await _completer(prompt, system=system, max_tokens=max_tokens, model=model, avoid=avoid)
    refs = candidates(model, avoid)
    if not refs:
        raise NoLocalModel("no local model is available (start ModelMistress or Ollama)")
    last: Optional[BaseException] = None
    for ref in refs:
        try:
            if "/" not in ref or ref.startswith("anthropic/"):      # a bare name would resolve to the Anthropic transport
                continue
            transport, name = transport_for_ref(ref)
            resp = await asyncio.wait_for(
                transport.send(model=name, history=[transport.user_message(prompt)], tool_schemas=[], max_tokens=max_tokens,
                               timeout_s=timeout_s, system_prompt=system), timeout=timeout_s + 10)
            return (resp.text or "").strip(), ref
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - try the next local model
            logger.info("conductor: %s failed (%s)", ref, exc)
            last = exc
    raise last if last else NoLocalModel("no local model answered")
