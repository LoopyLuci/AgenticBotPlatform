"""Read models over the event log.

Everything the API, CLI, TUI and GUI show comes from these functions and nothing
else, so every client sees identical data by construction. They are computed on
read from the events (no projection tables to drift or migrate); at this scale —
hundreds of runs — that is cheap, and it keeps the event log the single source
of truth.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Optional

from .store import EventStore

STALE_AFTER_S = 3 * 3600   # a run with no events for this long and no end is "stale", not "running"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def _fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "?"
    s = ms / 1000
    if s < 1:
        return f"{ms} ms"
    if s < 90:
        return f"{s:.1f} s"
    m, sec = divmod(int(s), 60)
    if m < 90:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _assemble(events: list[dict], now: float) -> dict:
    """One run from its events."""
    run: dict[str, Any] = {"id": None, "kind": None, "status": "running", "started": None, "ended": None,
                           "duration_ms": None, "attrs": {}, "summary": "", "steps": [], "decisions": [],
                           "notes": [], "last_event": None}
    steps: dict[str, dict] = {}
    order: list[str] = []
    for ev in events:
        d, k = ev["data"], ev["kind"]
        run["id"] = ev["run_id"]
        run["last_event"] = ev["ts"]
        if k == "run.start":
            run["started"] = ev["ts"]
            run["kind"] = d.get("run_kind")
            run["attrs"] = {a: v for a, v in d.items() if a != "run_kind"}
        elif k == "run.end":
            run["ended"] = ev["ts"]
            run["status"] = d.get("status", "unknown")
            run["duration_ms"] = d.get("duration_ms")
            run["summary"] = d.get("summary", "")
        elif k in ("step.start", "step.end"):
            name = ev["step"] or "?"
            if name not in steps:
                steps[name] = {"name": name, "status": "running", "started": None, "duration_ms": None,
                               "attempts": 0, "error": "", "detail": "", "skipped_reason": ""}
                order.append(name)
            st = steps[name]
            if k == "step.start":
                st["started"] = ev["ts"]
                st["attempts"] += 1
            else:
                st["status"] = d.get("status", "unknown")
                st["duration_ms"] = d.get("duration_ms")
                st["error"] = d.get("error", "")
                st["detail"] = d.get("detail", "")
                st["skipped_reason"] = d.get("skipped_reason", "")
                st["attempts"] = max(st["attempts"], d.get("attempt", 1) or 1)
        elif k == "decision":
            run["decisions"].append({"ts": ev["ts"], **d})
        elif k == "note":
            run["notes"].append({"ts": ev["ts"], **d})
    run["steps"] = [steps[n] for n in order]
    if run["ended"] is None and run["last_event"] and now - run["last_event"] > STALE_AFTER_S:
        run["status"] = "stale"
    if run["duration_ms"] is None and run["started"]:
        run["duration_ms"] = int(((run["ended"] or run["last_event"] or now) - run["started"]) * 1000)
    return run


def get_run(store: EventStore, run_id: str, now: Optional[float] = None) -> Optional[dict]:
    evs = store.events(run_id=run_id, limit=5000)
    return _assemble(evs, now or time.time()) if evs else None


def list_runs(store: EventStore, limit: int = 50, kind: Optional[str] = None, now: Optional[float] = None) -> list[dict]:
    """Newest first. Each entry is a compact run (no step list)."""
    now = now or time.time()
    starts = store.select(
        "SELECT run_id FROM events WHERE kind = 'run.start' ORDER BY seq DESC LIMIT ?", (max(1, min(limit * 3, 1000)),))
    out: list[dict] = []
    for row in starts:
        run = get_run(store, row["run_id"], now)
        if not run or (kind and run["kind"] != kind):
            continue
        out.append({k: run[k] for k in ("id", "kind", "status", "started", "ended", "duration_ms", "summary")}
                   | {"attrs": run["attrs"], "steps": len(run["steps"]),
                      "failed_steps": sum(1 for s in run["steps"] if s["status"] == "failed")})
        if len(out) >= limit:
            break
    return out


def step_stats(store: EventStore, name: Optional[str] = None, run_kind: Optional[str] = None,
               last_n: int = 50) -> dict[str, dict]:
    """Per-step duration statistics over the most recent `last_n` finished
    executions of each step. Steps that were skipped are counted, not timed."""
    rows = store.select(
        "SELECT e.step AS step, e.data AS data, e.run_id AS run_id FROM events e "
        "WHERE e.kind = 'step.end' AND e.step IS NOT NULL " + ("AND e.step = ? " if name else "") +
        "ORDER BY e.seq DESC LIMIT 20000", (name,) if name else ())
    kinds: dict[str, Optional[str]] = {}
    if run_kind:
        for r in store.select("SELECT run_id, data FROM events WHERE kind = 'run.start'"):
            kinds[r["run_id"]] = json.loads(r["data"]).get("run_kind")
    per: dict[str, list[dict]] = {}
    for r in rows:
        if run_kind and kinds.get(r["run_id"]) != run_kind:
            continue
        bucket = per.setdefault(r["step"], [])
        if len(bucket) < last_n:
            bucket.append(json.loads(r["data"]))
    stats: dict[str, dict] = {}
    for step, items in per.items():
        durations = [i["duration_ms"] for i in items if i.get("status") == "ok" and i.get("duration_ms") is not None]
        stats[step] = {
            "n": len(items),
            "ok": sum(1 for i in items if i.get("status") == "ok"),
            "failed": sum(1 for i in items if i.get("status") == "failed"),
            "skipped": sum(1 for i in items if i.get("status") == "skipped"),
            "p50_ms": int(_percentile(durations, 50)), "p95_ms": int(_percentile(durations, 95)),
            "max_ms": int(max(durations)) if durations else 0,
            "last_ms": items[0].get("duration_ms"), "last_status": items[0].get("status"),
        }
    return stats


def summary(store: EventStore, now: Optional[float] = None) -> dict:
    now = now or time.time()
    runs = list_runs(store, limit=200, now=now)
    by_kind: dict[str, dict] = {}
    for r in runs:
        k = by_kind.setdefault(r["kind"] or "?", {"runs": 0, "ok": 0, "failed": 0, "running": 0, "last": None})
        k["runs"] += 1
        if r["status"] == "ok":
            k["ok"] += 1
        elif r["status"] in ("failed", "aborted", "rolled_back"):
            k["failed"] += 1
        elif r["status"] == "running":
            k["running"] += 1
        if k["last"] is None:
            k["last"] = {"id": r["id"], "status": r["status"], "started": r["started"]}
    chain = store.verify_chain()
    return {"events": store.count(), "runs_considered": len(runs), "by_kind": by_kind,
            "active": [r for r in runs if r["status"] == "running"],
            "chain": {"ok": chain["ok"], "count": chain["count"], "first_bad_seq": chain["first_bad_seq"]},
            "workers": workers(store, now)}


def workers(store: EventStore, now: Optional[float] = None, stale_after_s: float = 120.0) -> list[dict]:
    """Latest heartbeat per ML/pipeline worker. A worker silent for
    `stale_after_s` is reported as `stale`, whatever it last said."""
    now = now or time.time()
    rows = store.select(
        "SELECT data, ts FROM events WHERE kind = 'worker.heartbeat' ORDER BY seq DESC LIMIT 2000")
    seen: dict[str, dict] = {}
    for r in rows:
        d = json.loads(r["data"])
        w = d.get("worker")
        if w and w not in seen:
            age = now - r["ts"]
            seen[w] = {"worker": w, "state": "stale" if age > stale_after_s else d.get("state", "unknown"),
                       "reported_state": d.get("state"), "model": d.get("model", ""), "queue": d.get("queue"),
                       "detail": d.get("detail", ""), "last_seen": r["ts"], "age_s": round(age, 1)}
    return sorted(seen.values(), key=lambda x: x["worker"])


def decisions(store: EventStore, limit: int = 100, run_id: Optional[str] = None) -> list[dict]:
    evs = store.events(kind="decision", run_id=run_id, limit=limit, descending=True)
    return [{"ts": e["ts"], "run_id": e["run_id"], **e["data"]} for e in evs]


def explain(store: EventStore, run_id: str, now: Optional[float] = None) -> Optional[str]:
    """A plain-language account of a run, generated deterministically from its
    events (no model involved)."""
    run = get_run(store, run_id, now)
    if run is None:
        return None
    what = [run["attrs"].get("version"), run["attrs"].get("title")]
    head = " ".join(str(x) for x in [run["kind"] or "run", *what] if x)
    lines = [f"{head} ({run['id']}) {run['status']}"
             + (f" after {_fmt_ms(run['duration_ms'])}." if run["duration_ms"] is not None else ".")]
    if run["summary"]:
        lines.append(f"Summary: {run['summary']}")
    steps = run["steps"]
    if steps:
        ran = [s for s in steps if s["status"] not in ("skipped",) and s["duration_ms"] is not None]
        if ran:
            slow = max(ran, key=lambda s: s["duration_ms"])
            total = sum(s["duration_ms"] for s in ran) or 1
            lines.append(f"{len(ran)} step(s) ran; the slowest was '{slow['name']}' at {_fmt_ms(slow['duration_ms'])} "
                         f"({100 * slow['duration_ms'] // total}% of step time).")
        for s in steps:
            if s["status"] == "skipped":
                lines.append(f"'{s['name']}' was skipped" + (f": {s['skipped_reason']}." if s["skipped_reason"] else "."))
            elif s["status"] == "failed":
                why = s["error"] or s["detail"]
                lines.append(f"'{s['name']}' FAILED" + (f": {why}" if why else "") + ".")
            if s["attempts"] and s["attempts"] > 1:
                lines.append(f"'{s['name']}' needed {s['attempts']} attempts.")
    for d in run["decisions"]:
        why = f" because {d['reason']}" if d.get("reason") else ""
        conf = f" (confidence {d['confidence']:.2f})" if d.get("confidence") is not None else ""
        lines.append(f"{d.get('actor', '?')} decided to {d.get('decision', '?')}{why}{conf}.")
    for n in run["notes"]:
        if n.get("level") in ("warn", "error"):
            lines.append(f"[{n['level']}] {n.get('message', '')}")
    return "\n".join(lines)
