"""Daily background pruning of the highest-volume, lowest-long-term-value
tables (jobs, telemetry_events, connections_log,
support_bot_classifications) — see config/backends.yaml's `retention`
block and bot/db.py's prune_old_data() for exactly what's touched and why.

Without this, a long-running server's database only ever grows: every
request appends a job/telemetry/connection-log row, nothing ever removes
one, and the only way to reclaim space today is a human finding the
dashboard's manual Vacuum button — which reclaims disk space after a
delete, but doesn't decide what to delete in the first place. This task
is the "what to delete" half; Vacuum (still manual, since it briefly locks
writes) stays a deliberate choice, not automated here.
"""

from __future__ import annotations

import asyncio
import logging

from bot.config import config
from bot import db

logger = logging.getLogger("bot.retention")

RUN_INTERVAL_S = 24 * 3600


async def run_once() -> None:
    retention_cfg = (config.current.get("retention") or {})
    if not retention_cfg.get("enabled", True):
        return
    days = int(retention_cfg.get("days", 90))
    if days <= 0:
        logger.warning("retention.days=%s is not positive — skipping this pass", days)
        return
    removed = db.prune_old_data(days)
    total = sum(removed.values())
    if total:
        logger.info("pruned %d row(s) older than %d days: %s", total, days, removed)


async def run_forever(stop_event: asyncio.Event) -> None:
    logger.info("retention pruning started (every %ds, config-driven)", RUN_INTERVAL_S)
    while not stop_event.is_set():
        try:
            await run_once()
        except Exception:
            logger.exception("retention pruning pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RUN_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
    logger.info("retention pruning stopped")
