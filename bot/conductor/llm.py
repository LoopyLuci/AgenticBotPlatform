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
    from abp_run.core import split_model, transport_for

    last: Optional[BaseException] = None
    for ref in refs:
        try:
            if "/" not in ref or ref.startswith("anthropic/"):      # a bare name would resolve to the Anthropic transport
                continue
            provider, name = split_model(ref, default_provider="")
            transport = transport_for(provider, name)
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
