"""The manager's own small record: the last-digest marker, the check-in log and the token accounting.

One JSON file (data/agent/manager/state.json), rewritten atomically. Only digests that were *delivered* (marked or checked in) count
toward the manager's token total; a peek costs the reader tokens too but does not move the marker.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.RLock()
KEEP_CHECKINS = 30


def _path() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("manager") / "state.json"


def _blank() -> dict:
    return {"last_digest_ts": 0.0, "digests": 0, "tokens_total": 0, "checkins": [], "checkin_count": 0, "last_checkin_ts": 0.0,
            "queued": None, "latest": None, "first_ts": time.time()}


def load() -> dict:
    with _lock:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            return {**_blank(), **data} if isinstance(data, dict) else _blank()
        except (OSError, ValueError):
            return _blank()


def save(data: dict) -> None:
    with _lock:
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)


def update(**fields: Any) -> dict:
    with _lock:
        data = load()
        data.update(fields)
        save(data)
        return data


def record_delivery(ts: float, tokens: int, *, checkin: bool = False, latest: Any = None) -> dict:
    """A digest reached its reader: move the marker to its time, add its tokens, log it when it was a scheduled check-in."""
    with _lock:
        data = load()
        data["last_digest_ts"] = max(float(data.get("last_digest_ts") or 0), float(ts))
        data["digests"] = int(data.get("digests", 0)) + 1
        data["tokens_total"] = int(data.get("tokens_total", 0)) + int(tokens)
        if checkin:
            data["checkin_count"] = int(data.get("checkin_count", 0)) + 1
            data["last_checkin_ts"] = float(ts)
            data["checkins"] = (list(data.get("checkins") or []) + [{"ts": ts, "tokens": int(tokens)}])[-KEEP_CHECKINS:]
            data["queued"] = None
            data["latest"] = latest
        save(data)
        return data
