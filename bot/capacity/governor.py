"""The capacity governor's state machine and its persistence.

One system-wide state, `cloud-led` | `hybrid` | `local-only`, in data/agent/capacity/capacity.db (survives a restart), plus a
history of every change (who or what, when, why).

  effective state = the owner's override, while it has not expired; else the automatic state.

The automatic state moves on signals (bot/capacity/classify.py):
  * a usage cap          -> local-only until the reset time the message gave, else for `cap_hold_s` (doubling on a repeat, up to
                            `max_hold_s`); when a hold without a reset time ends, hybrid for `probation_s` - a cloud call that
                            works in that time returns to cloud-led, a new cap goes back to local-only
  * short rate limits    -> `rate_limit_threshold` of them inside `rate_limit_window_s` turn hybrid on for `hybrid_hold_s`
  * a long "retry after" -> (over `long_retry_s`) is treated as a usage cap that resets then
Time passes lazily: every read applies what has expired. Nothing here stops or restarts work that is running; the state is
only read when new work picks a model (bot/capacity/routing.py).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from bot.capacity.classify import RATE_LIMIT, USAGE_CAP, Signal

CLOUD_LED, HYBRID, LOCAL_ONLY = "cloud-led", "hybrid", "local-only"
STATES = (CLOUD_LED, HYBRID, LOCAL_ONLY)
HISTORY_KEEP = 500
DEFAULTS: dict[str, Any] = {
    "enabled": True, "mode": "auto", "rate_limit_threshold": 3, "rate_limit_window_s": 300, "hybrid_hold_s": 600,
    "long_retry_s": 900, "cap_hold_s": 3600, "max_hold_s": 21600, "probation_s": 600,
    "hybrid": {"cloud_classes": ["hard_reasoning", "long_context"]}, "reroute_fixed_models": True,
    "local_providers": ["modelmistress", "ollama", "unsloth", "lmstudio", "llamacpp", "vllm"],
}
_lock = threading.RLock()
_DEDUPE_S = 2.0
_last_seen: dict[tuple, float] = {}


def settings() -> dict[str, Any]:
    out = {**DEFAULTS, "hybrid": dict(DEFAULTS["hybrid"])}
    try:
        from bot.config import config

        got = config.current.get("capacity") or {}
    except Exception:  # noqa: BLE001
        got = {}
    if isinstance(got, dict):
        for k, v in got.items():
            if k == "hybrid" and isinstance(v, dict):
                out["hybrid"].update(v)
            elif k in DEFAULTS:
                out[k] = v
    return out


def _num(cfg: dict, key: str) -> float:
    try:
        return max(0.0, float(cfg.get(key, DEFAULTS[key])))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


# ---- storage -----------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    from bot.agent_runtime.state import state_dir

    conn = sqlite3.connect(str(state_dir("capacity") / "capacity.db"), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, "
                 "kind TEXT NOT NULL, from_state TEXT, to_state TEXT, reason TEXT, detail TEXT)")
    return conn


def _blank() -> dict:
    return {"auto_state": CLOUD_LED, "auto_since": 0.0, "auto_until": None, "reset_known": False, "auto_reason": "", "strikes": 0,
            "probation": False, "rl_events": [], "override": None}


def _load(conn) -> dict:
    row = conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
    data = _blank()
    if row:
        try:
            data.update(json.loads(row[0]))
        except (ValueError, TypeError):
            pass
    return data


def _save(conn, data: dict) -> None:
    conn.execute("INSERT INTO state (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data", (json.dumps(data),))


def _log(conn, now: float, actor: str, kind: str, old: str, new: str, reason: str, detail: Optional[dict] = None) -> None:
    conn.execute("INSERT INTO history (ts, actor, kind, from_state, to_state, reason, detail) VALUES (?,?,?,?,?,?,?)",
                 (now, actor, kind, old, new, reason[:500], json.dumps(detail) if detail else None))
    conn.execute("DELETE FROM history WHERE id <= (SELECT MAX(id) FROM history) - ?", (HISTORY_KEEP,))


def _effective(data: dict) -> str:
    ov = data.get("override")
    return ov["state"] if ov else data["auto_state"]


def _pinned(cfg: dict) -> Optional[str]:
    mode = str(cfg.get("mode") or "auto").strip().lower()
    return mode if mode in STATES else None


def _tick(conn, data: dict, now: float, cfg: dict) -> bool:
    """Apply whatever time has expired. True when `data` changed."""
    changed = False
    ov = data.get("override")
    if ov and ov.get("expires_at") and now >= ov["expires_at"]:
        old = _effective(data)
        data["override"] = None
        _log(conn, now, "system", "override_expired", old, _effective(data), f"the override ({ov['state']}) set by {ov.get('actor')} ran out")
        changed = True
    until = data.get("auto_until")
    if data["auto_state"] != CLOUD_LED and until and now >= until:
        old = _effective(data)
        if data["auto_state"] == LOCAL_ONLY and not data.get("reset_known"):
            probation = _num(cfg, "probation_s")
            data.update(auto_state=HYBRID, auto_since=now, auto_until=now + probation, probation=True, reset_known=False,
                        auto_reason="the usage-cap hold ended; trying cloud again for hard tasks")
            _log(conn, now, "system", "auto", old, _effective(data), data["auto_reason"])
        else:
            why = "the provider's reset time passed" if data.get("reset_known") else "the hold ended"
            data.update(auto_state=CLOUD_LED, auto_since=now, auto_until=None, probation=False, reset_known=False, auto_reason=why,
                        strikes=0 if data["auto_state"] == LOCAL_ONLY else data.get("strikes", 0))
            _log(conn, now, "system", "auto", old, _effective(data), why)
        changed = True
    return changed


def _view(data: dict, cfg: dict, now: float) -> dict:
    ov = data.get("override")
    pin = _pinned(cfg)
    mode = "manual" if ov else ("config" if pin else "auto")
    state = ov["state"] if ov else (pin or data["auto_state"])
    return {
        "state": state, "mode": mode, "enabled": bool(cfg.get("enabled", True)),
        "auto": {"state": data["auto_state"], "since": data["auto_since"] or None, "until": data.get("auto_until"),
                 "reset_known": bool(data.get("reset_known")), "reason": data.get("auto_reason", ""), "probation": bool(data.get("probation")),
                 "strikes": data.get("strikes", 0),
                 "recent_rate_limits": len([t for t in data.get("rl_events", []) if now - t <= _num(cfg, "rate_limit_window_s")])},
        "override": ov, "config_mode": str(cfg.get("mode") or "auto"), "now": now,
    }


def get_state(now: Optional[float] = None) -> dict:
    """The state now (time-expired holds and overrides applied). Never raises: on a storage error, cloud-led."""
    now = time.time() if now is None else now
    cfg = settings()
    try:
        with _lock:
            conn = _db()
            try:
                data = _load(conn)
                if _tick(conn, data, now, cfg):
                    _save(conn, data)
                    conn.commit()
                return _view(data, cfg, now)
            finally:
                conn.close()
    except sqlite3.Error:
        d = _blank()
        return _view(d, cfg, now)


def effective_state(now: Optional[float] = None) -> str:
    """cloud-led when the governor is disabled; otherwise the state routing should obey."""
    cfg = settings()
    if not cfg.get("enabled", True):
        return CLOUD_LED
    return get_state(now)["state"]


def _mutate(fn, now: float):
    """Run fn(conn, data, cfg) under the lock in one transaction; save, return its result."""
    cfg = settings()
    with _lock:
        conn = _db()
        try:
            data = _load(conn)
            _tick(conn, data, now, cfg)
            out = fn(conn, data, cfg)
            _save(conn, data)
            conn.commit()
            return out
        finally:
            conn.close()


# ---- manual control ----------------------------------------------------------------------
def set_override(state: str, *, actor: str = "owner", reason: str = "", expires_at: Optional[float] = None,
                 ttl_s: Optional[float] = None, now: Optional[float] = None) -> dict:
    """Pin the system to `state` (optionally until `expires_at` / for `ttl_s` seconds). `state` may be `auto` to clear."""
    now = time.time() if now is None else now
    state = str(state or "").strip().lower()
    if state == "auto":
        return clear_override(actor=actor, reason=reason, now=now)
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)} or auto")
    if ttl_s is not None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        expires_at = now + float(ttl_s)
    if expires_at is not None and expires_at <= now:
        raise ValueError("expires_at is in the past")

    def go(conn, data, cfg):
        old = _effective(data)
        data["override"] = {"state": state, "actor": actor, "reason": reason[:300], "since": now, "expires_at": expires_at}
        _log(conn, now, actor, "override", old, state, reason or "set by hand", {"expires_at": expires_at})
        return _view(data, cfg, now)

    return _mutate(go, now)


def clear_override(*, actor: str = "owner", reason: str = "", now: Optional[float] = None) -> dict:
    """Back to automatic."""
    now = time.time() if now is None else now

    def go(conn, data, cfg):
        if data.get("override"):
            old = _effective(data)
            data["override"] = None
            _log(conn, now, actor, "override_cleared", old, _effective(data), reason or "back to automatic")
        return _view(data, cfg, now)

    return _mutate(go, now)


# ---- automatic detection -----------------------------------------------------------------
def observe(signal: Signal, *, provider: str = "", model: str = "", now: Optional[float] = None) -> Optional[dict]:
    """Feed one classified failure of a *cloud* provider. Returns the new state view when the automatic state changed."""
    cfg = settings()
    if not cfg.get("enabled", True) or signal is None:
        return None
    now = time.time() if now is None else now
    key = (signal.kind, provider, signal.reason[:60])
    if now - _last_seen.get(key, -1e9) < _DEDUPE_S:
        return None                     # the same failure seen at two layers
    _last_seen[key] = now
    if len(_last_seen) > 256:
        _last_seen.clear()
    who = f"{provider}/{model}".strip("/") or "provider"

    def go(conn, data, cfg):
        old_eff, old_auto = _effective(data), data["auto_state"]
        kind, reset, retry = signal.kind, signal.reset_at, signal.retry_after_s
        if kind == RATE_LIMIT and retry and retry > _num(cfg, "long_retry_s"):
            kind, reset = USAGE_CAP, now + retry
        if kind == USAGE_CAP:
            if reset and reset > now:
                until, known = min(reset, now + 8 * 86400), True
                reason = f"{who} reached its usage limit; it resets in {_human(until - now)}"
            else:
                strikes = int(data.get("strikes", 0))
                until = now + min(_num(cfg, "cap_hold_s") * (2 ** strikes), _num(cfg, "max_hold_s"))
                known = False
                data["strikes"] = strikes + 1
                reason = f"{who} reached its usage limit (no reset time given); holding local-only for {_human(until - now)}"
            if data["auto_state"] == LOCAL_ONLY and data.get("auto_until") and data["auto_until"] >= until:
                return None
            data.update(auto_state=LOCAL_ONLY, auto_since=data["auto_since"] if old_auto == LOCAL_ONLY else now, auto_until=until,
                        reset_known=known, probation=False, auto_reason=reason, rl_events=[])
            _log(conn, now, "auto", "usage_cap", old_eff, _effective(data), f"{reason}: {signal.reason}", signal.as_dict())
            return _view(data, cfg, now)
        # a short rate limit
        window = _num(cfg, "rate_limit_window_s")
        events = [t for t in data.get("rl_events", []) if now - t <= window] + [now]
        data["rl_events"] = events
        if data["auto_state"] == LOCAL_ONLY or len(events) < int(_num(cfg, "rate_limit_threshold")):
            return None
        until = now + _num(cfg, "hybrid_hold_s")
        reason = f"{len(events)} rate limits in {_human(window)} (latest from {who}); hybrid for {_human(until - now)}"
        data.update(auto_state=HYBRID, auto_since=data["auto_since"] if old_auto == HYBRID else now, auto_until=max(until, data.get("auto_until") or 0)
                    if old_auto == HYBRID else until, reset_known=False, probation=False, auto_reason=reason, rl_events=[])
        _log(conn, now, "auto", "rate_limits", old_eff, _effective(data), reason, signal.as_dict())
        return _view(data, cfg, now)

    try:
        return _mutate(go, now)
    except sqlite3.Error:
        return None


def observe_success(provider: str = "", model: str = "", *, now: Optional[float] = None) -> Optional[dict]:
    """A cloud call worked. During a probation (hybrid after an unknown-reset cap) that ends the hold."""
    cfg = settings()
    if not cfg.get("enabled", True):
        return None
    try:
        quick = get_state(now)["auto"]
        if not quick["probation"]:
            return None
    except Exception:  # noqa: BLE001
        return None
    now = time.time() if now is None else now

    def go(conn, data, cfg):
        if not data.get("probation") or data["auto_state"] != HYBRID:
            return None
        old = _effective(data)
        data.update(auto_state=CLOUD_LED, auto_since=now, auto_until=None, probation=False, strikes=0, reset_known=False,
                    auto_reason=f"{provider}/{model} answered again", rl_events=[])
        _log(conn, now, "auto", "recovered", old, _effective(data), data["auto_reason"])
        return _view(data, cfg, now)

    try:
        return _mutate(go, now)
    except sqlite3.Error:
        return None


# TODO(LF3 phase 2): an active probe (one tiny cloud call on a timer while local-only) so recovery does not wait for hybrid's
# first real call; and LF5's digest hook - emit "cloud capacity is back" when the effective state returns to cloud-led.
def history(limit: int = 50) -> list[dict]:
    try:
        with _lock:
            conn = _db()
            try:
                rows = conn.execute("SELECT id, ts, actor, kind, from_state, to_state, reason, detail FROM history ORDER BY id DESC LIMIT ?",
                                    (max(1, min(int(limit), HISTORY_KEEP)),)).fetchall()
            finally:
                conn.close()
    except sqlite3.Error:
        return []
    return [{"id": r[0], "ts": r[1], "actor": r[2], "kind": r[3], "from": r[4], "to": r[5], "reason": r[6],
             "detail": json.loads(r[7]) if r[7] else None} for r in rows]


def _human(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    if seconds >= 60:
        return f"{int(seconds // 60)} minutes"
    return f"{int(seconds)} seconds"
