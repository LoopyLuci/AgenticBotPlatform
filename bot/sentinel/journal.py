"""The Sentinel's own record of what it saw and did.

Deliberately NOT the main database: the journal has to keep working when the
database is the thing that broke. It is an append-only JSON-lines file under
data/sentinel/, size-capped with one rotated generation, plus an in-memory
ring of recent entries for the dashboard.

alert() is the one way a Sentinel finding reaches a person: a log line at the
right level, a journal entry, and (for warnings and worse) a push
notification to paired phones — de-duplicated, so a condition that persists
for a day raises one alert, not one per check.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.sentinel")

SENTINEL_DIR = PROJECT_ROOT / "data" / "sentinel"
JOURNAL_MAX_BYTES = 5 * 1024 * 1024
RECENT_MAX = 500
# The same alert key is not re-sent more often than this.
REALERT_AFTER_S = 6 * 3600

LEVELS = ("info", "warning", "critical")

_lock = threading.Lock()
_recent: "collections.deque[dict[str, Any]]" = collections.deque(maxlen=RECENT_MAX)
_last_alert: dict[str, float] = {}


def journal_path() -> Path:
    return SENTINEL_DIR / "journal.jsonl"


def record(kind: str, message: str, *, level: str = "info", **data: Any) -> dict[str, Any]:
    """Appends one entry to the journal. Never raises: a Sentinel that can't
    write its journal (disk full, read-only mount) still does its job."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "level": level,
             "message": message, **({"data": data} if data else {})}
    with _lock:
        _recent.append(entry)
        try:
            path = journal_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > JOURNAL_MAX_BYTES:
                path.replace(path.with_suffix(".jsonl.1"))
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
    return entry


def recent(limit: int = 100, *, kind: Optional[str] = None, min_level: str = "info") -> list[dict[str, Any]]:
    floor = LEVELS.index(min_level) if min_level in LEVELS else 0
    with _lock:
        items = [e for e in _recent if LEVELS.index(e.get("level", "info")) >= floor and (kind is None or e["kind"] == kind)]
    return items[-limit:][::-1]


def load_recent_from_disk(limit: int = RECENT_MAX) -> None:
    """Seeds the in-memory ring from the journal file at startup, so the
    dashboard shows what happened before a restart (often the interesting part)."""
    path = journal_path()
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return
    with _lock:
        _recent.clear()
        for line in lines:
            try:
                _recent.append(json.loads(line))
            except ValueError:
                continue


def alert(key: str, message: str, *, level: str = "warning", notify: bool = True, **data: Any) -> bool:
    """Raises a finding. `key` identifies the condition (e.g. "db.integrity",
    "cve:PYSEC-2026-1"); while it keeps firing, people are told once per
    REALERT_AFTER_S. Returns True when this call actually alerted."""
    now = time.monotonic()
    with _lock:
        last = _last_alert.get(key)
        if last is not None and now - last < REALERT_AFTER_S:
            return False
        _last_alert[key] = now
    record("alert", message, level=level, key=key, **data)
    log = logger.critical if level == "critical" else logger.warning if level == "warning" else logger.info
    # CRITICAL writes a crash report (bot/diagnostics.py); keep that for real emergencies.
    log("sentinel: %s", message)
    if notify and level in ("warning", "critical"):
        _push(message)
    return True


def clear(key: str) -> None:
    """The condition behind `key` is gone: the next occurrence alerts again right away."""
    with _lock:
        was = _last_alert.pop(key, None)
    if was is not None:
        record("resolved", f"resolved: {key}", key=key)


def _push(message: str) -> None:
    try:
        from bot import push
        from bot import tasks as bg

        bg.spawn_soon(lambda: push.notify_new_message("ABP Sentinel", message), name="sentinel-push")
    except Exception:  # noqa: BLE001 — alerting must never break the check that raised it
        logger.debug("sentinel push skipped", exc_info=True)


def _reset_for_tests() -> None:
    with _lock:
        _recent.clear()
        _last_alert.clear()
