"""Event schema — an ALLOW-LIST.

A field is recorded only if the schema for that event kind declares it, so a new
field cannot leak a secret into telemetry by accident. Anything unknown is
dropped, strings are length-capped and secret-shaped text is redacted as a second
layer of defence.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

MAX_STR = 500
MAX_INPUT_KEYS = 20

STATUSES = ("ok", "failed", "skipped", "running", "rolled_back", "aborted", "unknown")

# kind -> {field: type}. `inputs` is the one free-form field (flat scalars only).
KINDS: dict[str, dict[str, type]] = {
    "run.start": {"run_kind": str, "title": str, "version": str, "parent_run": str, "host": str,
                  "commit": str, "branch": str},
    "run.end": {"status": str, "duration_ms": int, "summary": str},
    "step.start": {"attempt": int},
    "step.end": {"status": str, "duration_ms": int, "attempt": int, "error": str, "detail": str,
                 "skipped_reason": str},
    "decision": {"actor": str, "decision": str, "reason": str, "rule": str, "confidence": float,
                 "inputs": dict},
    "worker.heartbeat": {"worker": str, "state": str, "model": str, "queue": int, "detail": str},
    "note": {"level": str, "message": str},
}

_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
]
_KEYED = re.compile(r"(?i)\b(token|secret|password|passwd|api[_\-]?key|authorization)\b(\s*[=:]\s*)(\S+)")
_HOME = Path.home().as_posix()


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    text = _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    if _HOME and len(_HOME) > 3:
        text = text.replace(_HOME, "~").replace(os.path.expanduser("~"), "~")
    return text


def _clean_str(value: Any) -> str:
    s = redact(str(value))
    return s if len(s) <= MAX_STR else s[: MAX_STR - 1] + "…"


def _clean_inputs(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in list(value.items())[:MAX_INPUT_KEYS]:
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[_clean_str(k)[:50]] = v
        elif isinstance(v, str):
            out[_clean_str(k)[:50]] = _clean_str(v)
    return out


def sanitize(kind: str, data: dict | None) -> dict:
    """Keep only declared fields, coerced to their declared type. Raises
    ValueError for an unknown kind (a programming error, not bad data)."""
    if kind not in KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    schema = KINDS[kind]
    out: dict[str, Any] = {}
    for name, value in (data or {}).items():
        typ = schema.get(name)
        if typ is None or value is None:
            continue
        try:
            if typ is str:
                out[name] = _clean_str(value)
            elif typ is int:
                out[name] = int(value)
            elif typ is float:
                out[name] = float(value)
            elif typ is dict:
                out[name] = _clean_inputs(value)
        except (TypeError, ValueError):
            continue
    if "status" in out and out["status"] not in STATUSES:
        out["status"] = "unknown"
    return out
