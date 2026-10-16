"""The capacity governor (LF3): one system-wide state - cloud-led, hybrid or local-only - set automatically from provider usage
limits and by hand, obeyed by model routing, plus handoff packs so a different model can resume in-flight work.

    classify.py   is this failure "out of capacity"? (HTTP status + body/message + headers -> a Signal)
    governor.py   the state machine, its persistence and history
    routing.py    what the state means for choosing a model
    handoff.py    per-task packs (goal, plan, files, diff, tests, notes, last error)

The functions below are what the rest of ABP calls; they never raise and cost nothing when the governor is off.
Docs: docs/capacity-governor.md.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from bot.capacity import classify as _classify
from bot.capacity import governor
from bot.capacity.governor import effective_state  # noqa: F401
from bot.capacity.governor import CLOUD_LED, HYBRID, LOCAL_ONLY, STATES  # noqa: F401

logger = logging.getLogger("bot.capacity")


def observe_failure(provider: str, model: str, status: Optional[int], text: Any, headers: Optional[dict] = None, *,
                    local: bool = False, now: Optional[float] = None) -> Optional[dict]:
    """A provider call failed. Classify it and, when it means out of capacity, feed the governor. A local provider's 429 is
    that server being busy, not a cloud allowance, so it is ignored. Returns the new state view when the automatic state moved."""
    if local:
        return None
    try:
        signal = _classify.classify(status, text, headers, now=now)
        if signal is None:
            return None
        return governor.observe(signal, provider=provider, model=model, now=now)
    except Exception:  # noqa: BLE001 - detection must never cost a reply
        logger.debug("capacity: could not record a failure", exc_info=True)
        return None


def observe_text(source: str, text: Any, *, now: Optional[float] = None) -> Optional[dict]:
    """A CLI / external agent backend failed with this text (Claude Code's 'usage limit reached' lands here)."""
    return observe_failure(source, "", None, text, None, now=now)


def observe_ok(provider: str, model: str, *, local: bool = False) -> None:
    """A provider call worked (ends a probation)."""
    if local:
        return
    try:
        governor.observe_success(provider, model)
    except Exception:  # noqa: BLE001
        logger.debug("capacity: could not record a success", exc_info=True)
