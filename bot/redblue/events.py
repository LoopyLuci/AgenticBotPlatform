"""Telemetry -> typed Event normalization and a windowed feed. No detection logic here."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Event:
    ts: float
    actor: str
    action: str
    target: str
    grant_result: str = "na"  # allowed / denied / na
    cell_id: str = ""
    resource: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _sandbox_ns(raw: dict) -> Event:
    pid = raw.get("pid")
    return Event(
        ts=float(raw.get("ts") or time.time()),
        actor=f"pid:{pid}" if pid is not None else "sandbox_ns",
        action=str(raw.get("kind", "")),
        target=str(raw.get("detail", "")),
        cell_id=str(raw.get("cell", "")),
        resource={"pid": pid} if pid is not None else {},
    )


def _tool_call(raw: dict) -> Event:
    args = raw.get("args") or raw.get("arguments") or raw.get("input") or {}
    target = ""
    if isinstance(args, dict):
        for k in ("path", "file_path", "url", "command", "cmd", "target"):
            if args.get(k):
                target = str(args[k])
                break
    res = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    return Event(
        ts=float(raw.get("ts") or raw.get("timestamp") or time.time()),
        actor=str(raw.get("agent") or raw.get("actor") or "agent"),
        action=str(raw.get("tool") or raw.get("name") or ""),
        target=target,
        grant_result=str(raw.get("grant_result", "na")),
        cell_id=str(raw.get("cell_id") or raw.get("cell") or ""),
        resource={"args": args if isinstance(args, dict) else {}, **({"result": res} if res else {})},
    )


def _grant_check(raw: dict) -> Event:
    ok = raw.get("allowed", raw.get("ok"))
    result = "na" if ok is None else ("allowed" if ok else "denied")
    return Event(
        ts=float(raw.get("ts") or time.time()),
        actor=str(raw.get("actor", "")),
        action=str(raw.get("action") or raw.get("permission") or "grant_check"),
        target=str(raw.get("target") or raw.get("resource") or ""),
        grant_result=result,
        cell_id=str(raw.get("cell_id", "")),
        resource={"reason": raw["reason"]} if raw.get("reason") else {},
    )


_MAPPERS = {"sandbox_ns": _sandbox_ns, "tool_call": _tool_call, "grant_check": _grant_check}


def normalize(raw: dict, source: str) -> Event:
    if source not in _MAPPERS:
        raise ValueError(f"unknown telemetry source {source!r}")
    if not isinstance(raw, dict):
        raise ValueError("raw event must be a dict")
    return _MAPPERS[source](raw)


class EventFeed:
    def __init__(self, maxlen: int = 5000) -> None:
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, event: Event) -> Event:
        with self._lock:
            self._events.append(event)
        return event

    def ingest(self, raw: dict, source: str) -> Event:
        return self.add(normalize(raw, source))

    def recent(self, n: int = 100, *, since: Optional[float] = None) -> list[Event]:
        with self._lock:
            rows: Any = list(self._events)
        if since is not None:
            rows = [e for e in rows if e.ts >= since]
        return rows[-n:] if n > 0 else []

    def __len__(self) -> int:
        return len(self._events)
