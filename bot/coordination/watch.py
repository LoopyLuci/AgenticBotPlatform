"""Stuck-work check: a periodic look at the work board for work that has quietly stopped moving.

Every `coordination.stuck_watch_interval_s` (default 60 s) it reads the board's summary and raises a warning - an entry in
the Sentinel journal (the activity log shown in the dashboard) plus the Sentinel's existing push path to paired phones, and
a log line - for:

- `lease`    a claimed task whose lease is about to run out (an owner that heartbeats keeps it far from that);
- `failed`   a task that failed after using all its attempts (typically "lease expired N times"; each task once);
- `idle`     runnable tasks waiting while agents sit idle, for longer than a grace period (workers between two steps are
             idle for a moment; that is not stuck);
- `locks`    locks held by an agent that is gone (no sign of life for three heartbeat intervals).

Each condition is reported at most once per `coordination.stuck_alert_every_s` (default 30 min) however often it is seen.
`coordination.stuck_watch: false` switches the whole thing off. It only reads the board; it never changes it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from bot import coordination

logger = logging.getLogger("bot.coordination.watch")

FAILED_LOOKBACK_S = 24 * 3600
IDLE_GRACE_S = 60.0


def watch_settings() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    try:
        from bot.config import config

        cfg = dict(config.current.get("coordination") or {})
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": bool(cfg.get("stuck_watch", True)),
            "interval_s": max(5.0, float(cfg.get("stuck_watch_interval_s") or 60)),
            "alert_every_s": max(1.0, float(cfg.get("stuck_alert_every_s") or 1800))}


def _journal_emit(key: str, message: str, bucket: int) -> None:
    from bot.sentinel import journal

    journal.alert(f"coordination.{key}@{bucket}", message, level="warning")


class StuckWatch:
    def __init__(self, *, emit: Optional[Callable[[str, str, int], None]] = None, clock: Callable[[], float] = time.time,
                 alert_every_s: Optional[float] = None, idle_grace_s: float = IDLE_GRACE_S):
        self.emit = emit or _journal_emit
        self.clock = clock
        self.alert_every_s = alert_every_s if alert_every_s is not None else watch_settings()["alert_every_s"]
        self.idle_grace_s = idle_grace_s
        self._sent: dict[str, float] = {}             # condition key -> when it was last reported
        self._idle_since: dict[str, float] = {}       # board -> since when runnable work has waited next to idle agents
        self._failed_seen: set[str] = set()

    # ------------------------------------------------------------------ checks
    def findings(self, board) -> list[tuple[str, str]]:
        """(key, message) for everything stuck right now, before rate limiting."""
        now = self.clock()
        summary = board.summary()
        out: list[tuple[str, str]] = []
        agents = summary.get("agents") or {}
        idle, gone = list(agents.get("idle") or []), list(agents.get("gone") or [])
        waiting_now: set[str] = set()
        for name, b in sorted((summary.get("boards") or {}).items()):
            for e in b.get("expiring") or []:
                out.append((f"lease:{e['task']}", f"lease of task '{e.get('title')}' on {name} (owner {e.get('owner')}) "
                            f"runs out in {max(0, int(e.get('in_ms') or 0) // 1000)} s and the owner has not renewed it"))
            runnable = int((b.get("counts") or {}).get("open", 0)) - len(b.get("blocked") or [])
            if runnable > 0 and idle:
                waiting_now.add(name)
                since = self._idle_since.setdefault(name, now)
                if now - since >= self.idle_grace_s:
                    out.append((f"idle:{name}", f"{runnable} runnable task(s) on {name} are waiting while "
                                f"{len(idle)} agent(s) are idle ({', '.join(idle[:3])}) - no one is taking them"))
        for name in list(self._idle_since):
            if name not in waiting_now:
                del self._idle_since[name]
        for t in board.list_tasks(None, state="failed", limit=200):
            if t["id"] in self._failed_seen or now - (t.get("updated_at") or 0) / 1000.0 > FAILED_LOOKBACK_S:
                continue
            if t.get("attempts", 0) >= t.get("max_attempts", 3):
                out.append((f"failed:{t['id']}", f"task '{t['title']}' on {t['board']} failed after {t['attempts']} "
                            f"attempt(s): {t.get('note') or 'no reason recorded'}"))
        if gone:
            for lk in board.list_locks():
                if lk.get("owner") in gone:
                    out.append((f"locks:{lk.get('owner')}:{lk.get('resource')}",
                                f"lock {lk.get('resource')} is held by {lk.get('owner')}, which is gone"))
        return out

    def run_once(self, board) -> list[str]:
        """Check once and report what is new (rate limited); returns the messages reported."""
        now = self.clock()
        reported: list[str] = []
        for key, message in self.findings(board):
            last = self._sent.get(key)
            if last is not None and now - last < self.alert_every_s:
                continue
            self._sent[key] = now
            if key.startswith("failed:"):
                self._failed_seen.add(key.split(":", 1)[1])
            logger.warning("work board: %s", message)
            try:
                self.emit(key.split(":", 1)[0], message, int(now // self.alert_every_s))
            except Exception:  # noqa: BLE001 - alerting must never stop the check
                logger.debug("could not raise the alert", exc_info=True)
            reported.append(message)
        for key in [k for k, t in self._sent.items() if now - t > 10 * self.alert_every_s]:
            del self._sent[key]
        return reported


async def run_forever(stop_event: asyncio.Event) -> None:
    watch = StuckWatch()
    logger.info("work board stuck-work check started")
    while not stop_event.is_set():
        cfg = watch_settings()
        if cfg["enabled"] and coordination.enabled():
            watch.alert_every_s = cfg["alert_every_s"]
            try:
                board = await coordination.aget_board()
                await asyncio.to_thread(watch.run_once, board)
            except Exception as exc:  # noqa: BLE001 - an unreachable board is not a finding of this check
                logger.debug("stuck-work check skipped: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cfg["interval_s"])
        except asyncio.TimeoutError:
            pass
    logger.info("work board stuck-work check stopped")
