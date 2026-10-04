"""The memory fabric's background work (bot/main.py runs run_forever):

    every minute          a person's edits to the vault's memory files read back; the files rewritten
    every auto_fetch_minutes (default 20)   each enabled source that is due synced (one at a time)
    once a day            the daily close: each source's leftover chunks sealed into a summary, the vault rewritten
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)
_last = {"close": 0.0}


def tick(now: float | None = None) -> dict:
    from bot.memoryfabric import knowledge, sources, store, vault
    now = now or time.time()
    st = store.settings()
    out: dict = {"synced": [], "closed": 0}
    if st.get("vault", True):
        out["vault"] = vault.sync_memories()
        from bot.memoryfabric import rules
        out["goals"] = rules.read_goals_file()
    minutes = float(st.get("auto_fetch_minutes") or 0)
    if minutes > 0:
        for sid in sources.due(minutes):
            r = sources.sync(sid, log=logger.info)
            out["synced"].append({"id": sid, "stage": r["stage"]})
    if now - _last["close"] >= 86400:
        _last["close"] = now
        for s in sources.listing():
            out["closed"] += knowledge.seal(s["id"], force=True)
        if st.get("vault", True):
            vault.write_all()
    return out


async def run_forever(stop_event: asyncio.Event) -> None:
    try:                                   # let ABP come up first
        await asyncio.wait_for(stop_event.wait(), timeout=50)
        return
    except asyncio.TimeoutError:
        pass
    _last["close"] = time.time() - 86400 + 3600        # the first daily close an hour after start
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001
            logger.exception("memory fabric tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
