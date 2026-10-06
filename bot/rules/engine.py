"""The rule engine: `abp_rules`, the sandboxed Rhai module ABP runs rules on, or nothing at all.

`abp_rules` (native/abp-rules) embeds Rhai with no filesystem, no network, no clock, no module
imports and no dynamic evaluation, and caps operations, call depth, string, array and map sizes and
expression depth. Everything a rule can say is therefore arithmetic, comparison and the built-in
string and collection methods - which is the point: a rule is a *decision*, not a program, and the
worst a broken one can do is answer wrongly.

This module is the whole of ABP's contact with it:

    available()                     is there an engine, and which one
    limits()                        what it enforces, for a page to show
    validate(source, kind)          does it compile, and where does it not
    evaluate(source, kind, ctx)     run it and return what it answered
    matches(source, ctx, kind)      run it and return yes or no
    test(source, kind, ctx)         run it against a sample and say how long it took

**Without the engine, rules are off and everything else is unchanged.** ABP must run perfectly well
with no native module built, so this imports `abp_rules` optionally and reports a clear reason when
it is not there (`ABP_NATIVE=0` turns every native module off, which is how both paths get tested).
Every integration point asks `available()` first; with no engine there is nothing to enforce, which
is the same position a person is in who has not written a rule yet.

Rules are compiled once and cached. The Rust side keeps the parsed AST keyed by source, and this
side keeps the `Program` wrapper so a rule evaluated on every turn does not re-enter the extension
at all.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("bot.rules")

KINDS = ("condition", "filter", "route")
DEFAULT_LIMITS = {
    "max_operations": 200_000,
    "max_call_levels": 16,
    "max_string_size": 65_536,
    "max_array_size": 4_096,
    "max_map_size": 1_024,
    "max_expr_depth": 64,
    "max_variables": 256,
    "max_source_chars": 8_192,
    "max_context_depth": 8,
}
MAX_SOURCE = 8_192
_PROGRAM_CACHE = 256

DISABLED_MESSAGE = (
    "rules are off: the abp_rules native module is not installed, so there is no sandboxed rule "
    "engine to run a rule on. Everything else works exactly as before; install it with "
    "`python native/abp-rules/build.py --venv <python.exe>` to turn rules on."
)


class RuleError(Exception):
    """A rule that would not compile or would not run. `line` and `column` are 0 when the engine
    could not say where, `limit` is the machine-readable reason and `phase` is "compile" or "run"."""

    def __init__(self, message: str, *, phase: str = "compile", line: int = 0, column: int = 0, limit: str = "") -> None:
        super().__init__(message)
        self.phase = phase
        self.line = int(line)
        self.column = int(column)
        self.limit = limit

    @property
    def where(self) -> str:
        return f"line {self.line}, column {self.column}" if self.line else "the whole rule"

    def as_dict(self) -> dict:
        """The shape the API returns, so a page can point at the exact character."""
        return {"message": str(self), "phase": self.phase, "line": self.line, "column": self.column, "limit": self.limit}


class RulesDisabled(RuleError):
    """Raised when a rule is asked to do anything and there is no engine to do it with."""

    def __init__(self, message: str = DISABLED_MESSAGE) -> None:
        super().__init__(message, phase="disabled")


_native: Any = None
_reason: str = ""
_checked = False


def _env_disabled() -> bool:
    return os.environ.get("ABP_NATIVE", "1").strip().lower() in ("0", "false", "no", "off")


def _load() -> None:
    """Import `abp_rules` once, and remember why it is not there when it is not."""
    global _native, _reason, _checked
    if _checked:
        return
    _checked = True
    if _env_disabled():
        _reason = "ABP_NATIVE=0 turned the native modules off"
        logger.info("rules are off: %s", _reason)
        return
    try:
        import abp_rules  # noqa: PLC0415 - deliberately optional; ABP runs without it
    except ImportError as exc:
        _reason = f"abp_rules is not installed ({exc})"
        logger.info("rules are off: %s", _reason)
        return
    _native = abp_rules
    _reason = ""
    logger.info("rules: using the sandboxed rule engine %s (limits: %s)", abp_rules.__version__,
                ", ".join(f"{k}={v}" for k, v in sorted(abp_rules.limits().items())))


def available() -> bool:
    """Whether there is a rule engine to run a rule on."""
    _load()
    return _native is not None


def backend() -> str:
    """Which implementation is active: "abp_rules", or "disabled" with a reason."""
    _load()
    return "abp_rules" if _native is not None else "disabled"


def status() -> dict:
    """Everything a page needs to show whether rules are on, and what they may do."""
    _load()
    out: dict = {
        "available": _native is not None,
        "backend": "abp_rules" if _native is not None else "disabled",
        "kinds": list(KINDS),
        "limits": limits(),
        "reason": _reason,
    }
    if _native is not None:
        out["engine_version"] = _native.__version__
        hits, misses, entries = _native.cache_stats()
        out["cached_rules"] = entries
        out["cache_hits"] = hits
        out["cache_misses"] = misses
    return out


def limits() -> dict:
    """The limits in force. The engine's own when there is one, the documented ones when not."""
    _load()
    if _native is not None:
        return dict(_native.limits())
    return dict(DEFAULT_LIMITS)


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise RuleError(f"a rule's kind is {' or '.join(KINDS)}, not {kind!r}", phase="compile", limit="wrong_type")
    return kind


