"""EscapeArtist/WatchDog CLI: run the red<->blue loop and print the vector-taxonomy coverage.

Local and model-free: it needs no dashboard and drives bot/redblue/ in-process. PROPOSE-ONLY — every
containment is shown as it would be proposed and passed through a dry-run ContainmentExecutor with no sinks,
so nothing live is ever executed and no code is ever patched. There is deliberately no flag that changes that.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from bot.redblue.containment import ContainmentExecutor
from bot.redblue.journal import IncidentJournal
from bot.redblue.redteam import RedTeam, run_loop
from bot.redblue.schemas import Incident, VectorClass
from bot.redblue.taxonomy import PROBES, VectorProbe
from bot.redblue.watchdog import WatchDog


def _probe_incident(probe: VectorProbe) -> tuple[bool, Optional[Incident]]:
    """One probe through run_loop on a fresh WatchDog: (detected, the trigger's incident if any)."""
    wd = WatchDog()
    rep = run_loop(RedTeam(probes=(probe,)), wd)
    inc = wd.incidents[-1] if wd.incidents else None
    return rep.detected == 1, inc


def run_report(probes: tuple[VectorProbe, ...] = PROBES) -> tuple[dict, list[Incident]]:
    """The loop result as a dict, plus each detected vector's trigger incident (for the journal)."""
    total = run_loop(RedTeam(probes=probes)).to_dict()
    executor = ContainmentExecutor(dry_run=True)          # no sinks: nothing can be executed live
    vectors: list[dict] = []
    incidents: list[Incident] = []
    for p in probes:
        detected, inc = _probe_incident(p)
        row = {"name": p.name, "vector_class": p.vector_class.value, "expect": p.expect,
               "detected": detected, "verdict": inc.verdict if inc else None,
               "signature": inc.matched_signature if inc else None, "containment": None}
        if detected and inc is not None:
            res = executor.execute(inc)
            row["containment"] = {"action": res.action, "status": "PROPOSED", "dry_run": True,
                                  "executed": res.executed, "outcome": res.outcome}
            incidents.append(inc)
        vectors.append(row)
    return {**total, "mode": "propose-only, dry-run", "vectors": vectors}, incidents


def coverage_report(probes: tuple[VectorProbe, ...] = PROBES) -> dict:
    """Per-vector-class coverage: run_loop over each class's probes, plus the overall number."""
    overall = run_loop(RedTeam(probes=probes)).to_dict()
    categories: list[dict] = []
    for vc in VectorClass:
        subset = tuple(p for p in probes if p.vector_class == vc)
        if not subset:                                     # no probe yet: unmeasured, not 100%
            categories.append({"vector_class": vc.value, "total": 0, "detected": 0, "missed": [], "coverage": None})
            continue
        categories.append({"vector_class": vc.value, **run_loop(RedTeam(probes=subset)).to_dict()})
    return {"overall": overall, "categories": categories}


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _run(args) -> int:
    report, incidents = run_report()
    if args.journal:
        journal = IncidentJournal(args.journal)
        findings = RedTeam().enumerate()
        for f in findings:
            journal.record(f)
        for inc in incidents:
            journal.record(inc)
        report["journal"] = {"path": str(journal.path), "findings": len(findings), "incidents": len(incidents)}
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        missed = report["missed"]
        print(f"red<->blue loop ({report['mode']}): {report['total']} tried, {report['detected']} detected, "
              f"{len(missed)} missed  (coverage {_pct(report['coverage'])})")
        for v in report["vectors"]:
            if v["detected"]:
                c = v["containment"]
                print(f"  DETECTED  {v['name']:<28} {v['signature']:<30} -> {c['action']} [PROPOSED, dry-run]")
            else:
                print(f"  MISSED    {v['name']:<28} verdict {v['verdict'] or 'none'} (expected {v['expect']})")
        if "journal" in report:
            j = report["journal"]
            print(f"journal: +{j['findings']} findings, +{j['incidents']} incidents -> {j['path']}")
    return 0 if not report["missed"] else 1


def _coverage(args) -> int:
    rep = coverage_report()
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        print(f"{'vector class':<16} {'detected':>8} {'total':>5} {'coverage':>8}  missed")
        for c in rep["categories"]:
            print(f"{c['vector_class']:<16} {c['detected']:>8} {c['total']:>5} {_pct(c['coverage']):>8}  "
                  f"{', '.join(c['missed']) or '-'}")
        o = rep["overall"]
        print(f"{'overall':<16} {o['detected']:>8} {o['total']:>5} {_pct(o['coverage']):>8}")
    return 0 if not rep["overall"]["missed"] else 1


def add_parser(sub) -> None:
    rb = sub.add_parser("redblue", help="EscapeArtist/WatchDog: the red<->blue loop and its coverage "
                                        "(local, model-free, propose-only)")
    rsub = rb.add_subparsers(dest="redblue_cmd", required=True)
    # --json is also accepted after the subcommand; SUPPRESS keeps the global --json from being reset.
    p = rsub.add_parser("run", help="run the loop: vectors tried, detected, missed, and the PROPOSED containments "
                                    "(exit 1 if any vector is missed)")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    p.add_argument("--journal", default=None, metavar="PATH",
                   help="also append the findings and detected incidents to this JSONL journal")
    p.set_defaults(redblue_func=_run)
    p = rsub.add_parser("coverage", help="the vector-taxonomy coverage benchmark, per class and overall "
                                         "(exit 1 if any vector is missed)")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    p.set_defaults(redblue_func=_coverage)


def run(args) -> int:
    try:
        return args.redblue_func(args)
    except Exception as exc:  # noqa: BLE001 — surfaced as a failed command, never a traceback
        print(f"redblue command failed: {exc}", file=sys.stderr)
        return 1
