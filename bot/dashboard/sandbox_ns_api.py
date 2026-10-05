"""Sandbox Nervous System API: what ABP is running right now, and the two ways to stop it.

    GET  /api/sandbox/status                    everything: the guard, every record, every cell with its
                                                measured CPU/memory and the limits the OS really has, recent events
    GET  /api/sandbox/cells                     the cells, each with the processes under it
    GET  /api/sandbox/processes                 every process this ABP process started and everything it
                                                started, live ones first
    GET  /api/sandbox/events?since=<epoch>      the event ring buffer, oldest first, only what is newer
    POST /api/sandbox/cells/{cell_id}/kill      stop one cell and everything in it
    POST /api/sandbox/estop                     stop every non-persistent cell (the daemons keep running)

This is the surface docs/sandbox-nervous-system.md describes: a status page that can say *who* is running
*what*, with the measured numbers and the limits the OS actually applied rather than the ones ABP asked
for. The CLI (`abp sandbox ...`) and the dashboard's Processes panel are both just this API.

Everything here needs the desktop dashboard token - a kill is an operator's decision, not a page's, and
the whole table describes the machine's processes. Kills and the emergency stop are written to ABP's
audit log. State lives in bot/sandbox_ns/registry.py, which is on bot/hotreload.py's denylist.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

from bot import db
from bot.sandbox_ns.registry import registry


def _with_processes(status: dict) -> dict:
    """`registry.status()` with the processes of each cell spelled out.

    The registry counts a cell's processes, which is right for the sampler's own bookkeeping and
    useless on a page: a count says nothing about which process is which, who asked for it, or what
    its command line is (already masked by the registry)."""
    by_cell: dict[str, list] = {}
    for row in status.get("processes") or []:
        if row.get("alive"):
            by_cell.setdefault(row.get("cell") or "", []).append(row)
    for cell in status.get("cells") or []:
        cell["processes"] = by_cell.get(cell.get("id") or "", [])
        cell["process_count"] = len(cell["processes"])
        cell["persistent"] = bool((cell.get("policy") or {}).get("persistent"))
    return status


def snapshot() -> dict:
    return _with_processes(registry.status())


def _audit(action: str, detail: str) -> None:
    try:
        db.log_audit(actor="dashboard", action=action, detail=detail[:500])
    except Exception:  # noqa: BLE001 - the audit log must never be the reason a kill does not happen
        pass


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]

    @app.get("/api/sandbox/status", dependencies=dep)
    async def sandbox_status():
        """Everything the nervous system knows: guard, records, cells (with limits and samples), events."""
        return await asyncio.to_thread(snapshot)

    @app.get("/api/sandbox/cells", dependencies=dep)
    async def sandbox_cells():
        """The cells this process holds, each with the processes under it and its measured CPU/memory."""
        return {"cells": (await asyncio.to_thread(snapshot))["cells"]}

    @app.get("/api/sandbox/processes", dependencies=dep)
    async def sandbox_processes(alive_only: bool = Query(default=True, description="only the processes that are still running")):
        """Every process ABP started and wrote down: the live ones first, then the newest of those left."""
        status = await asyncio.to_thread(snapshot)
        rows = status["processes"]
        if alive_only:
            rows = [r for r in rows if r.get("alive")]
        rows = sorted(rows, key=lambda r: (not bool(r.get("alive")), -(float(r.get("started") or 0.0))))
        return {"processes": rows, "run_id": status["run_id"]}

    @app.get("/api/sandbox/events", dependencies=dep)
    async def sandbox_events(since: Optional[float] = Query(default=None, description="only events newer than this epoch seconds"),
                             limit: int = Query(default=200, ge=1, le=500)):
        """The event ring buffer (spawn, descendant, exit, limit_hit, kill, reap, guard_converted),
        oldest first. `descendant` is a process a recorded one started - the interpreter behind a
        venv launcher, a build's compiler - recorded under its parent.

        `since` is what a poller passes back: the newest `ts` it has seen, so the answer is only what
        it has not seen. `now` comes back with it for the next call."""
        rows = await asyncio.to_thread(registry.events, limit, None)
        rows = sorted(rows, key=lambda e: e.get("ts") or 0)
        if since is not None:
            rows = [e for e in rows if float(e.get("ts") or 0) > float(since)]
        return {"events": rows[:limit], "now": time.time()}

    @app.post("/api/sandbox/cells/{cell_id}/kill", dependencies=dep)
    async def sandbox_kill_cell(cell_id: str):
        """Stop one cell: every process in it, and every process those started."""
        cell = next((c for c in registry.cells() if c.id == cell_id), None)
        if cell is None:
            raise HTTPException(status_code=404, detail=f"no cell {cell_id!r} in this ABP process")
        await asyncio.to_thread(cell.kill, f"killed from the dashboard ({cell.name})")
        _audit("sandbox_kill_cell", f"{cell_id} {cell.name} owner={cell.owner}")
        return {"killed": True, "cell": cell_id, "name": cell.name, "owner": cell.owner}

    @app.post("/api/sandbox/estop", dependencies=dep)
    async def sandbox_estop(body: Optional[dict] = None):
        """The emergency stop: every non-persistent cell dies. Daemons are somebody's running service
        and are left alone - still running, and still on this page."""
        reason = str((body or {}).get("reason") or "the emergency stop, from the dashboard")
        killed = await asyncio.to_thread(registry.estop, reason=reason)
        _audit("sandbox_estop", f"{len(killed)} cell(s): {', '.join(killed)[:300]}")
        return {"killed": killed, "count": len(killed), "reason": reason}