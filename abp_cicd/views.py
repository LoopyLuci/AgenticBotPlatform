"""View models shared by every interactive client (TUI and GUI).

Each panel is defined ONCE here: its title, which service capabilities it shows,
and how its data is loaded and shaped. Clients only render these shapes, so the
TUI and the GUI cannot drift apart; tests/test_cicd_clients.py fails if a client
lacks a panel or if some service capability is shown nowhere.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .service import CAPABILITIES

# panel id -> (title, capabilities it shows). Order is the tab order.
PANELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "overview": ("Overview", ("summary",)),
    "runs": ("Runs", ("runs", "run", "explain")),
    "steps": ("Step timing", ("step_stats",)),
    "decisions": ("Decisions", ("decisions",)),
    "workers": ("ML workers", ("workers",)),
    "events": ("Live events", ("events",)),
    "integrity": ("Integrity", ("chain",)),
}

# Semantic colours; each toolkit maps these to its own palette.
STATUS_STYLE = {"ok": "green", "failed": "red", "skipped": "grey", "running": "blue", "stale": "orange",
                "rolled_back": "orange", "aborted": "orange", "unknown": "grey", "serving": "green",
                "training": "blue", "idle": "grey", "degraded": "orange"}

EVENT_TAIL = 300


def uncovered_capabilities() -> list[str]:
    """Service capabilities no panel shows (must be empty)."""
    shown = {c for _title, caps in PANELS.values() for c in caps}
    return sorted(set(CAPABILITIES) - shown)


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "?"
    s = ms / 1000
    if s < 1:
        return f"{int(ms)} ms"
    if s < 90:
        return f"{s:.1f} s"
    m, sec = divmod(int(s), 60)
    if m < 90:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def fmt_time(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def gantt(run: dict, now: Optional[float] = None) -> dict:
    """Bars for a run's steps, positioned on a shared time axis. A step that
    recorded its start is placed at its real offset; one that only recorded a
    duration (or was skipped) follows the previous bar."""
    now = now or time.time()
    t0 = run.get("started")
    bars: list[dict] = []
    cursor = 0.0
    for s in run.get("steps", []):
        dur = s.get("duration_ms")
        if dur is None and s.get("started"):
            dur = int((now - s["started"]) * 1000)      # still running
        dur = max(0, int(dur or 0))
        if s.get("started") and t0:
            offset = max(0.0, (s["started"] - t0) * 1000)
        else:
            offset = cursor
        cursor = max(cursor, offset + dur)
        bars.append({"name": s["name"], "offset_ms": int(offset), "duration_ms": dur, "status": s.get("status", "unknown"),
                     "slowest": False})
    total = max(int(run.get("duration_ms") or 0), int(cursor))
    timed = [b for b in bars if b["status"] not in ("skipped",) and b["duration_ms"] > 0]
    if timed:
        max(timed, key=lambda b: b["duration_ms"])["slowest"] = True
    return {"total_ms": total, "bars": bars}


def timing_bars(stats: dict[str, dict]) -> list[dict]:
    """One entry per step for the timing chart: p50/p95/max plus outcome counts,
    slowest (by p95) first."""
    rows = [{"name": n, "p50_ms": s["p50_ms"], "p95_ms": s["p95_ms"], "max_ms": s["max_ms"], "n": s["n"],
             "failed": s["failed"], "last_status": s["last_status"]} for n, s in stats.items()
            if s["ok"] or s["failed"]]          # a step that was only ever skipped has nothing to time
    return sorted(rows, key=lambda r: r["p95_ms"], reverse=True)


def overview_tiles(summary: dict) -> list[dict]:
    """Headline tiles: pipeline health, release health, integrity, workers."""
    tiles = []
    for kind in ("pipeline", "release"):
        k = summary["by_kind"].get(kind)
        if k is None:
            tiles.append({"title": kind.title(), "value": "no runs", "status": "unknown", "detail": ""})
        else:
            last = k["last"] or {}
            tiles.append({"title": kind.title(), "value": last.get("status", "?"),
                          "status": last.get("status", "unknown"),
                          "detail": f"{k['ok']} ok / {k['failed']} failed of {k['runs']} recent"})
    chain = summary["chain"]
    tiles.append({"title": "Log integrity", "value": "verified" if chain["ok"] else "BROKEN",
                  "status": "ok" if chain["ok"] else "failed", "detail": f"{summary['events']} events"})
    live = [w for w in summary["workers"] if w["state"] not in ("stale",)]
    tiles.append({"title": "ML workers", "value": f"{len(live)} active" if summary["workers"] else "none",
                  "status": "ok" if live else "unknown",
                  "detail": f"{len(summary['workers']) - len(live)} stale" if summary["workers"] else "no worker has reported"})
    return tiles


def load_panel(transport: Any, panel: str, *, selected_run: Optional[str] = None, since: int = 0) -> dict:
    """Everything one panel needs, shaped for display. `transport` is any object
    exposing the service capabilities (LocalTransport / HttpTransport)."""
    if panel == "overview":
        s = transport.summary()
        return {"summary": s, "tiles": overview_tiles(s)}
    if panel == "runs":
        runs = transport.runs(limit=50)["runs"]
        rid = selected_run or (runs[0]["id"] if runs else None)
        run = transport.run(rid) if rid else None
        ex = transport.explain(rid) if rid else None
        return {"runs": runs, "selected": rid, "run": run, "explain": ex["text"] if ex else "",
                "gantt": gantt(run) if run else {"total_ms": 0, "bars": []}}
    if panel == "steps":
        stats = transport.step_stats()["steps"]
        return {"stats": stats, "bars": timing_bars(stats)}
    if panel == "decisions":
        return {"decisions": transport.decisions(limit=100)["decisions"]}
    if panel == "workers":
        return {"workers": transport.workers()["workers"]}
    if panel == "events":
        batch = transport.events(since=since, limit=EVENT_TAIL)
        return {"events": batch["events"], "last_seq": batch["last_seq"]}
    if panel == "integrity":
        return {"chain": transport.chain()}
    raise KeyError(panel)


def event_line(e: dict) -> str:
    import json
    return (f"{e['seq']:>6} {fmt_time(e['ts'])} {e['kind']:<16} {(e['run_id'] or '-'):<22} "
            f"{(e['step'] or '-'):<14} {json.dumps(e['data'], ensure_ascii=False)}")
