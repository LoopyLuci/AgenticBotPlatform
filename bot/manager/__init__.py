"""The manager channel (LF5): Claude supervises the local factory cheaply. See docs/local-manager.md.

    escalations.py  one view of everything that needs a decision (blocked/review tasks, the push queue, overrides, repeated failures),
                    each item with the minimum context to decide on - never raw logs
    digest.py       a capped, cached summary since the last digest (JSON and a short text form) and the last-digest marker
    checkin.py      the optional scheduled digest: runs only while the capacity governor says cloud capacity exists
    store.py        the marker, the check-in log and the token accounting (a small JSON file in the agent state folder)

Nothing here changes the factory: it reads the work board, the conductor's records and the capacity governor. Deciding stays with
`abp conductor approve|unblock` and `abp capacity`.
"""
from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "digest": {"cap_tokens": 3000,     # hard ceiling on a digest's estimated size (~4 characters a token)
               "cache_s": 30,          # an identical request inside this many seconds gets the cached digest
               "diff_chars": 400},     # diff excerpt per escalation in a digest (the escalations view carries up to 1500)
    "checkins": {"enabled": False,     # OFF by default
                 "interval_s": 3600,   # between scheduled check-ins
                 "poll_s": 60},
}


def settings() -> dict[str, Any]:
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        from bot.config import config

        got = config.current.get("manager") or {}
    except Exception:  # noqa: BLE001 - config trouble must not stop the manager channel
        got = {}
    if isinstance(got, dict):
        for k in out:
            if isinstance(got.get(k), dict):
                out[k].update(got[k])
    return out


def est_tokens(text: str) -> int:
    """A cheap, stable token estimate (4 characters a token, rounded up): good enough to budget a digest."""
    return (len(text) + 3) // 4
