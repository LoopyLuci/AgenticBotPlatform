"""Reading, comparing and gating reports."""
from __future__ import annotations

import json
from pathlib import Path


def load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare(current: dict, baseline: dict, *, tolerance: float = 0.0) -> dict:
    """Regressions are what the gate fails on: a task that passed before and fails now,
    or a score drop beyond `tolerance` points. New tasks and fixes are reported, never failed."""
    before = {r["id"]: r["passed"] for r in baseline.get("results", [])}
    after = {r["id"]: r["passed"] for r in current.get("results", [])}
    regressed = sorted(i for i, ok in before.items() if ok and after.get(i) is False)
    missing = sorted(i for i in before if i not in after)
    fixed = sorted(i for i, ok in after.items() if ok and before.get(i) is False)
    new = sorted(i for i in after if i not in before)
    drop = round(float(baseline.get("score", 0)) - float(current.get("score", 0)), 1)
    return {"regressed": regressed, "missing": missing, "fixed": fixed, "new": new,
            "score_drop": drop, "ok": not regressed and not missing and drop <= tolerance}


def render(report: dict) -> str:
    lines = [f"agent eval - {report['mode']} - model {report['model']}",
             f"score {report['score']}%  ({report['passed']}/{report['total']} passed)  "
             f"{report['tokens']} tokens  {report['duration_ms']} ms", ""]
    for r in report["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        lines.append(f"  {mark}  {r['id']:<22} {r['iterations']} calls  {r['duration_ms']} ms  {r['title']}")
        if not r["passed"]:
            if r.get("error"):
                lines.append(f"        error: {r['error']}")
            for c in r["checks"]:
                if not c["ok"]:
                    lines.append(f"        x {c['name']}  {c['detail']}")
    return "\n".join(lines)