def _translate(exc: Exception, phase: str) -> RuleError:
    """The engine's own exception, as a RuleError that keeps its position and reason."""
    if isinstance(exc, RuleError):
        return exc
    return RuleError(str(exc), phase=phase,
                     line=int(getattr(exc, "line", 0) or 0), column=int(getattr(exc, "column", 0) or 0),
                     limit=str(getattr(exc, "limit", "") or ""))


_programs: dict[tuple[str, str], Any] = {}


def program(source: str, kind: str = "condition") -> Any:
    """The compiled rule, from the cache when this exact source was compiled before."""
    _load()
    if _native is None:
        raise RulesDisabled()
    _check_kind(kind)
    if len(source) > MAX_SOURCE:
        raise RuleError(f"the rule is {len(source)} characters long, over the {MAX_SOURCE} character limit",
                        phase="compile", limit="size")
    key = (source, kind)
    found = _programs.get(key)
    if found is not None:
        return found
    try:
        compiled = _native.compile(source, kind)
    except Exception as exc:  # noqa: BLE001 - every engine failure becomes one RuleError shape
        raise _translate(exc, "compile") from exc
    if len(_programs) >= _PROGRAM_CACHE:
        _programs.clear()
    _programs[key] = compiled
    return compiled


def validate(source: str, kind: str = "condition") -> dict:
    """Does it compile? Raises RuleError with a position when it does not; returns the limits and
    the engine's name otherwise, so a caller has something to show next to a rule that is fine."""
    compiled = program(source, kind)
    return {"ok": True, "kind": compiled.kind, "limits": dict(compiled.limits), "backend": backend()}


def evaluate(source: str, kind: str, context: dict) -> Any:
    """Run the rule and return whatever it answered: a bool for a condition or a filter, a value
    (usually a name) for a route."""
    compiled = program(source, kind)
    try:
        return compiled.evaluate(context)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "run") from exc


def matches(source: str, context: dict, kind: str = "condition") -> bool:
    """Run the rule and return its yes or no. The `kind` is the rule's own, so a filter that
    answers a string is refused by name as a filter rather than as somebody else's condition."""
    compiled = program(source, kind)
    try:
        return compiled.matches(context)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc, "run") from exc


def test(source: str, kind: str, context: dict) -> dict:
    """Run a rule against a sample context and say what came back and how long it took. What the
    API's test button and every "try it" in a UI calls."""
    started = time.monotonic()
    value = evaluate(source, kind, context)
    return {
        "ok": True,
        "kind": kind,
        "result": value,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "backend": backend(),
    }


def is_enabled() -> bool:
    """True when rules may be stored and used at all. Storing a rule that could never run would be
    worse than refusing to store it."""
    return available()


def reset_cache() -> None:
    """Forget the compiled programs this process holds. Only ever wanted by a test."""
    global _native, _reason, _checked

    _programs.clear()
    _native, _reason, _checked = None, "", False