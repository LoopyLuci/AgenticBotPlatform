"""Instrumentation API used by the release and pipeline scripts.

    with recorder.start_run("pipeline", title="pre-push") as run:
        with run.step("python") as step:
            ...                      # exception -> step failed; otherwise ok
            step.set(status="skipped", skipped_reason="no changes")   # or set explicitly
        run.decision(actor="rules", decision="skip rust", reason="no src-tauri changes")

Every call is safe: if the store cannot be written the recorder silently becomes a
no-op (one warning), so instrumentation can never fail a build. Child processes
inherit ABP_CICD_RUN and record it as their `parent_run`, so a release links to the
pipeline gate it launched.
"""
from __future__ import annotations

import os
import secrets
import socket
import time
from typing import Any, Optional

from .store import EventStore, get_store

PARENT_ENV = "ABP_CICD_RUN"


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _outcome(exc_type, exc) -> str:
    """How a `with` block ended, as a status."""
    if exc_type is None:
        return "ok"
    if isinstance(exc, SystemExit):
        return "ok" if exc.code in (0, None) else "failed"
    if issubclass(exc_type, KeyboardInterrupt):
        return "aborted"
    return "failed"


class Step:
    def __init__(self, run: "Run", name: str, attrs: dict):
        self.run, self.name, self.attrs = run, name, attrs
        self.status: Optional[str] = None
        self.detail = ""
        self.error = ""
        self.skipped_reason = ""
        self._t0 = 0.0

    def set(self, *, status: Optional[str] = None, detail: str = "", skipped_reason: str = "") -> None:
        if status:
            self.status = status
        if detail:
            self.detail = detail
        if skipped_reason:
            self.skipped_reason = skipped_reason

    def __enter__(self) -> "Step":
        self._t0 = time.monotonic()
        self.run._emit("step.start", {"attempt": self.attrs.get("attempt", 1)}, step=self.name)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        outcome = _outcome(exc_type, exc)
        if outcome != "ok":
            self.error = f"{exc_type.__name__}: {exc}"
        self.run._emit("step.end", {
            "status": self.status if (self.status and outcome == "ok") else outcome,
            "duration_ms": _ms_since(self._t0), "attempt": self.attrs.get("attempt", 1),
            "error": self.error, "detail": self.detail, "skipped_reason": self.skipped_reason,
        }, step=self.name)
        return False  # never swallow the caller's exception


class Run:
    def __init__(self, store: Optional[EventStore], run_kind: str, **attrs: Any):
        self.store = store
        self.kind = run_kind
        self.attrs = attrs
        self.id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        self.final_status: Optional[str] = None
        self.summary = ""
        self._t0 = 0.0
        self._prev_env: Optional[str] = None

    def _emit(self, kind: str, data: dict, step: Optional[str] = None) -> None:
        if self.store is not None:
            self.store.safe_append(kind, data, run_id=self.id, step=step)

    def __enter__(self) -> "Run":
        self._t0 = time.monotonic()
        self._prev_env = os.environ.get(PARENT_ENV)
        data = {"run_kind": self.kind, "host": socket.gethostname(), **self.attrs}
        if self._prev_env and "parent_run" not in data:
            data["parent_run"] = self._prev_env
        self._emit("run.start", data)
        os.environ[PARENT_ENV] = self.id   # children record us as their parent
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        outcome = _outcome(exc_type, exc)
        if outcome != "ok" and not self.summary:
            self.summary = f"{exc_type.__name__}: {exc}"
        # An explicit finish() (e.g. "rolled_back") wins over how the block ended.
        self._emit("run.end", {"status": self.final_status or outcome,
                               "duration_ms": _ms_since(self._t0), "summary": self.summary})
        if self._prev_env is None:
            os.environ.pop(PARENT_ENV, None)
        else:
            os.environ[PARENT_ENV] = self._prev_env
        return False

    # -- api ----------------------------------------------------------------
    def step(self, name: str, **attrs: Any) -> Step:
        return Step(self, name, attrs)

    def record_step(self, name: str, status: str, duration_ms: int = 0, **data: Any) -> None:
        """For a step that was timed elsewhere or never ran (skipped)."""
        self._emit("step.end", {"status": status, "duration_ms": duration_ms, **data}, step=name)

    def decision(self, *, actor: str, decision: str, reason: str = "", rule: str = "",
                 confidence: Optional[float] = None, inputs: Optional[dict] = None) -> None:
        self._emit("decision", {"actor": actor, "decision": decision, "reason": reason, "rule": rule,
                                "confidence": confidence, "inputs": inputs or {}})

    def note(self, message: str, level: str = "info") -> None:
        self._emit("note", {"level": level, "message": message})

    def finish(self, status: str, summary: str = "") -> None:
        """Set the outcome for a run that ends without raising (e.g. returns 1)."""
        self.final_status = status
        if summary:
            self.summary = summary


def start_run(run_kind: str, store: Optional[EventStore] = None, **attrs: Any) -> Run:
    """A Run bound to the default store. If the store can't be opened the Run is
    a no-op rather than an error."""
    try:
        return Run(store or get_store(), run_kind, **attrs)
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        return Run(None, run_kind, **attrs)
