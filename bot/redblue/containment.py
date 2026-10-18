"""Reversible containment executor for WatchDog.

Maps an Incident's proposed action to a concrete, bounded, auditable result and either RECORDS it (dry_run,
the default) or invokes a registered sink. Only the reversible allowlist (detector.CONTAINMENT) is ever
executed; anything else is refused. A durable code patch is never performed here — that path is human-gated.
Default dry_run=True means wiring this into WatchDog changes no live state until someone turns it on and
supplies the real sinks.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from bot.redblue.detector import CONTAINMENT
from bot.redblue.schemas import Incident


@dataclass
class ContainmentResult:
    action: str
    target: str
    executed: bool
    outcome: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"action": self.action, "target": self.target, "executed": self.executed,
                "outcome": self.outcome, "ts": self.ts}


class ContainmentExecutor:
    def __init__(self, *, dry_run: bool = True,
                 sinks: Optional[dict[str, Callable[[Incident], str]]] = None):
        self.dry_run = dry_run
        self.sinks = dict(sinks or {})                 # action -> callable(incident)->outcome str
        self.audit: list[ContainmentResult] = []

    def execute(self, inc: Incident) -> ContainmentResult:
        action = inc.containment_action or "none"
        if action not in CONTAINMENT:                  # never run anything off the reversible allowlist
            return self._log(ContainmentResult(action, inc.actor, False, f"refused:not-reversible:{action}"))
        if action == "none":
            return self._log(ContainmentResult(action, inc.actor, False, "noop"))
        if self.dry_run:
            return self._log(ContainmentResult(action, inc.actor, False, f"dry-run:{action}"))
        sink = self.sinks.get(action)
        if sink is None:
            return self._log(ContainmentResult(action, inc.actor, False, f"unwired:{action}"))
        try:
            return self._log(ContainmentResult(action, inc.actor, True, sink(inc)))
        except Exception as exc:                       # a failing sink is surfaced, never raised into the loop
            return self._log(ContainmentResult(action, inc.actor, False, f"failed:{action}:{exc}"))

    def _log(self, res: ContainmentResult) -> ContainmentResult:
        self.audit.append(res)
        return res

    def as_watchdog_handlers(self) -> dict[str, Callable[[Incident], str]]:
        """Handlers dict for WatchDog(handlers=...); each routes through execute() so dry-run + audit apply."""
        return {a: (lambda inc, _a=a: self.execute(inc).outcome) for a in CONTAINMENT if a != "none"}
