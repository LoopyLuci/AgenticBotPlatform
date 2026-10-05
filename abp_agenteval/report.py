"""Reading, comparing and gating reports."""
from __future__ import annotations

import json
from pathlib import Path


def load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, data: dict) -> None:
    """Write a report, creating the folder it goes in. A live run is slow and expensive;
    losing it to FileNotFoundError on a directory the caller named and expected to exist is
    the last thing that should happen."""
    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    measured = report.get("measured", report["total"])
    lines = [f"agent eval - {report['mode']} - model {report['model']}",
             f"score {report['score']}%  ({report['passed']}/{measured} passed)  "
             f"{report['tokens']} tokens  {report['duration_ms']} ms"]
    if report.get("limited"):
        # A task the provider's limit stopped measures the limit, not the model - say so rather than
        # letting a free tier's 429 read as a model that could not do the work.
        lines.append(f"{report['limited']} of {report['total']} tasks did not measure the model (its "
                     f"allowance ran out); the score is over the {measured} that ran")
    lines.append("")
    for r in report["results"]:
        limited = r.get("limited", False)
        mark = "PASS" if r["passed"] else ("SKIP" if limited else "FAIL")
        lines.append(f"  {mark}  {r['id']:<22} {r['iterations']} calls  {r['duration_ms']} ms  {r['title']}")
        if not r["passed"]:
            if r.get("error"):
                lines.append(f"        error: {r['error']}")
            for c in r["checks"]:
                if not c["ok"]:
                    lines.append(f"        x {c['name']}  {c['detail']}")
    return "\n".join(lines)
