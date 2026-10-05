"""Sandbox Nervous System CLI: every process ABP started and is still running, its cell, what that
cell may do, and the two ways to stop it (one cell, or every non-persistent cell).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from bot.dashboard_client import ApiError, DashboardClient


def _print(args, data: Any, *, table: Optional[list[str]] = None) -> None:
    if args.json:
        print(json.dumps(data, indent=1, ensure_ascii=False))
        return
    if table and isinstance(data, list):
        rows = [[str(row.get(col, "")) for col in table] for row in data]
        widths = [max(len(col), *(len(r[i]) for r in rows)) if rows else len(col) for i, col in enumerate(table)]
        print("  ".join(col.ljust(w) for col, w in zip(table, widths)))
        for r in rows:
            print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
        return
    print(json.dumps(data, indent=1, ensure_ascii=False) if isinstance(data, (dict, list)) else data)


async def _status(args, client: DashboardClient) -> int:
    data = await client.sandbox_status()
    _print(args, data)
    return 0


async def _cells(args, client: DashboardClient) -> int:
    cells = await client.sandbox_cells()
    if args.json:
        _print(args, {"cells": cells})
        return 0
    rows = []
    for c in cells:
        pol = c.get("policy") or {}
        rows.append({
            "id": c.get("id", "")[:40],
            "name": c.get("name", "")[:30],
            "owner": c.get("owner", ""),
            "preset": pol.get("name", ""),
            "persistent": "yes" if pol.get("persistent") else "no",
            "procs": c.get("process_count", 0),
            "cpu": f"{(c.get('sample') or {}).get('cpu_percent', 0):.1f}%",
            "mem": f"{(c.get('sample') or {}).get('rss_mb', 0):.1f}MB",
        })
    _print(args, rows, table=["id", "name", "owner", "preset", "persistent", "procs", "cpu", "mem"])
    return 0


async def _ps(args, client: DashboardClient) -> int:
    procs = await client.sandbox_processes(alive_only=not args.all)
    if args.json:
        _print(args, {"processes": procs, "run_id": (await client.sandbox_status()).get("run_id")})
        return 0
    rows = []
    for p in procs:
        rows.append({
            "pid": p.get("pid", 0),
            "of": p.get("parent_pid") or "",
            "cell": p.get("cell", "")[:30],
            "owner": p.get("owner", ""),
            "alive": "yes" if p.get("alive") else "no",
            "argv": " ".join(p.get("argv", []))[:60],
        })
    _print(args, rows, table=["pid", "of", "cell", "owner", "alive", "argv"])
    return 0


async def _events(args, client: DashboardClient) -> int:
    data = await client.sandbox_events(since=args.since, limit=args.limit)
    _print(args, data)
    return 0


async def _kill(args, client: DashboardClient) -> int:
    result = await client.sandbox_kill_cell(args.cell_id)
    _print(args, result)
    return 0


async def _estop(args, client: DashboardClient) -> int:
    result = await client.sandbox_estop(args.reason or "emergency stop from CLI")
    _print(args, result)
    return 0


def add_parser(sub) -> None:
    sb = sub.add_parser("sandbox", help="Sandbox Nervous System: cells, processes, events, kill, estop")
    ssub = sb.add_subparsers(dest="sandbox_cmd", required=True)

    p = ssub.add_parser("status", help="everything the nervous system knows (guard, records, cells, events)")
    p.set_defaults(func=_status)

    p = ssub.add_parser("cells", help="the cells this ABP holds, with their processes and measured CPU/memory")
    p.set_defaults(func=_cells)

    p = ssub.add_parser("ps", help="every process ABP started and wrote down, `of` being what started it")
    p.add_argument("--all", action="store_true", help="include exited processes")
    p.set_defaults(func=_ps)

    p = ssub.add_parser("events", help="the event ring buffer (spawn, descendant, exit, limit_hit, kill, reap, guard_converted)")
    p.add_argument("--since", type=float, default=None, help="only events newer than this epoch seconds")
    p.add_argument("--limit", type=int, default=200, help="max events to return")
    p.set_defaults(func=_events)

    p = ssub.add_parser("kill", help="stop one cell and everything in it")
    p.add_argument("cell_id")
    p.set_defaults(func=_kill)

    p = ssub.add_parser("estop", help="stop every non-persistent cell (daemons keep running)")
    p.add_argument("--reason", default=None, help="reason string for the audit log")
    p.set_defaults(func=_estop)


async def run(args, client: DashboardClient) -> int:
    try:
        return await args.func(args, client)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"sandbox command failed: {exc}", file=sys.stderr)
        return 1