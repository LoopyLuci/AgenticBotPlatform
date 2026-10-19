"""Red/blue (EscapeArtist/WatchDog) API: a read-only view of the persisted incident journal.

    GET /api/redblue/summary?limit=   counts by verdict and vector class, the newest incidents, red-team findings,
                                      and the containment WatchDog PROPOSED for each (dry-run, never executed)

Read-only by design: nothing here executes containment, patches code, or writes the journal. Proposed containments
are computed through a ContainmentExecutor pinned to dry_run with no sinks, so the outcome is always 'dry-run:<action>'
(or 'refused:...' for anything off the reversible allowlist). Security telemetry, so it needs the desktop dashboard
token, not a paired device's key."""
from __future__ import annotations

from collections import Counter
from typing import Callable

from fastapi import Depends, FastAPI, Query


def summarize(journal, limit: int = 20) -> dict:
    """The summary payload for one IncidentJournal. Pure read: the journal file is only opened for reading."""
    from bot.redblue.containment import ContainmentExecutor
    from bot.redblue.taxonomy import vector_class_of

    incs = journal.incidents()
    findings = journal.findings()
    recent = sorted(incs, key=lambda i: i.timestamp, reverse=True)[:limit]
    dry = ContainmentExecutor(dry_run=True, sinks={})          # pinned: proposals only, nothing is wired to run
    proposed = []
    for inc in recent:
        if not inc.containment_action or inc.containment_action == "none":
            continue
        res = dry.execute(inc).to_dict()
        proposed.append({"incident_ts": inc.timestamp, "signature": inc.matched_signature, **res})
    by_verdict = Counter(i.verdict for i in incs)
    return {
        "total": len(incs),
        "by_verdict": dict(by_verdict),
        "by_vector_class": dict(Counter(vector_class_of(i.matched_signature) for i in incs)),
        "escapes": by_verdict.get("escape", 0),
        "last_ts": max((i.timestamp for i in incs), default=None),
        "recent": [{**i.to_dict(), "vector_class": vector_class_of(i.matched_signature)} for i in recent],
        "proposed_containments": proposed,
        "findings": {
            "total": len(findings),
            "by_vector_class": dict(Counter(f.vector_class.value for f in findings)),
            "breached": sum(1 for f in findings if f.breached),
        },
        "dry_run": True,
    }


def register(app: FastAPI, read_auth: Callable) -> None:
    from bot.redblue import journal as journal_mod

    read = [Depends(read_auth)]

    @app.get("/api/redblue/summary", dependencies=read)
    def redblue_summary(limit: int = Query(20, ge=1, le=200)):
        from bot.redblue import live

        return {**summarize(journal_mod.default_journal(), limit), "live": live.stats()}   # the passive live WatchDog's feed
