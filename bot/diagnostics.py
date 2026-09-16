"""Advanced diagnostics: local-only telemetry counters, structured crash
reports, and an exportable support bundle for bug reports.

Like bot/activity_log.py, this attaches ONE handler to the root logger
instead of asking every call site across the codebase to also remember to
report telemetry or write a crash report — every module's existing
logger.warning/.error/.critical call already routes through here for
free, the same "one handler, not a parallel logging system" rationale
activity_log.py's own docstring documents.

Nothing here ever leaves this machine on its own — see list_crash_reports()
and build_support_bundle() for how a human pulls this out to attach to a
bug report. There is no telemetry endpoint and no network call in this
module, deliberately.

CRITICAL is reserved in this codebase for genuinely-uncaught exceptions
(bot/main.py's sys.excepthook/threading.excepthook installs) and
unrecoverable startup failures — never for routine, already-self-healing
retries (those log at WARNING/ERROR and are counted, not reported) — so
crash reports stay a small, meaningful set instead of one per transient
hiccup.
"""

from __future__ import annotations

import json
import logging
import os
import platform as _platform
import sys
import threading
import time
import traceback
import zipfile
from collections import Counter, deque
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"
CRASH_DIR = LOG_DIR / "crash_reports"
BUNDLE_DIR = LOG_DIR / "support_bundles"
MAX_CRASH_REPORTS = 200
MAX_SUPPORT_BUNDLES = 20
MAX_RECENT_EVENTS = 500


def _read_app_version() -> str:
    try:
        conf_path = PROJECT_ROOT / "desktop-app" / "src-tauri" / "tauri.conf.json"
        return json.loads(conf_path.read_text(encoding="utf-8"))["version"]
    except Exception:
        return "unknown"


APP_VERSION = _read_app_version()


class _Telemetry:
    """In-memory, per-process counters and a small ring buffer of
    self-healing/crash events. Reset on process restart — this is a
    live-diagnostics view, not a historical database (crash reports and
    logs/bot.log are what persist across restarts)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_EVENTS)
        self.started_at = time.time()

    def increment(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] += by

    def record_event(self, category: str, detail: str) -> None:
        with self._lock:
            self._events.append({"ts": time.time(), "category": category, "detail": detail[:500]})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "started_at": self.started_at,
                "uptime_s": time.time() - self.started_at,
                "counters": dict(self._counters),
                "recent_events": list(self._events)[-100:],
            }


telemetry = _Telemetry()


def _next_report_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime()) + f"-{int((time.time() % 1) * 1_000_000):06d}"


def _prune_dir(directory: Path, pattern: str, keep: int) -> None:
    try:
        paths = sorted(directory.glob(pattern))
    except OSError:
        return
    for path in paths[: max(len(paths) - keep, 0)]:
        try:
            path.unlink()
        except OSError:
            pass


def write_crash_report(record: logging.LogRecord) -> Optional[str]:
    """Writes one structured JSON crash report for a CRITICAL log record —
    everything a bug report needs in one file: the traceback (if any),
    what else was happening right before it (recent Activity tab
    entries), and enough environment detail to reproduce. Never raises —
    a broken crash reporter must never be the thing that crashes the
    process it's trying to protect."""
    try:
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        report_id = _next_report_id()
        exc_text = None
        exc_type_name = None
        if record.exc_info and record.exc_info[0] is not None:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
            exc_type_name = record.exc_info[0].__name__

        from bot import activity_log

        report = {
            "id": report_id,
            "ts": record.created,
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "thread": record.threadName,
            "exception_type": exc_type_name,
            "traceback": exc_text,
            "app_version": APP_VERSION,
            "python_version": sys.version,
            "platform": _platform.platform(),
            "recent_activity": activity_log.recent(limit=50),
        }
        path = CRASH_DIR / f"{report_id}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        _prune_dir(CRASH_DIR, "*.json", MAX_CRASH_REPORTS)
        telemetry.increment("crash_reports.written")
        telemetry.record_event("crash", record.getMessage())
        return report_id
    except Exception:
        logging.getLogger("bot.diagnostics").debug("failed to write crash report", exc_info=True)
        return None


class _DiagnosticsHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno >= logging.WARNING:
                telemetry.increment(f"log.{record.levelname.lower()}")
                telemetry.increment(f"log.{record.levelname.lower()}.{record.name}")
            if record.levelno >= logging.CRITICAL:
                write_crash_report(record)
        except Exception:
            pass  # a broken diagnostics handler must never take down logging itself


_handler: Optional[_DiagnosticsHandler] = None


def install(level: int = logging.WARNING) -> _DiagnosticsHandler:
    """Idempotent — safe to call more than once, matching
    activity_log.install()'s own contract."""
    global _handler
    if _handler is not None:
        return _handler
    _handler = _DiagnosticsHandler()
    _handler.setLevel(level)
    logging.getLogger().addHandler(_handler)
    return _handler


def list_crash_reports(limit: int = 50) -> list[dict[str, Any]]:
    try:
        paths = sorted(CRASH_DIR.glob("*.json"), reverse=True)[:limit]
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "id": data.get("id", path.stem),
            "ts": data.get("ts"),
            "iso_time": data.get("iso_time"),
            "level": data.get("level"),
            "logger": data.get("logger"),
            "message": data.get("message"),
            "exception_type": data.get("exception_type"),
        })
    return out


def get_crash_report(report_id: str) -> Optional[dict[str, Any]]:
    # report_id feeds a filename below — reject anything that could escape
    # CRASH_DIR (a path separator or a ".." segment) before it ever
    # touches the filesystem, since this is reachable from a dashboard route.
    if not report_id or "/" in report_id or "\\" in report_id or ".." in report_id:
        return None
    path = CRASH_DIR / f"{report_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "app_version": APP_VERSION,
        "python_version": sys.version.split()[0],
        "platform": _platform.platform(),
        "processor": _platform.processor(),
        "pid": os.getpid(),
    }
    try:
        import psutil

        proc = psutil.Process()
        info["memory_rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
        info["cpu_percent"] = proc.cpu_percent(interval=0.1)
        disk = psutil.disk_usage(str(PROJECT_ROOT))
        info["disk_free_gb"] = round(disk.free / (1024 ** 3), 2)
    except Exception:
        pass  # psutil is optional — the rest of the info is still useful without it
    return info


def build_support_bundle() -> Path:
    """Zips everything useful for a bug report into one file: system
    info, a telemetry snapshot, the most recent crash reports, and the
    tail of bot.log — nothing here that a human couldn't already see
    themselves in the dashboard or the logs/ directory, and nothing sent
    anywhere automatically. Returns the path to the written zip."""
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    bundle_path = BUNDLE_DIR / f"support-bundle-{_next_report_id()}.zip"

    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("system_info.json", json.dumps(system_info(), indent=2, default=str))
        zf.writestr("telemetry.json", json.dumps(telemetry.snapshot(), indent=2, default=str))
        for report in list_crash_reports(limit=20):
            full = get_crash_report(report["id"])
            if full is not None:
                zf.writestr(f"crash_reports/{report['id']}.json", json.dumps(full, indent=2, default=str))
        log_path = LOG_DIR / "bot.log"
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
                zf.writestr("bot_log_tail.txt", "\n".join(lines))
            except OSError:
                pass

    _prune_dir(BUNDLE_DIR, "support-bundle-*.zip", MAX_SUPPORT_BUNDLES)
    return bundle_path
