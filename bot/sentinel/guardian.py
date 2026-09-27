"""Process supervisor: `python -m bot.sentinel.guardian [args for bot.main]`.

Runs `python -m bot.main` as a child and keeps it alive:

- a clean exit (code 0, e.g. a normal shutdown) ends the guardian too;
- any other exit, including the watchdog's EXIT_RESTART after a hang, is
  restarted after a backoff of 1, 2, 5, 10, 30 then 60 seconds. The backoff
  resets once a child has stayed up for STABLE_AFTER_S;
- more than MAX_CRASHES crashes inside CRASH_WINDOW_S means something
  restarting can't fix. The guardian stops and leaves the evidence in the
  journal. Before that point, bootguard's safe mode will already have rolled
  the config back.

The child gets ABP_SUPERVISED=1, which is what lets the watchdog exit a wedged
process instead of only reporting it. SIGINT and SIGTERM are forwarded, so
Ctrl+C, `docker stop` and service managers shut ABP down cleanly.

The desktop app supervises bot.main itself with the same policy, so this is
for the headless paths: scripts/run.*, Docker, and service installs.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections import deque
from typing import Optional

BACKOFF_S = (1, 2, 5, 10, 30, 60)
STABLE_AFTER_S = 600
MAX_CRASHES = 10
CRASH_WINDOW_S = 1800


def _log(msg: str) -> None:
    print(f"[guardian] {msg}", file=sys.stderr, flush=True)
    try:
        from bot.sentinel import journal

        journal.record("guardian", msg, level="warning")
    except Exception:  # noqa: BLE001 — the guardian must work even if bot/ can't be imported
        pass


def supervise(argv: Optional[list[str]] = None, *, python: str = sys.executable, module: str = "bot.main",
              backoff: tuple[float, ...] = BACKOFF_S) -> int:
    import os

    cmd = [python, "-m", module, *(argv or [])]
    env = dict(os.environ, ABP_SUPERVISED="1", ABP_SUPERVISOR_PID=str(os.getpid()))
    crashes: deque[float] = deque()
    child: Optional[subprocess.Popen] = None
    stopping = False

    def _set_child(c: subprocess.Popen) -> None:
        nonlocal child
        child = c

    def _forward(signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _forward)
        except (ValueError, OSError):
            pass
    try:
        return _loop(cmd, env, backoff, crashes, lambda: stopping, lambda c: _set_child(c))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _loop(cmd, env, backoff, crashes, is_stopping, set_child) -> int:
    attempt = 0
    while True:
        started = time.monotonic()
        child = subprocess.Popen(cmd, env=env)
        set_child(child)
        code = child.wait()
        uptime = time.monotonic() - started
        if is_stopping() or code == 0:
            return 0
        now = time.monotonic()
        crashes.append(now)
        while crashes and now - crashes[0] > CRASH_WINDOW_S:
            crashes.popleft()
        if len(crashes) > MAX_CRASHES:
            _log(f"bot.main crashed {len(crashes)} times in {CRASH_WINDOW_S // 60} min — giving up (last exit {code})")
            return code or 1
        if uptime >= STABLE_AFTER_S:
            attempt = 0
        delay = backoff[min(attempt, len(backoff) - 1)]
        attempt += 1
        _log(f"bot.main exited with {code} after {uptime:.0f}s — restarting in {delay}s")
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not is_stopping():
            time.sleep(0.2)
        if is_stopping():
            return 0


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1:]))
