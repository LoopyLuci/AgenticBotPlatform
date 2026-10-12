"""Agent traces API (roadmap P0): the Traces screen's read-only routes.

    GET /api/agent/traces                 runs, newest first. Filters: instance_id, status (ok / failed / cancelled / running),
                                          q (text), since / until (epoch seconds of the run's start); paging: limit, offset
    GET /api/agent/traces/{run_id}        one run: totals, its events in order with the time since the run began,
                                          the parent run and the sub-agent runs it started

Everything comes from the tamper-evident trace store (bot/agent_runtime/trace.py), which records the shape of
a run (tool, status, durations, a short redacted target) and never prompts, replies or tool output. These routes
add nothing to that and change nothing.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SCAN_CAP = 5000          # newest runs looked at per request; older ones stay in the store, reachable with since/until


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _ends(store, run_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(run_ids), 400):
        chunk = run_ids[i:i + 400]
        rows = store.select(f"SELECT run_id, ts, data FROM events WHERE kind = 'agent.run.end' AND run_id IN ({','.join('?' * len(chunk))})", chunk)
        for r in rows:
            out[r["run_id"]] = {"ts": r["ts"], **json.loads(r["data"])}
    return out


def _counts(store, run_ids: list[str]) -> dict[str, dict]:
    out = {rid: {"llm_calls": 0, "tool_calls": 0, "failed_tools": 0, "denied_tools": 0} for rid in run_ids}
    for i in range(0, len(run_ids), 400):
        chunk = run_ids[i:i + 400]
        rows = store.select(
            "SELECT run_id, kind, data FROM events WHERE kind IN ('llm.call','tool.call') AND run_id IN "
            f"({','.join('?' * len(chunk))})", chunk)
        for r in rows:
            c = out[r["run_id"]]
            if r["kind"] == "llm.call":
                c["llm_calls"] += 1
            else:
                c["tool_calls"] += 1
                status = json.loads(r["data"]).get("status")
                c["failed_tools"] += status == "failed"
                c["denied_tools"] += status == "denied"
    return out


def _run_status(end: Optional[dict]) -> str:
    return str((end or {}).get("status") or "running")


def _summary(start_ev: dict, end: Optional[dict]) -> dict:
    d = start_ev["data"]
    return {"run_id": start_ev["run_id"], "started": start_ev["ts"], "status": _run_status(end),
            "agent": d.get("agent"), "model": d.get("model"), "transport": d.get("transport"),
            "instance_id": d.get("instance_id"), "session": d.get("session"), "source": d.get("source"),
            "parent_run": d.get("parent_run"), "effort": d.get("effort"), "tools_available": d.get("tools"),
            "duration_ms": (end or {}).get("duration_ms"), "iterations": (end or {}).get("iterations"),
            "tokens": (end or {}).get("tokens"), "usd": (end or {}).get("usd"), "error": (end or {}).get("error") or None}


def register(app: FastAPI, read_auth: Callable) -> None:
    from bot.agent_runtime import trace

    read = [Depends(read_auth)]

    def _store():
        return trace.get_store()

    def _starts(store, since: Optional[float], until: Optional[float], where: str = "", params: tuple = ()) -> list[dict]:
        sql = "SELECT * FROM events WHERE kind = 'agent.run.start'"
        args: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            args.append(until)
        sql += where + " ORDER BY ts DESC, seq DESC LIMIT ?"
        return [store._row(r) for r in store.select(sql, (*args, *params, _SCAN_CAP))]

    @app.get("/api/agent/traces", dependencies=read)
    async def list_traces(instance_id: Optional[int] = None, status: str = Query("", max_length=40),
                          q: str = Query("", max_length=200), since: Optional[float] = None,
                          until: Optional[float] = None, limit: int = Query(25, ge=1, le=100),
                          offset: int = Query(0, ge=0)):
        """Runs newest first with their outcome and totals. `status` matches the recorded end status, or
        `running` for a run with no end yet. `q` matches the run id, agent, model, session, tool names, targets and errors."""
        store = _store()
        starts = _starts(store, since, until)
        text = q.strip().lower()
        if text:
            hit = {r["run_id"] for r in store.select(
                "SELECT DISTINCT run_id FROM events WHERE LOWER(data) LIKE ? ESCAPE '\\'", (_like(text),))}
            starts = [s for s in starts if s["run_id"] in hit or text in str(s["run_id"]).lower()]
        if instance_id is not None:
            starts = [s for s in starts if s["data"].get("instance_id") == instance_id]
        ends = _ends(store, [s["run_id"] for s in starts])
        want = status.strip().lower()
        if want:
            starts = [s for s in starts if _run_status(ends.get(s["run_id"])).lower() == want]
        total = len(starts)
        page = starts[offset:offset + limit]
        counts = _counts(store, [s["run_id"] for s in page])
        statuses = sorted({_run_status(ends.get(s["run_id"])) for s in starts})
        runs = [{**_summary(s, ends.get(s["run_id"])), **counts[s["run_id"]]} for s in page]
        return {"runs": runs, "total": total, "limit": limit, "offset": offset, "statuses": statuses,
                "capped": len(starts) >= _SCAN_CAP}

    @app.get("/api/agent/traces/{run_id}", dependencies=read)
    async def trace_detail(run_id: str):
        """One run in order. Each event is the stored (already redacted) record plus `offset_ms` since the run began."""
        if not _RUN_ID.match(run_id):
            raise HTTPException(400, "bad run id")
        store = _store()
        evs = trace.run_events(run_id, store)
        start_ev = next((e for e in evs if e["kind"] == "agent.run.start"), None)
        if not start_ev:
            raise HTTPException(404, "no such run")
        end = next((dict(e["data"], ts=e["ts"]) for e in evs if e["kind"] == "agent.run.end"), None)
        t0 = start_ev["ts"]
        timeline = [{"seq": e["seq"], "ts": e["ts"], "offset_ms": int(max(0.0, e["ts"] - t0) * 1000), "kind": e["kind"],
                     "data": e["data"]} for e in evs]
        llm = [e["data"] for e in evs if e["kind"] == "llm.call"]
        tools = [e["data"] for e in evs if e["kind"] == "tool.call"]
        totals = {
            "llm_calls": len(llm), "tool_calls": len(tools),
            "tokens": sum(int(d.get("tokens") or 0) for d in llm),
            "usd": (round(sum(float(d["usd"]) for d in llm if d.get("usd") is not None), 6)
                    if any(d.get("usd") is not None for d in llm) else None),
            "cache_read": sum(int(d.get("cache_read") or 0) for d in llm),
            "cache_create": sum(int(d.get("cache_create") or 0) for d in llm),
            "llm_ms": sum(int(d.get("duration_ms") or 0) for d in llm),
            "tool_ms": sum(int(d.get("duration_ms") or 0) for d in tools),
            "failed_tools": sum(1 for d in tools if d.get("status") == "failed"),
            "denied_tools": sum(1 for d in tools if d.get("status") == "denied"),
            "approvals": sum(1 for e in evs if e["kind"] == "approval"),
        }
        summary = _summary(start_ev, end)
        parent = None
        if summary["parent_run"]:
            p_evs = [e for e in trace.run_events(summary["parent_run"], store) if e["kind"] in ("agent.run.start", "agent.run.end")]
            p_start = next((e for e in p_evs if e["kind"] == "agent.run.start"), None)
            if p_start:
                p_end = next((e["data"] for e in p_evs if e["kind"] == "agent.run.end"), None)
                parent = _summary(p_start, p_end)
        kids = _starts(store, None, None, " AND data LIKE ?", (f'%"parent_run":"{run_id}"%',))
        kids = [k for k in kids if k["data"].get("parent_run") == run_id]
        kids.reverse()
        k_ends = _ends(store, [k["run_id"] for k in kids])
        return {"run": summary, "totals": totals, "events": timeline, "parent": parent,
                "children": [_summary(k, k_ends.get(k["run_id"])) for k in kids]}
