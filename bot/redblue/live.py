"""The live WatchDog: passive, propose-only monitoring of what ABP's agents actually do.

Two in-process sources feed it, both from the native agent runtime:
    tool_call    every tool an agent asks to run (bot/agent_runtime/tools.execute_tool), arguments trimmed to what the
                 detector reads (paths, URLs, commands) - never file contents
    grant_check  every permission decision on a tool call (bot/agent_runtime/permissions.evaluate): allow / deny / ask

`emit()` only puts the record on a bounded queue and returns; a background thread runs the rule detector, so an agent's tool
call never waits on detection, and a full queue drops (and counts) records instead of blocking. Incidents go to the
IncidentJournal (data/redblue/incidents.jsonl), which GET /api/redblue/summary reads.

PROPOSE-ONLY, by construction: the WatchDog is built with no containment handlers, nothing here calls `contain()`, and no
setting adds a handler. Proposed containments are only ever shown (dry-run) by the summary. `redblue.watchdog.enabled: false`
turns the feed off.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("bot.redblue.live")

QUEUE_MAX = 10000
_ARG_KEYS = ("path", "file_path", "url", "command", "cmd", "target", "cwd", "pattern", "glob")
_ARG_CHARS = 500

_lock = threading.Lock()
_queue: "queue.Queue[tuple[dict, str]]" = queue.Queue(maxsize=QUEUE_MAX)
_wd: Any = None
_thread: Optional[threading.Thread] = None
_stats = {"received": 0, "observed": 0, "dropped": 0, "errors": 0, "incidents": 0, "started": 0.0}


def enabled() -> bool:
    """`ABP_REDBLUE_WATCHDOG=off` (the test session sets it) wins over `redblue.watchdog.enabled` (default on)."""
    if os.environ.get("ABP_REDBLUE_WATCHDOG", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    try:
        from bot.config import config

        cfg = (config.current.get("redblue") or {}).get("watchdog") or {}
        return cfg.get("enabled", True) is not False
    except Exception:  # noqa: BLE001 - config trouble must never break a tool call
        return True


def watchdog():
    """The process-wide WatchDog: journal on, NO containment handlers (propose-only)."""
    global _wd
    with _lock:
        if _wd is None:
            from bot.redblue import journal
            from bot.redblue.watchdog import WatchDog

            _wd = WatchDog(journal=journal.default_journal(), handlers={})
        return _wd


def _worker() -> None:
    while True:
        raw, source = _queue.get()
        try:
            inc = watchdog().observe(raw, source)
            with _lock:
                _stats["observed"] += 1
                if inc is not None:
                    _stats["incidents"] += 1
        except Exception:  # noqa: BLE001 - one bad record must never stop the monitor
            with _lock:
                _stats["errors"] += 1
            logger.debug("live WatchDog: could not observe a %s record", source, exc_info=True)
        finally:
            _queue.task_done()


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="redblue-watchdog", daemon=True)
            _thread.start()
            _stats["started"] = time.time()


def _trim(args: Any) -> dict:
    if not isinstance(args, dict):
        return {}
    return {k: str(args[k])[:_ARG_CHARS] for k in _ARG_KEYS if args.get(k) not in (None, "")}


def emit(raw: dict, source: str) -> None:
    """Hand one record to the monitor. Never raises, never blocks."""
    try:
        if not enabled():
            return
        _ensure_thread()
        with _lock:
            _stats["received"] += 1
        _queue.put_nowait((raw, source))
    except queue.Full:
        with _lock:
            _stats["dropped"] += 1
    except Exception:  # noqa: BLE001
        logger.debug("live WatchDog: emit failed", exc_info=True)


def tool_call(name: str, tool_input: Any, *, instance_id: Optional[int] = None, workspace: Any = None) -> None:
    emit({"ts": time.time(), "tool": name, "args": _trim(tool_input), "agent": _actor(instance_id),
          "workspace": str(workspace or "")}, "tool_call")


def grant_check(name: str, tool_input: Any, verdict: Any, *, instance_id: Optional[int] = None) -> None:
    decision = str(getattr(verdict, "decision", "") or "")
    allowed = True if decision == "allow" else False if decision == "deny" else None      # ask / default: no grant yet
    args = _trim(tool_input)
    target = next((args[k] for k in ("path", "file_path", "url", "command", "cmd", "target") if args.get(k)), "")
    emit({"ts": time.time(), "actor": _actor(instance_id), "action": name, "target": target, "allowed": allowed,
          "reason": str(getattr(verdict, "reason", "") or decision)[:200]}, "grant_check")


def _actor(instance_id: Optional[int]) -> str:
    return f"instance:{instance_id}" if instance_id is not None else "agent"


def drain(timeout: float = 5.0) -> bool:
    """Wait until every queued record is observed (tests, a clean shutdown). True when the queue emptied in time."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _queue.unfinished_tasks


def stats() -> dict:
    with _lock:
        out = dict(_stats)
    out.update(enabled=enabled(), queued=_queue.qsize(), handlers=len(getattr(_wd, "handlers", {}) or {}) if _wd else 0,
               mode="propose-only")
    return out


def _reset_for_tests() -> None:
    global _wd
    drain(2.0)
    with _lock:
        _wd = None
        for k in _stats:
            _stats[k] = 0.0 if k == "started" else 0
