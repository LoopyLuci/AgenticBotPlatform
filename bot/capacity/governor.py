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
    # The recovery probe (bot/capacity/probe.py): one tiny request to a capped provider on a timer, backing off while it fails.
    "probe": {"enabled": True, "interval_s": 300, "max_interval_s": 3600, "backoff": 2.0, "max_per_hour": 6, "reset_fraction": 0.5,
              "timeout_s": 20, "targets": []},
    # Handoff packs written automatically as a task runs (bot/capacity/autopack.py).
    "handoff": {"auto": True, "min_interval_s": 20, "git_diff": True, "max_diff_chars": 12000},
    "local_providers": ["modelmistress", "ollama", "unsloth", "lmstudio", "llamacpp", "vllm"],
}
_lock = threading.RLock()
_DEDUPE_S = 2.0
_last_seen: dict[tuple, float] = {}
_pending: list[dict] = []           # events logged inside a transaction, published once it has committed
CAPPED_KEEP = 8


def settings() -> dict[str, Any]:
    out = {**DEFAULTS, **{k: dict(DEFAULTS[k]) for k in ("hybrid", "probe", "handoff")}}
    try:
        from bot.config import config

        got = config.current.get("capacity") or {}
    except Exception:  # noqa: BLE001
        got = {}
    if isinstance(got, dict):
        for k, v in got.items():
            if k in ("hybrid", "probe", "handoff") and isinstance(v, dict):
                out[k].update(v)
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
            "probation": False, "rl_events": [], "override": None,
            "capped": {}, "probe": {"next_at": 0.0, "times": [], "fails": 0, "last": None}}


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
    _pending.append({"ts": now, "actor": actor, "kind": kind, "from": old, "to": new, "reason": reason[:300]})


def _flush() -> None:
    """Publish the events the last transaction logged (LF5 hook: bot/capacity/events.py). Never raises."""
    with _lock:
        items, _pending[:] = list(_pending), []
    if not items:
        return
    try:
        from bot.capacity import events

        for it in items:
            events.emit_change(it)
    except Exception:  # noqa: BLE001 - telling listeners must never cost a state change
        pass


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
            _forget_caps(data)
            _log(conn, now, "system", "auto", old, _effective(data), why)
        changed = True
    return changed


def _forget_caps(data: dict) -> None:
    """Back on cloud-led: nothing is capped any more, so there is nothing to probe."""
    data["capped"] = {}
    data["probe"] = {"next_at": 0.0, "times": [], "fails": 0, "last": (data.get("probe") or {}).get("last")}


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
        "probe": _probe_view(data, cfg, now),
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
                changed = _tick(conn, data, now, cfg)
                if changed:
                    _save(conn, data)
                    conn.commit()
                out = _view(data, cfg, now)
            finally:
                conn.close()
            if changed:
                _flush()
            return out
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
        except BaseException:
            _pending.clear()
            raise
        finally:
            conn.close()
        _flush()
        return out


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
def _note_capped(data: dict, provider: str, model: str, now: float, reason: str) -> None:
    """Remember which cloud provider/model ran out: the recovery probe asks that one."""
    if not provider:
        return
    key = f"{provider}/{model}".strip("/")
    capped = data.setdefault("capped", {})
    capped[key] = {"provider": provider, "model": model, "ts": now, "reason": reason[:160]}
    while len(capped) > CAPPED_KEEP:
        capped.pop(min(capped, key=lambda k: capped[k]["ts"]))
    pr = data.setdefault("probe", {"next_at": 0.0, "times": [], "fails": 0, "last": None})
    if float(pr.get("next_at") or 0) <= now:
        pr["next_at"] = now + _probe_interval(settings().get("probe") or {}, 0)      # the first probe is one interval away


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
            _note_capped(data, provider, model, now, signal.reason)
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
        _note_capped(data, provider, model, now, signal.reason)
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
        _forget_caps(data)
        _log(conn, now, "auto", "recovered", old, _effective(data), data["auto_reason"])
        return _view(data, cfg, now)

    try:
        return _mutate(go, now)
    except sqlite3.Error:
        return None


# ---- the recovery probe (bot/capacity/probe.py decides what to send; this keeps the schedule and applies the outcome) ------
def _probe_view(data: dict, cfg: dict, now: float) -> dict:
    pc, pr = cfg.get("probe") or {}, data.get("probe") or {}
    hour = [t for t in pr.get("times", []) if now - t < 3600]
    return {"enabled": bool(pc.get("enabled", True)), "next_at": pr.get("next_at") or None, "fails": pr.get("fails", 0),
            "last": pr.get("last"), "last_hour": len(hour), "max_per_hour": int(pc.get("max_per_hour", 6)),
            "targets": [{"provider": c["provider"], "model": c["model"]} for c in data.get("capped", {}).values()]}


def _probe_interval(pc: dict, fails: int) -> float:
    base = max(5.0, float(pc.get("interval_s", 300)))
    cap = max(base, float(pc.get("max_interval_s", 3600)))
    return min(cap, base * (max(1.0, float(pc.get("backoff", 2.0))) ** max(0, fails)))


