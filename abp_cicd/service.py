"""The single definition of every response shape.

`Service` is what the HTTP routes call and what the CLI's local mode calls, and
`HttpTransport` (transport.py) mirrors its method names over HTTP. Because the
shapes live only here, every client — API, CLI, TUI, GUI, MCP — sees identical
data. tests/test_cicd_parity.py fails if a Service capability is missing from a
client.
"""
from __future__ import annotations

from typing import Optional

from . import queries
from .store import EventStore

# Every read capability. Clients must expose all of these.
CAPABILITIES = ("summary", "runs", "run", "explain", "step_stats", "decisions", "workers", "events", "chain")


class Service:
    def __init__(self, store: EventStore):
        self.store = store

    def summary(self) -> dict:
        return queries.summary(self.store)

    def runs(self, limit: int = 50, kind: Optional[str] = None) -> dict:
        return {"runs": queries.list_runs(self.store, limit=limit, kind=kind)}

    def run(self, run_id: str) -> Optional[dict]:
        return queries.get_run(self.store, run_id)

    def explain(self, run_id: str) -> Optional[dict]:
        text = queries.explain(self.store, run_id)
        return None if text is None else {"id": run_id, "text": text}

    def step_stats(self, name: Optional[str] = None, run_kind: Optional[str] = None, last_n: int = 50) -> dict:
        return {"steps": queries.step_stats(self.store, name=name, run_kind=run_kind, last_n=last_n)}

    def decisions(self, limit: int = 100, run_id: Optional[str] = None) -> dict:
        return {"decisions": queries.decisions(self.store, limit=limit, run_id=run_id)}

    def workers(self) -> dict:
        return {"workers": queries.workers(self.store)}

    def events(self, since: int = 0, limit: int = 200, kind: Optional[str] = None, run_id: Optional[str] = None) -> dict:
        evs = self.store.events(since_seq=since, limit=limit, kind=kind, run_id=run_id)
        return {"events": evs, "last_seq": evs[-1]["seq"] if evs else since}

    def chain(self) -> dict:
        return self.store.verify_chain()
