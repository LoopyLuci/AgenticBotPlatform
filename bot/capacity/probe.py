"""The recovery probe: while cloud capacity is short, ask the capped provider the smallest possible question now and then.

Without it, recovery waits for the provider's stated reset time, or (after a hold with no reset time) for a real call during
hybrid probation. The probe finds out sooner, cheaply, and without any real work being sent to a provider that may refuse it.

  * what it sends: one user message ("ping"), no tools, no system prompt, `max_tokens=1` - a few input tokens, one output token.
  * when: `capacity.probe.interval_s` (300 s) apart, doubling after each failure up to `max_interval_s` (1 h); never more than
    `max_per_hour` (6); an answer that names a reset time or a retry-after is waited for. When the provider stated a reset time,
    nothing is sent before `reset_fraction` (half) of the hold has passed.
  * what it does with the answer: works -> local-only becomes hybrid probation, a working probe in hybrid returns to cloud-led
    (the steps a real working call takes); a limit answer -> back off, and a later reset time in it extends the hold. Work that
    is running is never touched: the state only decides where *new* work goes.
  * who: a loop started with the app (`run_forever`), `POST /api/capacity/probe` / `abp capacity probe` for a probe now.
The governor keeps the schedule (it survives a restart); this module sends the request. Tests and embedders swap the sender with
`set_prober(async fn(provider, model, timeout_s) -> Outcome)`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from bot.capacity import classify, governor

logger = logging.getLogger("bot.capacity.probe")
POLL_S = 30.0


@dataclass
class Outcome:
    ok: bool
    status: str = ""                       # "ok", "usage_cap", "rate_limit", "error", "unprobeable"
    detail: str = ""
    reset_at: Optional[float] = None
    retry_after_s: Optional[float] = None


Prober = Callable[[str, str, float], Awaitable[Outcome]]


async def default_prober(provider: str, model: str, timeout_s: float) -> Outcome:
    """One 1-token request through the provider's own transport (so keys, base URLs and headers are the real ones)."""
    if not provider or not model:
        return Outcome(False, "unprobeable", "no model is known for this provider; set capacity.probe.targets (provider/model)")
    try:
        from abp_run.core import transport_for

        transport = transport_for(provider, model)
    except Exception as exc:  # noqa: BLE001
        return Outcome(False, "unprobeable", f"no transport for {provider}: {exc}"[:300])
    try:
        await asyncio.wait_for(transport.send(model=model, history=[transport.user_message("ping")], tool_schemas=[], max_tokens=1,
                                              timeout_s=timeout_s, system_prompt=""), timeout=timeout_s + 5)
        return Outcome(True, "ok")
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - any failure is an answer of "not yet"
        status = getattr(transport, "last_status", None)
        sig = classify.classify(status, str(exc), getattr(transport, "rate_headers", None) or {})
        if sig is not None:
            return Outcome(False, sig.kind, str(exc)[:300], sig.reset_at, sig.retry_after_s)
        return Outcome(False, "error", f"{type(exc).__name__}: {exc}"[:300])


_prober: Prober = default_prober


def set_prober(fn: Optional[Prober]) -> None:
    global _prober
    _prober = fn or default_prober


async def probe_once(*, force: bool = False, now: Optional[float] = None) -> dict:
    """Run one probe round if one is due (or `force`). {"ran": bool, "outcomes": [...], "state": the new state view}."""
    from bot import capacity

    claim = governor.probe_claim(force=force, now=now)
    if claim is None:
        return {"ran": False, "outcomes": [], "state": governor.get_state(now)}
    outcomes = []
    token = capacity.probing.set(True)            # the governor is not told twice: usage_limits must not count this call
    try:
        for t in claim["targets"]:
            try:
                out = await _prober(t["provider"], t["model"], claim["timeout_s"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                out = Outcome(False, "error", f"{type(exc).__name__}: {exc}"[:300])
            at = time.time() if now is None else now
            governor.probe_finish(out.ok, provider=t["provider"], model=t["model"], status=out.status, detail=out.detail,
                                  reset_at=out.reset_at, retry_after_s=out.retry_after_s, now=at)
            outcomes.append({"provider": t["provider"], "model": t["model"], "ok": out.ok, "status": out.status})
            if out.ok:
                break
    finally:
        capacity.probing.reset(token)
    return {"ran": True, "outcomes": outcomes, "state": governor.get_state(now)}


async def run_forever(stop_event: asyncio.Event) -> None:
    """The app's background loop: look every POLL_S seconds whether a probe is due. Does nothing while cloud-led."""
    logger.info("capacity recovery probe started")
    while not stop_event.is_set():
        try:
            await probe_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("capacity probe round failed", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_S)
        except asyncio.TimeoutError:
            pass
    logger.info("capacity recovery probe stopped")
