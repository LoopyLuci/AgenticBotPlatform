"""Capacity-change events: the hook LF5's digest reads ("cloud capacity is back", "we went local-only and why").

Every change of the capacity state (and every recovery probe outcome) is published here after it has been committed:

    events.recent(since=0)      the last events in this process, oldest first, each with a rising `seq`
    events.subscribe(fn)        fn(event) on every new event (a listener that raises is ignored)
    events.digest_lines(since)  the same events as short plain lines, for a digest

An event: {"seq", "ts", "kind", "actor", "from", "to", "reason"}. `kind` is `capacity.changed` when the effective state moved,
`capacity.recovered` when it returned to cloud-led, `capacity.probe` for a probe outcome that moved nothing. Events are also
put on the local work board's event feed (kind `capacity.<...>`), next to the `board.*` events, so one reader sees both.
The durable record is the governor's history table (`GET /api/capacity/history`); this ring is the live feed.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Any, Callable, Optional

_lock = threading.Lock()
_ring: collections.deque = collections.deque(maxlen=200)
_listeners: list[Callable[[dict], None]] = []
_seq = 0
PROBE_KINDS = ("probe_ok", "probe_failed", "probe_cap")


def subscribe(fn: Callable[[dict], None]) -> Callable[[], None]:
    """Call `fn(event)` for every event from now on. Returns the function that unsubscribes."""
    with _lock:
        _listeners.append(fn)

    def off() -> None:
        with _lock:
            if fn in _listeners:
                _listeners.remove(fn)

    return off


def recent(since: int = 0, limit: int = 100) -> list[dict]:
    with _lock:
        return [dict(e) for e in _ring if e["seq"] > since][-max(1, limit):]


def digest_lines(since: int = 0, limit: int = 20) -> list[str]:
    out = []
    for e in recent(since, limit):
        when = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        move = f"{e['from']} -> {e['to']}" if e.get("from") != e.get("to") else e.get("to", "")
        out.append(f"{when} {e['kind']}: {move} - {e.get('reason', '')}"[:240])
    return out


def emit_change(item: dict) -> Optional[dict]:
    """Publish one governor history entry. Called by the governor after its transaction committed."""
    global _seq
    old, new, kind = item.get("from"), item.get("to"), item.get("kind", "")
    if old != new:
        ekind = "capacity.recovered" if new == "cloud-led" else "capacity.changed"
    elif kind in PROBE_KINDS:
        ekind = "capacity.probe"
    else:
        return None
    with _lock:
        _seq += 1
        ev: dict[str, Any] = {"seq": _seq, "ts": item.get("ts") or time.time(), "kind": ekind, "actor": item.get("actor", ""),
                              "from": old, "to": new, "reason": item.get("reason", ""), "cause": kind}
        _ring.append(ev)
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(dict(ev))
        except Exception:  # noqa: BLE001 - a listener must never break the governor
            pass
    _to_board(ev)
    return ev


def _to_board(ev: dict) -> None:
    """Best effort: the local work board's feed gets the event too (a digest that already reads the board sees it)."""
    try:
        from bot import coordination

        for board in coordination.open_local_boards():
            board.publish(ev["kind"], {k: ev[k] for k in ("ts", "actor", "from", "to", "reason", "cause")})
    except Exception:  # noqa: BLE001
        pass
