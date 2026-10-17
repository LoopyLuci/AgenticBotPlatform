"""Check-ins: an optional scheduled digest that only runs while cloud capacity exists.

The governor decides. cloud-led and hybrid: the check-in runs (digest built, marker moved, tokens counted, kept as `latest` and
published on the work board's event feed). local-only: nothing is built - the factory keeps working, the check-in is just queued
(`queued` in the manager state) and runs on the first poll after capacity returns, however recently the last one ran. A scheduled
check-in with nothing new (see digest.quiet) is skipped and costs no tokens. Off by default: manager.checkins.enabled.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from bot import manager
from bot.manager import digest, store

logger = logging.getLogger("bot.manager")


def _capacity_state() -> str:
    from bot.capacity import governor

    return governor.effective_state()


def run_once(*, force: bool = False, now: Optional[float] = None) -> dict:
    """One check-in. Returns {"status": "done" | "queued" | "quiet", ...}. `force` skips the quiet test, never the capacity gate."""
    now = time.time() if now is None else now
    state = _capacity_state()
    if state == "local-only":
        if not store.load().get("queued"):
            store.update(queued={"since": now, "reason": "local-only: cloud capacity is not available"})
        return {"status": "queued", "capacity": state, "reason": "local-only: queued until cloud capacity returns"}
    doc = digest.build(mark=False, fresh=True, now=now)
    if digest.quiet(doc) and not force:
        store.update(last_checkin_ts=now, queued=None)
        return {"status": "quiet", "capacity": state, "tokens": 0}
    mem = store.record_delivery(doc["generated"], doc["tokens"], checkin=True, latest=digest.to_text(doc, 3000))
    doc["accounting"] = digest._accounting(mem, doc["factory"])
    digest._cache.clear()
    _publish(doc)
    return {"status": "done", "capacity": state, "tokens": doc["tokens"], "digest": doc}


def _publish(doc: dict) -> None:
    try:
        from bot import coordination

        for board in coordination.open_local_boards():
            board.publish("manager.checkin", {"tokens": doc["tokens"], "escalations": doc["escalations"]["count"]})
    except Exception:  # noqa: BLE001
        pass


def due(now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    cfg = manager.settings()["checkins"]
    if not cfg.get("enabled"):
        return False
    mem = store.load()
    if mem.get("queued") and _capacity_state() != "local-only":
        return True                                    # capacity is back: the waiting check-in goes first
    return now - float(mem.get("last_checkin_ts") or 0) >= float(cfg["interval_s"])


def view() -> dict:
    mem = store.load()
    cfg = manager.settings()["checkins"]
    return {"enabled": bool(cfg.get("enabled")), "interval_s": cfg["interval_s"], "capacity": _capacity_state(),
            "queued": mem.get("queued"), "last_checkin_ts": mem.get("last_checkin_ts") or None, "count": mem.get("checkin_count", 0),
            "tokens_total": mem.get("tokens_total", 0), "recent": (mem.get("checkins") or [])[-5:], "latest": mem.get("latest")}


async def run_forever(stop_event: asyncio.Event) -> None:
    """The app's background loop. Does nothing (one config read a poll) while manager.checkins.enabled is false."""
    logger.info("manager check-ins started")
    while not stop_event.is_set():
        try:
            if due():
                res = await asyncio.to_thread(run_once)
                logger.info("manager check-in: %s", res.get("status"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("manager check-in failed", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(5.0, float(manager.settings()["checkins"]["poll_s"])))
        except asyncio.TimeoutError:
            pass
    logger.info("manager check-ins stopped")
