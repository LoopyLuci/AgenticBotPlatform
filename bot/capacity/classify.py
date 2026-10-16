"""Does this provider failure mean "out of capacity"? A pure function over what ABP already sees: an HTTP status, the error
body or message text, and the rate-limit headers.

    classify(status, text, headers=None) -> Signal | None

Two kinds of signal matter:
  * `rate_limit` - a short throttle (requests/tokens per minute, "try again in 6s"). Retry later; many in a row mean hybrid.
  * `usage_cap`  - a quota, credit balance, spend limit or plan window that is used up ("usage limit reached", insufficient
                   quota, an overage that was rejected, Claude Code's "5-hour limit reached - resets 3pm"). It lasts until a
                   reset time, which is taken from the message when one is given.
Everything else (overloaded 529, 500s, bad requests, auth errors) is not a capacity problem and returns None.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

RATE_LIMIT = "rate_limit"
USAGE_CAP = "usage_cap"

# Phrases that mean the allowance itself is gone (checked first: a cap body often also says "rate_limit").
_CAP = re.compile(
    r"usage limit|(?<!rate )limit reached|you(?:'|’)?ve hit your|you have hit your|hit your (?:usage |weekly |daily |5-hour )?limit|"
    r"insufficient[_ ]quota|credit balance is too low|insufficient[_ ]credits|"
    r"out of credits|no credits|payment required|billing[_ ]hard[_ ]limit|spend(?:ing)? limit|api usage limits|"
    r"overage[^.]{0,40}(?:reject|exceed|limit|disabled|not allowed)|(?:reject|exceed)[^.]{0,40}overage|out_of_credits|"
    r"extra usage|monthly (?:usage|spend|quota)|daily (?:usage|quota|limit)|per-day|requests per day|tokens per day|\brpd\b|\btpd\b|"
    r"free[- ]models?[- ]per[- ]day|plan limit|weekly limit")
# Weaker: a quota message that is a per-minute throttle when the text says "per minute" (Gemini says both the same way).
_CAP_WEAK = re.compile(r"exceeded your current quota|quota exceeded|quota_exceeded")
# Phrases that mean "slow down".
_RATE = re.compile(r"rate[_ -]?limit|too many requests|tokens per min|requests per min|\btpm\b|\brpm\b|resource[_ ]exhausted|throttl|"
                   r"slow down|try again in|retry after|retry in")

_DUR = re.compile(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?(?![a-z]))?\s*(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?(?![a-z]))?")
_TRY_AGAIN = re.compile(r"(?:try again|retry|retrydelay|retry_after|retry-after|resets?)\W{0,12}(?:in|after|:)?\W{0,6}([0-9][0-9hms. ]{0,24}[hms])", re.I)
_EPOCH_AFTER_BAR = re.compile(r"limit reached\s*\|\s*(\d{10,13})", re.I)
_RESET_HEADER = re.compile(r"x-?ratelimit-?reset[\"']?\s*[:=]\s*[\"']?(\d{10,13})", re.I)
_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ t]+(?:at\s+)?(\d{2}:\d{2})(?::\d{2})?)?\s*(utc|z)?", re.I)
_CLOCK = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
                    r"(?:\s*\((?P<tz>[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?|UTC)\))?", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class Signal:
    kind: str                         # rate_limit | usage_cap
    reset_at: Optional[float] = None  # epoch seconds the allowance comes back, when the message says
    retry_after_s: Optional[float] = None
    reason: str = ""                  # a short human line
    status: Optional[int] = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "reset_at": self.reset_at, "retry_after_s": self.retry_after_s, "reason": self.reason,
                "status": self.status}


def _flatten(text: Any) -> str:
    """The error text as one lower-cased string; a JSON body is flattened to its message/type/code fields as well as raw."""
    if text is None:
        return ""
    if isinstance(text, (dict, list)):
        try:
            return json.dumps(text).lower()
        except (TypeError, ValueError):
            return str(text).lower()
    return str(text).lower()


def _seconds(token: str) -> Optional[float]:
    token = token.strip().lower()
    if not token:
        return None
    m = _DUR.fullmatch(token)
    if m and any(m.groups()):
        h, mi, s = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    try:
        return float(token)
    except ValueError:
        return None


def retry_after(text: str, headers: Optional[dict] = None) -> Optional[float]:
    """Seconds to wait, from a Retry-After header or "try again in 7m12s" / retryDelay "34s" in the body."""
    if headers:
        for k, v in headers.items():
            if str(k).lower() == "retry-after":
                try:
                    return max(0.0, float(v))
                except (TypeError, ValueError):
                    break
    m = _TRY_AGAIN.search(text)
    if m:
        got = _seconds(m.group(1))
        if got is not None and got >= 0:
            return got
    return None


def _zone(name: Optional[str]):
    if not name:
        return None
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - no tz database on this machine: fall back to local time
        return None


def reset_time(text: str, headers: Optional[dict] = None, now: Optional[float] = None) -> Optional[float]:
    """The moment (epoch seconds) the allowance comes back, when the message gives one: an epoch after `limit reached|`, an
    X-RateLimit-Reset value, "regain access on 2026-11-01 at 00:00 UTC", or "resets 3pm (Europe/Berlin)"."""
    now = time.time() if now is None else now
    m = _EPOCH_AFTER_BAR.search(text)
    if m:
        return _epoch(m.group(1))
    hay = text + " " + " ".join(f"{k}:{v}" for k, v in (headers or {}).items())
    m = _RESET_HEADER.search(hay.lower())
    if m:
        return _epoch(m.group(1))
    if re.search(r"regain access|resets? (?:on|at)|available again|will reset", text):
        m = _ISO.search(text)
        if m:
            try:
                dt = datetime.strptime(f"{m.group(1)} {m.group(2) or '00:00'}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                pass
    m = _CLOCK.search(text)
    if m:
        return _clock(m, now)
    return None


def _epoch(digits: str) -> float:
    v = float(digits)
    return v / 1000.0 if v > 1e11 else v


def _clock(m: "re.Match", now: float) -> Optional[float]:
    hour, minute, ap = int(m.group("h")), int(m.group("m") or 0), (m.group("ap") or "").lower()
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz = _zone(m.group("tz"))
    local_now = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()
    mon = _MONTHS.get((m.group("mon") or "").lower()[:3])
    try:
        if mon:
            when = local_now.replace(month=mon, day=int(m.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
            if when.timestamp() < now - 86400:
                when = when.replace(year=when.year + 1)
        else:
            when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when.timestamp() <= now:
                when += timedelta(days=1)
    except ValueError:
        return None
    return when.timestamp()


def classify(status: Optional[int], text: Any = "", headers: Optional[dict] = None, *, now: Optional[float] = None) -> Optional[Signal]:
    """A Signal when the failure means the provider is out of capacity, else None."""
    now = time.time() if now is None else now
    low = _flatten(text)
    status = int(status) if isinstance(status, int) else None
    if status in (401, 403) and not _CAP.search(low):
        return None                     # an auth problem, not capacity
    if status == 529 or "overloaded_error" in low:
        return None                     # the provider is busy, not our allowance
    hdr = {str(k).lower(): v for k, v in (headers or {}).items()}
    if str(hdr.get("anthropic-ratelimit-unified-status", "")).lower() == "rejected":
        cap = True
    else:
        cap = bool(_CAP.search(low)) or status == 402
        if not cap and _CAP_WEAK.search(low):
            cap = not re.search(r"per ?minute|per_minute|perminute", low)
    rate = bool(_RATE.search(low)) or status == 429
    if not cap and not rate:
        return None
    wait = retry_after(low, hdr)
    reset = reset_time(low, hdr, now)
    if cap:
        if reset is None and wait is not None and wait > 0:
            reset = now + wait
        if reset is not None and reset <= now:
            reset = None
        return Signal(USAGE_CAP, reset_at=reset, retry_after_s=wait, status=status, reason=_reason(low, "usage limit reached"))
    if reset is not None and wait is None and reset > now:
        wait = reset - now
    return Signal(RATE_LIMIT, retry_after_s=wait, status=status, reason=_reason(low, "rate limited"))


def _reason(low: str, default: str) -> str:
    m = re.search(r"\"message\"\s*:\s*\"([^\"]{3,200})", low)
    line = m.group(1) if m else re.sub(r"\s+", " ", low).strip()[:160]
    return (line or default)[:160]
