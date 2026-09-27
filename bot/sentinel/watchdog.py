"""Watches the process from the outside of its event loop.

A heartbeat coroutine ticks on the loop every second; a separate daemon
thread checks the tick. When the loop falls behind, the thread captures the
loop thread's live stack, which names the exact line doing the blocking. That
turns "the dashboard felt slow" into a pinpointed bug report, fingerprinted
by the bug hunter like any other error.

When the loop stops ticking for `hang_after_s`, the process is wedged. If a
supervisor is running it (ABP_SUPERVISED=1: the guardian, the desktop app or
a container), the watchdog writes every thread's stack to a crash report and
exits with EXIT_RESTART so the supervisor starts a fresh process. Without a
supervisor it only alerts; exiting would just leave ABP down.

It also samples resident memory and thread count once a minute and flags
sustained growth, the signature of a leak.

When a supervisor names itself (ABP_SUPERVISOR_PID) and then disappears (killed
hard, crashed), the process shuts itself down cleanly instead of living on as an
orphan that holds the dashboard port and blocks the next start.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from typing import Any, Callable, Optional

from bot.sentinel import journal

logger = logging.getLogger("bot.sentinel.watchdog")

EXIT_RESTART = 75  # EX_TEMPFAIL: "try again", what supervisors restart on
BEAT_S = 1.0


class Watchdog:
    def __init__(self, *, lag_warn_s: float = 2.0, hang_after_s: float = 180.0, memory_limit_mb: int = 4096) -> None:
        self.lag_warn_s = lag_warn_s
        self.hang_after_s = hang_after_s
        self.memory_limit_mb = memory_limit_mb
        self._last_beat = time.monotonic()
        self._loop_thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._beat_task: Optional[asyncio.Task] = None
        self.max_lag_s = 0.0
        self.stalls = 0
        self.samples: deque[tuple[float, float, int]] = deque(maxlen=24 * 60)  # (ts, rss_mb, threads)
        self._reported_stacks: set[str] = set()
        # Called (from the watchdog thread) when the supervisor disappears;
        # bot.main points it at its graceful-shutdown event.
        self.on_orphaned: Optional[Callable[[], None]] = None

    # ---- loop side
    async def _beat(self) -> None:
        while True:
            self._last_beat = time.monotonic()
            await asyncio.sleep(BEAT_S)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop_thread_id = threading.get_ident()
        self._last_beat = time.monotonic()
        self._beat_task = loop.create_task(self._beat(), name="sentinel-heartbeat")
        self._thread = threading.Thread(target=self._run, name="sentinel-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._beat_task is not None:
            self._beat_task.cancel()

    # ---- watchdog thread
    def _loop_stack(self) -> str:
        frame = sys._current_frames().get(self._loop_thread_id or -1)
        return "".join(traceback.format_stack(frame)) if frame is not None else "(loop thread stack unavailable)"

    def _supervisor_gone(self) -> bool:
        pid = os.environ.get("ABP_SUPERVISOR_PID", "")
        if not pid.isdigit():
            return False
        try:
            import psutil

            return not psutil.pid_exists(int(pid))
        except Exception:  # noqa: BLE001
            return False

    def _run(self) -> None:
        last_sample = 0.0
        last_parent_check = 0.0
        stalled_since: Optional[float] = None
        while not self._stop.wait(0.5):
            if time.monotonic() - last_parent_check >= 5:
                last_parent_check = time.monotonic()
                if self._supervisor_gone():
                    journal.record("orphaned", "supervisor process is gone; shutting down", level="warning")
                    logger.warning("supervisor process %s is gone; shutting down instead of running orphaned",
                                   os.environ.get("ABP_SUPERVISOR_PID"))
                    if self.on_orphaned is not None:
                        self.on_orphaned()
                    else:
                        import signal

                        signal.raise_signal(signal.SIGINT)
                    return
            lag = time.monotonic() - self._last_beat - BEAT_S
            self.max_lag_s = max(self.max_lag_s, lag)
            if lag > self.lag_warn_s and stalled_since is None:
                stalled_since = time.monotonic()
                self.stalls += 1
                self._report_block(lag)
            elif lag <= self.lag_warn_s:
                stalled_since = None
            if lag > self.hang_after_s:
                self._hang(lag)
            if time.monotonic() - last_sample >= 60:
                last_sample = time.monotonic()
                self._sample()

    def _report_block(self, lag: float) -> None:
        stack = self._loop_stack()
        # The innermost frame is where the loop is stuck; report each place once per run.
        lines = [ln for ln in stack.strip().splitlines() if ln.strip().startswith("File ")]
        where = lines[-1].strip() if lines else "?"
        if where in self._reported_stacks:
            return
        self._reported_stacks.add(where)
        logger.error("event loop blocked for %.1fs at %s\n%s", lag, where, stack[-3000:])

    def _hang(self, lag: float) -> None:
        dump = "\n\n".join(f"--- thread {tid} ---\n" + "".join(traceback.format_stack(f))
                           for tid, f in sys._current_frames().items())
        journal.record("hang", f"event loop unresponsive for {lag:.0f}s", level="critical", stacks=dump[-20000:])
        logger.critical("event loop unresponsive for %.0fs — all thread stacks:\n%s", lag, dump[-20000:])
        if os.environ.get("ABP_SUPERVISED") == "1":
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:  # noqa: BLE001
                    pass
            os._exit(EXIT_RESTART)
        # Unsupervised: alert once, then keep watching.
        self._last_beat = time.monotonic()

    def _sample(self) -> None:
        try:
            import psutil

            proc = psutil.Process()
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            threads = proc.num_threads()
        except Exception:  # noqa: BLE001
            return
        self.samples.append((time.time(), rss_mb, threads))
        if rss_mb > self.memory_limit_mb:
            journal.alert("watchdog.memory", f"resident memory {rss_mb:.0f} MB exceeds the {self.memory_limit_mb} MB limit",
                          level="warning")
        leak = self.leak_suspected()
        if leak:
            journal.alert("watchdog.leak", leak, level="warning")

    def leak_suspected(self) -> Optional[str]:
        """Steady growth across six hours: every hourly bucket above the one before,
        and at least 50% overall."""
        if len(self.samples) < 6 * 60:
            return None
        pts = list(self.samples)[-6 * 60:]
        hourly = [sum(p[1] for p in pts[i:i + 60]) / 60 for i in range(0, 360, 60)]
        if all(b > a for a, b in zip(hourly, hourly[1:])) and hourly[-1] > hourly[0] * 1.5:
            return f"memory grew steadily from {hourly[0]:.0f} to {hourly[-1]:.0f} MB over 6 hours (possible leak)"
        return None

    def status(self) -> dict[str, Any]:
        last = self.samples[-1] if self.samples else None
        return {"lag_s": round(max(0.0, time.monotonic() - self._last_beat - BEAT_S), 3),
                "max_lag_s": round(self.max_lag_s, 3), "stalls": self.stalls,
                "rss_mb": round(last[1], 1) if last else None, "threads": last[2] if last else None,
                "supervised": os.environ.get("ABP_SUPERVISED") == "1"}
