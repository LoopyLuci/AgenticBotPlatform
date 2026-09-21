"""How much of a model's allowance has been used, and holding calls back before they are refused (roadmap PM).

Free models come with limits - requests per minute, requests per day, tokens per minute, tokens per
day, sometimes a cap on simultaneous calls. This module counts every model call ABP makes (every
transport is covered, see `install()` below), compares the counts with the model's limits
(model_catalog.py: what you configured, else what the provider publishes), and:

  * **before a call**, waits a little if a per-minute limit would be crossed, or refuses at once with a
    clear message and the reset time if a daily limit is used up - so a bot fails over to its fallback
    model or tells the user, instead of hammering a provider that will only answer 429;
  * **after a call**, records the tokens used and anything the provider says about your allowance in
    its response headers (they beat any table: `x-ratelimit-*`, `anthropic-ratelimit-*`, `retry-after`);
  * **on a 429**, remembers when to try again.

Counts persist (SQLite under the agent state directory), so a restart does not forget today's usage.
A limit that nobody knows is *not enforced*; usage is still counted and reported.

    native_agent:
      models:
        enforce: true        # false = count and report, never hold a call back
        max_wait_s: 15       # the longest a call is delayed to stay under a per-minute limit
        timezone: ""         # for showing reset times, e.g. America/New_York; blank = this computer's
        limits:              # your own figures, which replace anything published
          "openrouter/*:free": {rpm: 20, rpd: 1000}
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import fnmatch
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from bot.backends.base import BackendError

logger = logging.getLogger("bot.usage_limits")

DEFAULT_MAX_WAIT_S = 15.0
KEEP_DAYS = 45
_lock = threading.Lock()
_inflight: dict[tuple, int] = {}
_blocked_until: dict[tuple, float] = {}      # (provider, model) -> unix time, from a 429
_reported: dict[tuple, dict] = {}            # (provider, model) -> what the provider's headers said
_inside: contextvars.ContextVar = contextvars.ContextVar("abp_usage_guard", default=False)
_last_prune = 0.0
# (provider, model) the current turn is running on - set by the agent loop so tools can answer "what am I?"
current_model: contextvars.ContextVar = contextvars.ContextVar("abp_current_model", default=None)


class RateLimited(BackendError):
    """A call was not made because the model's allowance is used up (or the provider said to wait)."""

    def __init__(self, message: str, *, retry_at: Optional[float] = None):
        super().__init__(message)
        self.retry_at = retry_at


def _cfg() -> dict:
    try:
        from bot.config import config

        return (((config.current.get("native_agent") or {}).get("models")) or {})
    except Exception:  # noqa: BLE001
        return {}


def enforcing() -> bool:
    return bool(_cfg().get("enforce", True))


