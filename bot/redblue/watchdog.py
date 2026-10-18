"""WatchDog P1 runtime: the always-on monitor that ties the pieces together.

ingest raw telemetry -> normalize to an Event -> run the rule detector -> record any Incident -> surface the
PROPOSED reversible containment. Side-effect-free by default: observe() classifies and records but executes
nothing unless a containment handler is explicitly wired, and even then only the reversible allowlist — a
durable code patch is never auto-applied (that path is human-gated; see docs/red-blue-escapeartist-watchdog.md).
The slow remediator/patcher is a separate, later component.
"""
from __future__ import annotations

from typing import Callable, Optional

from bot.redblue.detector import Detector
from bot.redblue.events import Event, EventFeed
from bot.redblue.schemas import Incident


class WatchDog:
    def __init__(self, detector: Optional[Detector] = None, *, feed: Optional[EventFeed] = None,
                 max_incidents: int = 1000, on_incident: Optional[Callable[[Incident], None]] = None,
                 handlers: Optional[dict[str, Callable[[Incident], str]]] = None):
        self.detector = detector or Detector()
        self.feed = feed or EventFeed()
        self.incidents: list[Incident] = []
        self.max_incidents = max_incidents
        self.on_incident = on_incident
        # containment_action -> callable(incident)->str. Wired later and gated to the reversible allowlist;
        # empty by default (propose-only), so the monitor never changes live state on its own.
        self.handlers = handlers or {}

    def _record(self, inc: Optional[Incident]) -> Optional[Incident]:
        if inc is None:
            return None
        self.incidents.append(inc)
        del self.incidents[:-self.max_incidents]
        if self.on_incident:
            try:
                self.on_incident(inc)
            except Exception:                     # a bad callback must never break monitoring
                pass
        return inc

    def observe(self, raw: dict, source: str) -> Optional[Incident]:
        """Ingest one raw telemetry record; returns the Incident if it looks like an escape attempt."""
        ev = self.feed.ingest(raw, source)
        return self._record(self.detector.inspect(ev, self.feed))

    def observe_event(self, ev: Event) -> Optional[Incident]:
        """Ingest an already-normalized Event (e.g. from an in-process source)."""
        self.feed.add(ev)
        return self._record(self.detector.inspect(ev, self.feed))

    def contain(self, inc: Incident) -> str:
        """Apply the proposed reversible containment IF a handler is wired; otherwise record it as proposed.
        Returns a short outcome string (propose-only by default: 'proposed:<action>')."""
        action = inc.containment_action or "none"
        handler = self.handlers.get(action)
        if handler is None:
            return f"proposed:{action}"
        try:
            return handler(inc)
        except Exception as exc:                  # containment failure is surfaced, never raised into the loop
            return f"failed:{action}:{exc}"

    def recent_incidents(self, n: int = 100) -> list[Incident]:
        return self.incidents[-n:]

    def summary(self) -> dict:
        """Coverage snapshot for the red/blue dashboard: counts by verdict and by matched signature."""
        by_verdict: dict[str, int] = {}
        by_signature: dict[str, int] = {}
        for i in self.incidents:
            by_verdict[i.verdict] = by_verdict.get(i.verdict, 0) + 1
            key = i.matched_signature or "?"
            by_signature[key] = by_signature.get(key, 0) + 1
        return {"total": len(self.incidents), "by_verdict": by_verdict, "by_signature": by_signature}
