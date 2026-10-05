"""Routines CLI: the saved, parameterised tasks the agent writes, listed, run and re-timed from here.

`abp_cli routine ...` is just /api/routines, so a headless run can be triggered and a broken
schedule fixed without a GUI - the same reason `approvals` exists.
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


def _values(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        key, sep, val = pair.partition("=")
        if not sep or not key:
            print(f"expected param=value, got {pair!r}", file=sys.stderr)
            raise SystemExit(2)
        out[key] = val
    return out


def _rows(routines: list[dict]) -> list[dict]:
    return [{
        "id": r.get("id", ""),
        "name": r.get("name", ""),
        "instance": r.get("instance_id", ""),
        "params": ",".join(r.get("params") or {}),
        "schedule": "paused" if r.get("paused") else (f"every {r['interval_s']}s" if r.get("interval_s") else "not scheduled"),
        "last_run": (r.get("last_run") or {}).get("outcome", "never"),
        "next_run": r.get("next_run_at") or "-",
    } for r in routines]


async def _find(client: DashboardClient, ref: str) -> dict:
    """Accept a routine id or its name, so `routine run pr-digest` reads the way it does in chat."""
    rows = await client.list_routines()
    if ref.isdigit():
        try:
            return await client.get_routine(int(ref))
        except ApiError:
            pass
    for r in rows:
        if r.get("name") == ref.strip().lower():
            return await client.get_routine(r["id"])
    raise ApiError(404, f"no routine named {ref!r}; `abp_cli routine list` shows them all")


async def _list(args, client: DashboardClient) -> int:
    rows = await client.list_routines(instance_id=args.instance)
    if args.json:
        _print(args, rows)
        return 0
    if not rows:
        print("No routines yet. Do a task with the agent, then ask it to save that as a routine.")
        return 0
    _print(args, _rows(rows), table=["id", "name", "instance", "params", "schedule", "last_run", "next_run"])
    return 0


async def _show(args, client: DashboardClient) -> int:
    routine = await _find(client, args.name)
    if not args.json:
        print(f"{routine['name']} (id {routine['id']}, bot {routine['instance_id']}): {routine['description'] or '(no description)'}")
        print(f"\nTemplate:\n{routine['template']}")
        print("\nParameters:")
        for k, v in (routine.get("params") or {}).items():
            print(f"  {k}: {v.get('description', '')}" + (f" (default {v['default']})" if "default" in v else ""))
        print("\nSchedules: " + (", ".join(
            f"#{s['id']} every {s['interval_s']}s {'on' if s['enabled'] else 'paused'}, next {s['next_run_at']}"
            for s in routine.get("schedules") or []) or "not scheduled"))
        print("\nHistory:")
        for h in routine.get("history") or []:
            print(f"  {h['started_at']:.0f} {h['outcome']} {h['summary'][:100]}")
        if not (routine.get("history") or []):
            print("  never run")
        return 0
    _print(args, routine)
    return 0


async def _run(args, client: DashboardClient) -> int:
    routine = await _find(client, args.name)
    result = await client.run_routine(routine["id"], _values(args.param), chat_id=args.chat_id)
    _print(args, result)
    return 0


async def _pause(args, client: DashboardClient) -> int:
    routine = await _find(client, args.name)
    _print(args, await client.pause_routine(routine["id"]))
    return 0


async def _resume(args, client: DashboardClient) -> int:
    routine = await _find(client, args.name)
    _print(args, await client.resume_routine(routine["id"]))
    return 0


async def _schedule(args, client: DashboardClient) -> int:
    routine = await _find(client, args.name)
    result = await client.set_routine_schedule(routine["id"], args.interval, _values(args.param), chat_id=args.chat_id)
    _print(args, result)
    return 0


async def _delete(args, client: DashboardClient) -> int:
    routine = await _find(client, args.name)
    _print(args, await client.delete_routine(routine["id"]))
    return 0


def add_parser(sub) -> None:
    rb = sub.add_parser("routine", help="routines: list, show, run, pause, resume, schedule, delete")
    rsub = rb.add_subparsers(dest="routine_cmd", required=True)

    p = rsub.add_parser("list", help="every routine, with its parameters, schedule and last run")
    p.add_argument("--instance", type=int, default=None, help="only this bot instance's routines")
    p.set_defaults(func=_list)

    p = rsub.add_parser("show", help="one routine (id or name): its template, parameters, schedules and history")
    p.add_argument("name")
    p.set_defaults(func=_show)

    p = rsub.add_parser("run", help="run a routine now, with param=value for its parameters")
    p.add_argument("name")
    p.add_argument("param", nargs="*", default=[], metavar="param=value")
    p.add_argument("--chat", dest="chat_id", default=None, help="deliver the result to this chat (default: the routine's own)")
    p.set_defaults(func=_run)

    p = rsub.add_parser("pause", help="stop every schedule of a routine")
    p.add_argument("name")
    p.set_defaults(func=_pause)

    p = rsub.add_parser("resume", help="start a routine's schedules again")
    p.add_argument("name")
    p.set_defaults(func=_resume)

    p = rsub.add_parser("schedule", help="change how often a routine runs (30m, 2h, 7d, or seconds)")
    p.add_argument("name")
    p.add_argument("interval")
    p.add_argument("param", nargs="*", default=[], metavar="param=value")
    p.add_argument("--chat", dest="chat_id", default=None, help="chat to deliver to (needed the first time)")
    p.set_defaults(func=_schedule)

    p = rsub.add_parser("delete", help="delete a routine, its schedules and its history")
    p.add_argument("name")
    p.set_defaults(func=_delete)


async def run(args, client: DashboardClient) -> int:
    try:
        return await args.func(args, client)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"routine command failed: {exc}", file=sys.stderr)
        return 1