def max_wait_s() -> float:
    try:
        return max(0.0, float(_cfg().get("max_wait_s", DEFAULT_MAX_WAIT_S)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WAIT_S


# ---- storage ----------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    from bot.agent_runtime.state import state_dir

    conn = sqlite3.connect(str(state_dir("usage") / "usage.db"), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, "
                 "tokens INTEGER NOT NULL DEFAULT 0, in_tok INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 200)")
    conn.execute("CREATE INDEX IF NOT EXISTS calls_pm ON calls (provider, model, ts)")
    return conn


def record(provider: str, model: str, *, tokens: int = 0, input_tokens: int = 0, status: int = 200,
           now: Optional[float] = None) -> None:
    """Count one call. A refused call (429) still counts as a request: the provider counted it."""
    global _last_prune
    now = time.time() if now is None else now
    try:
        with _lock:
            conn = _db()
            try:
                conn.execute("INSERT INTO calls VALUES (?,?,?,?,?,?)", (now, provider, model, int(tokens or 0), int(input_tokens or 0), int(status)))
                if now - _last_prune > 3600:
                    _last_prune = now
                    conn.execute("DELETE FROM calls WHERE ts < ?", (now - KEEP_DAYS * 86400,))
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error:
        logger.warning("could not record model usage", exc_info=True)


def _rows(provider: str, since: float, model: Optional[str] = None) -> list[tuple]:
    try:
        with _lock:
            conn = _db()
            try:
                if model is None:
                    cur = conn.execute("SELECT ts, model, tokens, in_tok, status FROM calls WHERE provider=? AND ts>=?", (provider, since))
                else:
                    cur = conn.execute("SELECT ts, model, tokens, in_tok, status FROM calls WHERE provider=? AND model=? AND ts>=?", (provider, model, since))
                return cur.fetchall()
            finally:
                conn.close()
    except sqlite3.Error:
        return []


# ---- time -----------------------------------------------------------------------------------
def tz():
    name = str(_cfg().get("timezone") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            logger.warning("unknown timezone %r for model usage; using this computer's", name)
    return datetime.now().astimezone().tzinfo


def day_bounds(reset: str, now: float) -> tuple[float, float]:
    """(start of the current day-window, when it next resets) as unix times."""
    if reset == "rolling":
        return now - 86400, now + 86400  # the reset moment for a rolling window is computed from the oldest call instead
    zone = timezone.utc
    if reset == "pacific_midnight":
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("America/Los_Angeles")
    local = datetime.fromtimestamp(now, zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), nxt.timestamp()


def show_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz()).strftime("%Y-%m-%d %H:%M %Z")


def show_span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


# ---- provider headers -----------------------------------------------------------------------
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_wait(value: Any, now: Optional[float] = None) -> Optional[float]:
    """Seconds from now until a reset/retry moment, from any of the formats providers use: "1s",
    "6m0s", "20ms", a bare number of seconds, an ISO timestamp, an epoch in seconds or milliseconds,
    or an HTTP date."""
    now = time.time() if now is None else now
    text = str(value or "").strip()
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        num = None
    if num is not None:
        if num > 1e12:
            return max(0.0, num / 1000 - now)
        if num > 1e9:
            return max(0.0, num - now)
        return max(0.0, num)
    parts = _DURATION.findall(text)
    if parts and _DURATION.sub("", text).strip() == "":
        return sum(float(n) * _UNIT[u] for n, u in parts)
    try:
        if "T" in text:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            stamp = parsedate_to_datetime(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, stamp.timestamp() - now)
    except (ValueError, TypeError):
        return None


def _num(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse_headers(headers: dict, now: Optional[float] = None) -> dict:
    """What a response's headers say about the allowance:
    {"requests": {limit, remaining, reset_in_s}, "tokens": {...}, "retry_after_s": n}. Empty if nothing."""
    now = time.time() if now is None else now
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    out: dict = {}

    def take(name: str, limit: Any, remaining: Any, reset: Any) -> None:
        if limit is None and remaining is None:
            return
        found = {"limit": _num(limit), "remaining": _num(remaining), "reset_in_s": parse_wait(reset, now)}
        out.setdefault(name, {}).update({k: v for k, v in found.items() if v is not None})

    for kind in ("requests", "tokens"):
        take(kind, h.get(f"x-ratelimit-limit-{kind}"), h.get(f"x-ratelimit-remaining-{kind}"), h.get(f"x-ratelimit-reset-{kind}"))
        take(kind, h.get(f"anthropic-ratelimit-{kind}-limit"), h.get(f"anthropic-ratelimit-{kind}-remaining"), h.get(f"anthropic-ratelimit-{kind}-reset"))
    if "tokens" not in out:      # Anthropic may report input and output tokens separately: the input side is what fills up
        take("tokens", h.get("anthropic-ratelimit-input-tokens-limit"), h.get("anthropic-ratelimit-input-tokens-remaining"),
             h.get("anthropic-ratelimit-input-tokens-reset"))
    if "requests" not in out:    # OpenRouter's bare form (only sent with a 429)
        take("requests", h.get("x-ratelimit-limit"), h.get("x-ratelimit-remaining"), h.get("x-ratelimit-reset"))
    if "retry-after" in h:
        wait = parse_wait(h["retry-after"], now)
        if wait is not None:
            out["retry_after_s"] = wait
    return out


def observe_headers(provider: str, model: str, headers: dict, *, now: Optional[float] = None) -> dict:
    """Remember what the provider said about the allowance. Returns the parsed figures."""
    now = time.time() if now is None else now
    parsed = parse_headers(headers, now)
    if parsed:
        entry = {"seen_at": now}
        for name in ("requests", "tokens"):
            if name in parsed:
                p = dict(parsed[name])
                if "reset_in_s" in p:
                    p["resets_at"] = now + p.pop("reset_in_s")
                entry[name] = p
        _reported[(provider, model)] = entry
    return parsed


def note_rate_limited(provider: str, model: str, retry_after_s: Optional[float], *, now: Optional[float] = None) -> float:
    """A 429 arrived: do not call this model again until then. Returns the moment."""
    now = time.time() if now is None else now
    wait = retry_after_s if retry_after_s and retry_after_s > 0 else 30.0
    until = now + min(wait, 3600.0)
    _blocked_until[(provider, model)] = until
    return until


# ---- what is used -------------------------------------------------------------------------
def _limits(provider: str, model: str):
    from bot import model_catalog

    return model_catalog.declared_limits(provider, model, model_catalog.catalog_provider_for(provider))


def _matching(rows: list[tuple], limits, model: str) -> list[tuple]:
    if limits.scope == "provider":
        pattern = _match_pattern(limits)
        return [r for r in rows if fnmatch.fnmatchcase(r[1].lower(), pattern.lower())]
    return [r for r in rows if r[1] == model]


def _match_pattern(limits) -> str:
    return getattr(limits, "match", "") or "*"


def _windows(provider: str, model: str, now: float, limits=None, est_tokens: int = 0) -> list[dict]:
    """Each window that has a limit, with its use, what is left and when it frees up."""
    limits = limits or _limits(provider, model)
    day_start, day_reset = day_bounds(limits.reset, now)
    rows = _matching(_rows(provider, min(now - 60, day_start)), limits, model)
    minute = [r for r in rows if r[0] >= now - 60]
    day = [r for r in rows if r[0] >= day_start]
    out = []

    def add(name: str, used: int, limit: Optional[int], span_rows: list, span: float, calendar_reset: Optional[float], per: str) -> None:
        if limit is None:
            return
        if calendar_reset is not None:
            resets_at = calendar_reset
        else:
            resets_at = (min(r[0] for r in span_rows) + span) if span_rows else now
        out.append({"name": name, "used": used, "limit": limit, "remaining": max(0, limit - used), "resets_at": resets_at,
                    "resets_in_s": max(0.0, resets_at - now), "per": per})

    add("requests/min", len(minute), limits.rpm, minute, 60, None, "minute")
    add("tokens/min", sum(r[2] for r in minute), limits.tpm, minute, 60, None, "minute")
    cal = day_reset if limits.reset != "rolling" else None
    add("requests/day", len(day), limits.rpd, day, 86400, cal, "day")
    add("tokens/day", sum(r[2] for r in day), limits.tpd, day, 86400, cal, "day")
    return out


def snapshot(provider: str, model: str, *, now: Optional[float] = None) -> dict:
    """Use, limits and headroom for one model right now."""
    now = time.time() if now is None else now
    limits = _limits(provider, model)
    windows = _windows(provider, model, now, limits)
    blocked = _blocked_until.get((provider, model), 0.0)
    reported = _reported.get((provider, model))
    fractions = [w["remaining"] / w["limit"] for w in windows if w["limit"]]
    for kind in ("requests", "tokens"):
        r = (reported or {}).get(kind) or {}
        if r.get("limit") and r.get("remaining") is not None and r.get("resets_at", now) >= now:
            fractions.append(r["remaining"] / r["limit"])
    day = [r for r in _rows(provider, now - 86400, model)]
    return {
        "provider": provider, "model": model, "windows": windows, "limits": limits.__dict__,
        "reported": reported, "blocked_until": blocked if blocked > now else None,
        "headroom": min(fractions) if fractions else None,
        "calls_24h": len(day), "tokens_24h": sum(r[2] for r in day), "rate_limited_24h": sum(1 for r in day if r[4] == 429),
        "in_flight": _inflight.get((provider, model), 0),
    }


def key_for_provider(name: str) -> str:
    """The key usage is counted under for a providers.yaml name: its catalog id, else its address's host
    (the same key a transport reports as provider_key)."""
    try:
        from urllib.parse import urlparse

        from bot import providers as registry

        cfg = registry.get_provider(name) or {}
        if cfg.get("catalog_id"):
            return str(cfg["catalog_id"])
        if cfg.get("base_url"):
            return (urlparse(str(cfg["base_url"])).hostname or str(cfg["base_url"])).lower()
    except Exception:  # noqa: BLE001
        pass
    return name


def headroom(provider: str, model: str) -> Optional[float]:
    """0..1 of the tightest allowance still available; None when no limit is known (so: unconstrained)."""
    snap = snapshot(provider, model)
    if snap["blocked_until"]:
        return 0.0
    return snap["headroom"]


def report(days: int = 1, *, now: Optional[float] = None) -> list[dict]:
    """Usage per model over the last `days` days, most used first."""
    now = time.time() if now is None else now
    since = now - max(1, days) * 86400
    try:
        with _lock:
            conn = _db()
            try:
                rows = conn.execute("SELECT provider, model, COUNT(*), SUM(tokens), SUM(status=429), SUM(status>=400 AND status<>429), MAX(ts) "
                                    "FROM calls WHERE ts>=? GROUP BY provider, model ORDER BY COUNT(*) DESC", (since,)).fetchall()
            finally:
                conn.close()
    except sqlite3.Error:
        return []
    return [{"provider": p, "model": m, "calls": c, "tokens": int(t or 0), "rate_limited": int(r or 0), "errors": int(e or 0), "last_used": last}
            for p, m, c, t, r, e, last in rows]


# ---- holding calls back --------------------------------------------------------------------
def _explain(provider: str, model: str, window: dict) -> str:
    return (f"{provider}/{model} has used {window['used']:,} of its {window['limit']:,} {window['name']}; "
            f"it frees up at {show_time(window['resets_at'])} (in {show_span(window['resets_in_s'])}).")


async def before_call(provider: str, model: str, est_tokens: int = 0, *, now: Optional[float] = None) -> None:
    """Wait briefly, or raise RateLimited, if calling now would cross a known limit."""
    if not enforcing():
        return
    ceiling = max_wait_s()
    for _ in range(3):
        moment = time.time() if now is None else now
        need_s, why = 0.0, ""
        until = _blocked_until.get((provider, model), 0.0)
        if until > moment:
            need_s, why = until - moment, f"{provider}/{model} asked us to wait (it answered 429); try again at {show_time(until)}."
        rep = _reported.get((provider, model)) or {}
        for kind, need in (("requests", 1), ("tokens", est_tokens)):
            r = rep.get(kind) or {}
            if r.get("remaining") is not None and r.get("resets_at", 0) > moment:
                short = r["remaining"] <= 0 if kind == "requests" else (r["remaining"] <= 0 or (need > 0 and r["remaining"] < need))
                wait = r["resets_at"] - moment
                if short and wait > need_s:
                    need_s, why = wait, (f"{provider}/{model} reports no {kind} left; it frees up at {show_time(r['resets_at'])} "
                                         f"(in {show_span(wait)}).")
        limits = _limits(provider, model)
        windows = _windows(provider, model, moment, limits)
        for w in windows:
            cost = 1 if w["name"].startswith("requests") else est_tokens
            if w["used"] + cost > w["limit"] and (cost <= w["limit"]):
                wait = w["resets_in_s"]
                if wait > need_s:
                    need_s, why = wait, _explain(provider, model, w)
        if limits.concurrent and _inflight.get((provider, model), 0) >= limits.concurrent:
            need_s, why = max(need_s, 1.0), f"{provider}/{model} allows {limits.concurrent} call(s) at once and they are all busy."
        if need_s <= 0:
            return
        if need_s > ceiling:
            raise RateLimited(why, retry_at=moment + need_s)
        await asyncio.sleep(need_s + 0.05)
        if now is not None:
            return
    return


def _status_of(exc: BaseException, transport) -> Optional[int]:
    status = getattr(transport, "last_status", None)
    if isinstance(status, int) and status >= 400:
        return status
    match = re.search(r"\b(429|5\d\d|4\d\d)\b", str(exc))
    return int(match.group(1)) if match else None


async def guarded(transport, fn, args: tuple, kwargs: dict):
    """Run one transport call under the allowance: check before, count after. Nested calls (a stream that
    falls back to send()) are counted once."""
    if _inside.get():
        return await fn(transport, *args, **kwargs)
    provider = getattr(transport, "provider_key", "") or type(transport).__name__.lower()
    model = str(kwargs.get("model") or "")
    est = 0
    try:
        from bot.agent_runtime import context_window

        est = int(context_window.measure_chars(kwargs.get("history") or [], kwargs.get("system_prompt"), kwargs.get("tool_schemas")) / context_window.ratio_for(model))
    except Exception:  # noqa: BLE001
        pass
    await before_call(provider, model, est)
    key = (provider, model)
    _inflight[key] = _inflight.get(key, 0) + 1
    token = _inside.set(True)
    try:
        transport.last_status, transport.rate_headers = None, {}
        try:
            response = await fn(transport, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            status = _status_of(exc, transport) or 599
            headers = getattr(transport, "rate_headers", {}) or {}
            parsed = observe_headers(provider, model, headers)
            if status == 429:
                note_rate_limited(provider, model, parsed.get("retry_after_s"))
            record(provider, model, tokens=0, status=status)
            raise
        observe_headers(provider, model, getattr(transport, "rate_headers", {}) or {})
        record(provider, model, tokens=int(getattr(response, "tokens", 0) or 0) or est, input_tokens=int(getattr(response, "input_tokens", 0) or 0))
        return response
    finally:
        _inside.reset(token)
        _inflight[key] = max(0, _inflight.get(key, 1) - 1)


def install(cls) -> None:
    """Wrap a transport class's send() and send_stream() so every call is counted. Called for each
    subclass of ProviderTransport (see transports/base.py)."""
    for name in ("send", "send_stream"):
        fn = cls.__dict__.get(name)
        if fn is None or getattr(fn, "_usage_guarded", False):
            continue

        def make(fn):
            @functools.wraps(fn)
            async def wrapper(self, *args, **kwargs):
                return await guarded(self, fn, args, kwargs)

            wrapper._usage_guarded = True
            return wrapper

        setattr(cls, name, make(fn))
