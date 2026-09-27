"""Automatic error and bug discovery.

A logging handler on the root logger fingerprints every WARNING-with-a-
traceback, ERROR and CRITICAL record as it happens: the exception type plus
the innermost frame inside ABP's own code, or, for plain messages, the logger
name plus the message with numbers, ids, paths and quoted values normalised
away. The same bug therefore collapses into one "issue" however many times it
fires and whatever request ids it carries.

Each issue tracks its count, first and last sighting, a recent rate and one
sample traceback (redacted). The Sentinel surfaces:

- new issues (a signature never seen before on this install), and
- spikes (an issue firing far above its own normal rate).

With `sentinel.bug_hunter.kanban_instance_id` set, a new issue also becomes a
card on that bot instance's "sentinel" kanban board. An auto-manage agent
watching the board (bot/auto_manage.py) can then investigate it, with the same
tools and approval gating as any other agent turn. It never merges a fix on
its own.

Issues persist to data/sentinel/issues.json, so "new" means new to this
install, not new since the last restart.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from bot.envfile import CODE_ROOT
from bot.sentinel import journal

ISSUES_PATH = journal.SENTINEL_DIR / "issues.json"
MAX_ISSUES = 2000
SPIKE_WINDOW_S = 600
SPIKE_MIN = 20

_lock = threading.Lock()
_issues: dict[str, dict[str, Any]] = {}
_dirty = False
_loaded = False
_new_since_report: list[str] = []

_NORMALISE = [
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"(?:[A-Za-z]:)?[\\/][^\s:]+"), "<path>"),
    (re.compile(r"\b\d+(?:\.\d+)*\b"), "<n>"),
]


def _normalise(text: str) -> str:
    text = text.splitlines()[0] if text else ""
    for pat, rep in _NORMALISE:
        text = pat.sub(rep, text)
    return text[:160]


def _our_frame(tb) -> Optional[str]:
    """The innermost traceback frame inside ABP's code (not the stdlib or a
    dependency) — where a fix would go."""
    root = str(CODE_ROOT)
    best = None
    for frame in traceback.extract_tb(tb):
        fn = frame.filename
        if fn.startswith(root) and "site-packages" not in fn and ".venv" not in fn:
            rel = os.path.relpath(fn, root).replace("\\", "/")
            best = f"{rel}:{frame.name}"
    return best


def signature(record: logging.LogRecord) -> str:
    if record.exc_info and record.exc_info[1] is not None:
        etype, _exc, tb = record.exc_info
        where = _our_frame(tb) or record.name
        return f"{etype.__name__} @ {where}"
    return f"{record.name}: {_normalise(record.getMessage())}"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(ISSUES_PATH.read_text(encoding="utf-8"))
        _issues.update(data.get("issues", {}))
    except (OSError, ValueError):
        pass


def observe(record: logging.LogRecord) -> None:
    global _dirty
    sig = signature(record)
    now = time.time()
    sample = None
    if record.exc_info and record.exc_info[1] is not None:
        sample = "".join(traceback.format_exception(*record.exc_info))[-4000:]
    with _lock:
        _load()
        issue = _issues.get(sig)
        if issue is None:
            if len(_issues) >= MAX_ISSUES:
                oldest = min(_issues, key=lambda k: _issues[k]["last_seen"])
                del _issues[oldest]
            issue = _issues[sig] = {"signature": sig, "level": record.levelname, "logger": record.name,
                                    "count": 0, "first_seen": now, "last_seen": now, "recent": [],
                                    "message": record.getMessage()[:500], "sample": None, "status": "open"}
            _new_since_report.append(sig)
        issue["count"] += 1
        issue["last_seen"] = now
        issue["recent"] = [t for t in issue["recent"] if now - t < SPIKE_WINDOW_S][-200:] + [now]
        if issue.get("status") == "resolved":
            issue["status"] = "regressed"
            _new_since_report.append(sig)
        if sample and not issue.get("sample"):
            issue["sample"] = sample
        _dirty = True


class BugHunterHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR and not record.exc_info:
            return  # plain warnings are conditions, not bugs
        if record.name.startswith("bot.sentinel"):
            return  # never fingerprint our own alerts
        try:
            observe(record)
        except Exception:  # noqa: BLE001 — a logging handler must never raise into the caller
            pass


_handler: Optional[BugHunterHandler] = None


def install() -> BugHunterHandler:
    global _handler
    if _handler is None:
        _handler = BugHunterHandler()
        logging.getLogger().addHandler(_handler)
    return _handler


def flush() -> None:
    global _dirty
    with _lock:
        if not _dirty:
            return
        data = {"issues": _issues}
        _dirty = False
    try:
        from bot.diagnostics import redact

        text = redact(json.dumps(data, default=str))
        ISSUES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ISSUES_PATH.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(ISSUES_PATH)
    except OSError:
        pass


def issues(limit: int = 100, *, status: Optional[str] = None) -> list[dict[str, Any]]:
    with _lock:
        _load()
        items = [dict(i, recent_rate=len(i["recent"])) for i in _issues.values()
                 if status is None or i.get("status") == status]
    for i in items:
        i.pop("recent", None)
    return sorted(items, key=lambda i: i["last_seen"], reverse=True)[:limit]


def set_status(sig: str, status: str) -> bool:
    global _dirty
    with _lock:
        _load()
        if sig not in _issues or status not in ("open", "resolved", "ignored"):
            return False
        _issues[sig]["status"] = status
        _dirty = True
    return True


def review(kanban_instance_id: Optional[int] = None) -> dict[str, Any]:
    """Called by the Sentinel each cycle: alerts on new/regressed and spiking
    issues, optionally files kanban cards, and persists the table."""
    with _lock:
        new = list(dict.fromkeys(_new_since_report))
        _new_since_report.clear()
        now = time.time()
        spikes = [i["signature"] for i in _issues.values()
                  if i.get("status") != "ignored" and len([t for t in i["recent"] if now - t < SPIKE_WINDOW_S]) >= SPIKE_MIN]
        snapshot = {s: dict(_issues[s]) for s in new + spikes if s in _issues}
    for sig in new:
        issue = snapshot.get(sig)
        if not issue or issue.get("status") == "ignored":
            continue
        verb = "regressed" if issue.get("status") == "regressed" else "new"
        journal.alert(f"bug:{sig}", f"{verb} error: {sig} — {issue['message'][:200]}", level="warning", notify=False)
        if kanban_instance_id:
            _file_card(kanban_instance_id, issue)
    for sig in spikes:
        journal.alert(f"bug-spike:{sig}", f"error spike: {sig} fired {len(snapshot[sig]['recent'])}x in "
                                           f"{SPIKE_WINDOW_S // 60} min", level="warning")
    flush()
    return {"new": new, "spikes": spikes}


def _file_card(instance_id: int, issue: dict[str, Any]) -> None:
    try:
        from bot import kanban
        from bot.diagnostics import redact

        text = redact(f"[sentinel] {issue['signature']}\n\n{issue['message']}\n\n{(issue.get('sample') or '')[-2500:]}\n\n"
                      "Investigate the root cause and propose a fix with a regression test. Do not merge it yourself.")
        kanban.add_card(instance_id, "sentinel", "todo", text)
    except Exception:  # noqa: BLE001 — filing a card is best effort
        logging.getLogger("bot.sentinel").debug("could not file a kanban card", exc_info=True)


def _reset_for_tests(path: Optional[Path] = None) -> None:
    global _loaded, _dirty, ISSUES_PATH
    with _lock:
        _issues.clear()
        _new_since_report.clear()
        _loaded = True
        _dirty = False
    if path is not None:
        ISSUES_PATH = path