def probe_claim(*, force: bool = False, now: Optional[float] = None) -> Optional[dict]:
    """Reserve one probe round if one is due: {"targets": [{provider, model}], "timeout_s"} or None. The reservation (the hourly
    count, the next time) is taken here in one transaction, so two loops never probe twice. `force` skips the interval and the
    wait for a stated reset (the hourly limit still holds)."""
    now = time.time() if now is None else now
    cfg = settings()
    pc = cfg.get("probe") or {}
    if not cfg.get("enabled", True) or not pc.get("enabled", True):
        return None

    def go(conn, data, cfg):
        pr = data.setdefault("probe", {"next_at": 0.0, "times": [], "fails": 0, "last": None})
        if data["auto_state"] == CLOUD_LED:
            return None
        if data.get("reset_known") and data.get("auto_until") and not force:
            since, until = data.get("auto_since") or now, data["auto_until"]
            if now < since + float(pc.get("reset_fraction", 0.5)) * max(0.0, until - since):
                return None                     # the provider said when it resets: do not ask before most of that has passed
        if now < float(pr.get("next_at") or 0) and not force:
            return None
        pr["times"] = [t for t in pr.get("times", []) if now - t < 3600]
        if len(pr["times"]) >= int(pc.get("max_per_hour", 6)):
            pr["next_at"] = max(float(pr.get("next_at") or 0), pr["times"][0] + 3600)
            return None
        targets = []
        for t in pc.get("targets") or []:
            prov, _, model = str(t).partition("/")
            targets.append({"provider": prov, "model": model})
        if not targets:
            targets = [{"provider": c["provider"], "model": c["model"]}
                       for c in sorted(data.get("capped", {}).values(), key=lambda c: -c["ts"])]
        targets = targets[:3]
        pr["times"].append(now)
        pr["next_at"] = now + _probe_interval(pc, pr.get("fails", 0))
        if not targets:
            pr["last"] = {"ts": now, "ok": False, "provider": "", "model": "", "status": "unprobeable",
                          "detail": "no capped API provider is known (a CLI agent's limit cannot be probed); recovery waits for the reset time"}
            return None
        return {"targets": targets, "timeout_s": float(pc.get("timeout_s", 20))}

    try:
        return _mutate(go, now)
    except sqlite3.Error:
        return None


def probe_finish(ok: bool, *, provider: str = "", model: str = "", status: str = "", detail: str = "",
                 reset_at: Optional[float] = None, retry_after_s: Optional[float] = None, now: Optional[float] = None) -> Optional[dict]:
    """Apply a probe's outcome. A success moves local-only to hybrid probation, and hybrid to cloud-led (the same two steps a
    working real call takes). A failure backs the next probe off, and honours a reset time or retry-after the answer gave."""
    now = time.time() if now is None else now

    def go(conn, data, cfg):
        pc = cfg.get("probe") or {}
        pr = data.setdefault("probe", {"next_at": 0.0, "times": [], "fails": 0, "last": None})
        last = {"ts": now, "ok": bool(ok), "provider": provider, "model": model, "status": status[:60], "detail": detail[:300]}
        pr["last"] = last
        who = f"{provider}/{model}".strip("/") or "provider"
        old = _effective(data)
        if ok:
            pr["fails"], pr["next_at"] = 0, now + _probe_interval(pc, 0)
            if data["auto_state"] == LOCAL_ONLY:
                data.update(auto_state=HYBRID, auto_since=now, auto_until=now + _num(cfg, "probation_s"), probation=True, reset_known=False,
                            auto_reason=f"a probe of {who} was answered; trying cloud again for hard tasks")
                _log(conn, now, "probe", "probe_ok", old, _effective(data), data["auto_reason"])
            elif data["auto_state"] == HYBRID:
                data.update(auto_state=CLOUD_LED, auto_since=now, auto_until=None, probation=False, strikes=0, reset_known=False,
                            auto_reason=f"a probe of {who} was answered", rl_events=[])
                _forget_caps(data)
                data["probe"]["last"] = last
                _log(conn, now, "probe", "recovered", old, _effective(data), data["auto_reason"])
            return _view(data, cfg, now)
        pr["fails"] = int(pr.get("fails", 0)) + 1
        wait = _probe_interval(pc, pr["fails"])
        hint = reset_at if reset_at and reset_at > now else (now + retry_after_s if retry_after_s else 0.0)
        pr["next_at"] = max(now + wait, min(hint, now + 8 * 86400)) if hint else now + wait
        if reset_at and reset_at > now and data["auto_state"] == LOCAL_ONLY and reset_at > (data.get("auto_until") or 0):
            until = min(reset_at, now + 8 * 86400)         # the answer named a later reset: hold until then, no further
            data.update(auto_until=until, reset_known=True, auto_reason=f"{who} still at its usage limit; it resets in {_human(until - now)}")
            _log(conn, now, "probe", "probe_cap", old, _effective(data), data["auto_reason"])
        else:
            _log(conn, now, "probe", "probe_failed", old, _effective(data), f"probe of {who}: {status or 'failed'} {detail[:160]}".strip())
        return _view(data, cfg, now)

    try:
        return _mutate(go, now)
    except sqlite3.Error:
        return None


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
