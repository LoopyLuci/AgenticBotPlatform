"""Durable, append-only persistence for WatchDog incidents (and red-team findings).

A WatchDog in a live run holds incidents in memory only (bot/redblue/watchdog.py); this makes them
survive the process so they can be reviewed, charted, or correlated later. Append-only JSONL — one record
per line — is crash-safe (a half-written trailing line is skipped on read, never corrupting earlier ones)
and needs no schema migration. Records round-trip through the same validated to_dict/from_dict as the
schemas, so a corrupt line is dropped rather than trusted. Nothing here executes containment or patches
anything: it only records what WatchDog already decided. Wire it as a WatchDog on_incident callback to
persist as incidents arrive — see IncidentJournal.record / as_watchdog_callback.
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterator, Optional, Union

from bot.redblue.schemas import Finding, Incident, SchemaError

_Record = Union[Incident, Finding]


class IncidentJournal:
    """Append-only JSONL store for Incidents and Findings. Thread-safe appends; tolerant reads."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, item: _Record) -> None:
        """Append one incident/finding as a JSON line. The record is tagged with its kind so a mixed
        journal reads back unambiguously; a persisted_at wall-clock is added for ordering across runs."""
        kind = "incident" if isinstance(item, Incident) else "finding"
        row = {"_kind": kind, "_persisted_at": time.time(), **item.to_dict()}
        line = json.dumps(row, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def as_watchdog_callback(self) -> Callable[[Incident], None]:
        """A callback for WatchDog(on_incident=...): every incident WatchDog records is also persisted."""
        return self.record

    def _iter_rows(self) -> Iterator[dict]:
        if not self.path.is_file():
            return
        with self.path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue                       # a torn trailing line from a crash — skip, don't fail the read

    def incidents(self) -> list[Incident]:
        """Every valid persisted Incident, in write order. A row that fails validation is skipped."""
        out: list[Incident] = []
        for row in self._iter_rows():
            if row.get("_kind") not in (None, "incident"):
                continue
            row = {k: v for k, v in row.items() if not k.startswith("_")}
            try:
                out.append(Incident.from_dict(row))
            except SchemaError:
                continue
        return out

    def findings(self) -> list[Finding]:
        out: list[Finding] = []
        for row in self._iter_rows():
            if row.get("_kind") != "finding":
                continue
            row = {k: v for k, v in row.items() if not k.startswith("_")}
            try:
                out.append(Finding.from_dict(row))
            except SchemaError:
                continue
        return out

    def summary(self) -> dict:
        """A dashboard-ready aggregate of the persisted incidents: totals by verdict, by signature, by
        proposed containment, and the last-seen timestamp — the numbers a WatchDog panel would show."""
        incs = self.incidents()
        by_verdict: Counter = Counter(i.verdict for i in incs)
        by_sig: Counter = Counter(i.matched_signature for i in incs if i.matched_signature)
        by_contain: Counter = Counter(i.containment_action for i in incs if i.containment_action)
        last_ts: Optional[float] = max((i.timestamp for i in incs), default=None)
        return {
            "total": len(incs),
            "by_verdict": dict(by_verdict),
            "by_signature": dict(by_sig),
            "by_containment": dict(by_contain),
            "escapes": by_verdict.get("escape", 0),
            "last_ts": last_ts,
        }
