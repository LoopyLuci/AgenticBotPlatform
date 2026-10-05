"""The numbers that bound what the gate is allowed to do to this machine.

Every one of them is here, and every one of them is overridable from the
environment, because the point of the module is that the gate's worst case is a
number somebody chose rather than a number nobody did:

    ABP_GATE_MAX_INSTANCES       how many instances may be alive at once (8)
    ABP_GATE_RESTART_LIMIT       restarts of ONE instance per window (3)
    ABP_GATE_RESTART_WINDOW_S    how long that window is, in seconds (600)
    ABP_GATE_RESTART_BACKOFF_S   seconds to wait before restart 1, 2, 3, ... (5, 20, 60)
    ABP_GATE_WATCH_INTERVAL_S    how often the watcher looks (5)
    ABP_GATE_INSTANCE_LIFETIME   `gate` (instances die with the gate) or
                                 `detached` (the active one survives it)

The defaults are what protects a person using this machine: the first version
of the gate had none of them, restarted whatever was unhealthy every five
seconds, and took the machine down with thousands of orphaned interpreters.
The tests run with the same code and much smaller numbers, which is the point of
having them in one place.
"""

from __future__ import annotations

import os

#: Generous for real use (one production, one standby mid-swap, one sandbox per
#: agent) and small enough that a bug which starts instances without stopping
#: them shows up as a refusal instead of as an out-of-memory machine.
DEFAULT_MAX_INSTANCES = 8
#: A crash loop needs to be a crash loop - three restarts, not one unlucky
#: crash - before the watcher stops and says so.
DEFAULT_RESTART_LIMIT = 3
DEFAULT_RESTART_WINDOW_S = 600.0
#: Exponential, and then some: 5s, 20s, 60s, 120s, ... so a code root that cannot
#: boot is retried rarely rather than constantly.
DEFAULT_RESTART_BACKOFF_S = (5.0, 20.0, 60.0, 120.0, 300.0)
DEFAULT_WATCH_INTERVAL_S = 5.0


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: {raw!r} is not a number") from exc


def max_instances() -> int:
    return max(1, int(_number("ABP_GATE_MAX_INSTANCES", DEFAULT_MAX_INSTANCES)))


def restart_limit() -> int:
    return max(1, int(_number("ABP_GATE_RESTART_LIMIT", DEFAULT_RESTART_LIMIT)))


def restart_window_s() -> float:
    return max(1.0, _number("ABP_GATE_RESTART_WINDOW_S", DEFAULT_RESTART_WINDOW_S))


def restart_backoff_s() -> tuple[float, ...]:
    """Seconds before the nth restart (n counted from 1), clamped to the last
    value. A list, not a multiplier, so the shape of the backoff is a decision
    that can be read in one place."""
    raw = os.environ.get("ABP_GATE_RESTART_BACKOFF_S", "").strip()
    if not raw:
        return DEFAULT_RESTART_BACKOFF_S
    out: list[float] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(max(0.0, float(chunk)))
        except ValueError as exc:
            raise ValueError(f"ABP_GATE_RESTART_BACKOFF_S: {chunk!r} is not a number of seconds") from exc
    return tuple(out) or DEFAULT_RESTART_BACKOFF_S


def watch_interval_s() -> float:
    return max(0.1, _number("ABP_GATE_WATCH_INTERVAL_S", DEFAULT_WATCH_INTERVAL_S))


def instance_lifetime() -> str:
    raw = os.environ.get("ABP_GATE_INSTANCE_LIFETIME", "").strip().lower()
    return raw if raw in ("gate", "detached") else "gate"