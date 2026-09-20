"""Agent traces — what the native agent actually did, recorded once, read by
everything that needs to know (the eval harness, dashboards, cost reports).

One append-only, hash-chained store (the same engine the CI/CD platform uses,
with its own allow-list schema so a new field can't leak a secret by accident).
A trace records *shape*, not content: which tool ran, how long it took, whether it
was approved, how many characters went in and out, and a short redacted target
(a path, or the start of a shell command). It never stores prompts, replies, file
contents or tool output — those already live in the session history, and copying
them here would create a second place secrets can sit.

Recording is best-effort and never raises: a full disk or a locked file must not
fail a user's turn. Turn it off with `ABP_AGENT_TRACE=0` or
`native_agent.trace.enabled: false`.

Runs nest. A sub-agent's `ask()` starts its own run whose `parent_run` is the
run that spawned it, so a swarm reads as a tree.
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from abp_cicd.store import EventStore

logger = logging.getLogger("bot.agent_runtime.trace")

TRACE_KINDS: dict[str, dict[str, type]] = {
    "agent.run.start": {"agent": str, "model": str, "transport": str, "instance_id": int, "session": str,
                        "tools": int, "effort": str, "parent_run": str, "source": str},
    "agent.run.end": {"status": str, "duration_ms": int, "iterations": int, "tokens": int, "error": str},
    "llm.call": {"iteration": int, "model": str, "duration_ms": int, "tokens": int, "tool_calls": int,
                 "stop": str, "cache_read": int, "cache_create": int, "streamed": int},
    "tool.call": {"tool": str, "status": str, "duration_ms": int, "approval": str, "error": str,
                  "target": str, "arg_chars": int, "output_chars": int, "read_only": int},
    "approval": {"tool": str, "decision": str, "reason": str},
    "compaction": {"messages": int, "before_chars": int},
    "note": {"level": str, "message": str},
}

_current: contextvars.ContextVar[Optional["Trace"]] = contextvars.ContextVar("agent_trace", default=None)
_stores: dict[str, EventStore] = {}


def db_path() -> Path:
    explicit = os.environ.get("ABP_AGENT_TRACE_DB", "").strip()
    if explicit:
        return Path(explicit)
    from bot.envfile import PROJECT_ROOT

    return PROJECT_ROOT / "data" / "agent" / "traces.db"


def enabled() -> bool:
    if os.environ.get("ABP_AGENT_TRACE", "").strip() in ("0", "false", "off", "no"):
        return False
    try:
        from bot.config import config

        cfg = (config.current.get("native_agent") or {}).get("trace") or {}
        return bool(cfg.get("enabled", True))
    except Exception:  # noqa: BLE001 — config trouble must not decide whether we can run
        return True


def get_store(path: Optional[Path] = None) -> EventStore:
    key = str(path or db_path())
    if key not in _stores:
        _stores[key] = EventStore(key, kinds=TRACE_KINDS)
    return _stores[key]


def current() -> Optional["Trace"]:
    return _current.get()


def _target(tool: str, tool_input: dict) -> str:
    """A short, human-checkable hint about what the call touched. The store's
    redaction runs over it again, so a token pasted into a command is masked."""
    if not isinstance(tool_input, dict):
        return ""
    if tool == "run_shell":
        return str(tool_input.get("command", ""))[:200]
    for key in ("path", "name", "url", "pattern", "query", "instance_id"):
        if tool_input.get(key) is not None:
            return str(tool_input[key])[:200]
    return ""


class Trace:
    """One agent run. Every method swallows its own failures."""

    def __init__(self, *, store: Optional[EventStore] = None, **start: Any):
        self.run_id = uuid.uuid4().hex[:16]
        self.started = time.monotonic()
        self.iterations = 0
        self.tokens = 0
        self._store = store
        self._token: Optional[contextvars.Token] = None
        parent = _current.get()
        self.parent_run = parent.run_id if parent else None
        if self.parent_run:
            start["parent_run"] = self.parent_run
        self._emit("agent.run.start", start)

    # -- plumbing ----------------------------------------------------------
    def _emit(self, kind: str, data: dict) -> None:
        try:
            store = self._store or get_store()
            store.safe_append(kind, data, run_id=self.run_id)
        except Exception:  # noqa: BLE001
            logger.debug("trace emit failed", exc_info=True)

    def __enter__(self) -> "Trace":
        self._token = _current.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._token is not None:
            _current.reset(self._token)
            self._token = None
        return False

    # -- events ------------------------------------------------------------
    def llm_call(self, *, model: str, duration_ms: int, tokens: Optional[int] = None, tool_calls: int = 0,
                 cache_read: Optional[int] = None, cache_create: Optional[int] = None, streamed: bool = False) -> None:
        self.iterations += 1
        self.tokens += tokens or 0
        self._emit("llm.call", {"iteration": self.iterations, "model": model, "duration_ms": duration_ms,
                                "tokens": tokens, "tool_calls": tool_calls,
                                "stop": "tool_use" if tool_calls else "end_turn",
                                "cache_read": cache_read, "cache_create": cache_create,
                                "streamed": 1 if streamed else 0})

    def tool_call(self, tool: str, tool_input: dict, *, status: str, duration_ms: int, output: str = "",
                  approval: str = "none", error: str = "", read_only: Optional[bool] = None) -> None:
        self._emit("tool.call", {"tool": tool, "status": status, "duration_ms": duration_ms, "approval": approval,
                                 "error": error, "target": _target(tool, tool_input),
                                 "arg_chars": len(str(tool_input)), "output_chars": len(output or ""),
                                 "read_only": None if read_only is None else int(read_only)})

    def approval(self, tool: str, decision: str, reason: str = "") -> None:
        self._emit("approval", {"tool": tool, "decision": decision, "reason": reason})

    def compaction(self, messages: int, before_chars: int) -> None:
        self._emit("compaction", {"messages": messages, "before_chars": before_chars})

    def note(self, message: str, level: str = "info") -> None:
        self._emit("note", {"level": level, "message": message})

    def end(self, status: str = "ok", error: str = "") -> None:
        self._emit("agent.run.end", {"status": status, "error": error, "iterations": self.iterations,
                                     "tokens": self.tokens,
                                     "duration_ms": int((time.monotonic() - self.started) * 1000)})


def start(**fields: Any) -> Optional[Trace]:
    """A new run, or None when tracing is off. Use as `with trace.start(...) as t:`
    guarded by `if t`, or via `begin()` which always returns something usable."""
    if not enabled():
        return None
    try:
        return Trace(**fields)
    except Exception:  # noqa: BLE001
        logger.debug("could not start a trace", exc_info=True)
        return None


class _Null:
    """Stands in when tracing is off so call sites need no `if trace:` noise."""

    run_id = None
    parent_run = None
    iterations = 0

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def __getattr__(self, name): return lambda *a, **k: None


NULL = _Null()


def begin(**fields: Any):
    return start(**fields) or NULL


def active():
    """The current run, or a no-op object; safe to call from anywhere."""
    return _current.get() or NULL


# ---- reading ---------------------------------------------------------------
def run_events(run_id: str, store: Optional[EventStore] = None) -> list[dict]:
    return (store or get_store()).events(run_id=run_id, limit=5000)


def summarize(run_id: str, store: Optional[EventStore] = None) -> dict:
    """A run reduced to what evals and dashboards ask about."""
    evs = run_events(run_id, store)
    tools = [e for e in evs if e["kind"] == "tool.call"]
    start_ev = next((e for e in evs if e["kind"] == "agent.run.start"), None)
    end_ev = next((e for e in evs if e["kind"] == "agent.run.end"), None)
    llm = [e for e in evs if e["kind"] == "llm.call"]
    return {
        "run_id": run_id,
        "status": (end_ev or {}).get("data", {}).get("status", "running"),
        "duration_ms": (end_ev or {}).get("data", {}).get("duration_ms"),
        "iterations": len(llm),
        "tokens": sum(int(e["data"].get("tokens") or 0) for e in llm),
        "tool_calls": [{"tool": e["data"].get("tool"), "status": e["data"].get("status"),
                        "target": e["data"].get("target", "")} for e in tools],
        "tool_counts": {t: sum(1 for e in tools if e["data"].get("tool") == t)
                        for t in sorted({e["data"].get("tool") for e in tools})},
        "denied": sum(1 for e in tools if e["data"].get("status") == "denied"),
        "errors": sum(1 for e in tools if e["data"].get("status") == "failed"),
        "start": (start_ev or {}).get("data", {}),
    }
