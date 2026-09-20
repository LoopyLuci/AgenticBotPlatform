"""abp-cicd — command-line view of the pipeline.

    python -m abp_cicd status
    python -m abp_cicd runs --kind release --limit 10
    python -m abp_cicd run <id>            # steps, decisions, notes
    python -m abp_cicd explain <id>        # plain-language account
    python -m abp_cicd steps [name]        # duration statistics (P50/P95)
    python -m abp_cicd decisions | workers | events [--follow] | verify
    python -m abp_cicd export FILE.jsonl   # local store only
    python -m abp_cicd prune DAYS          # local store only
    python -m abp_cicd tui                 # interactive terminal dashboard
    python -m abp_cicd gui                 # desktop dashboard window (ABP_CI-CD_GUI)

`--json` prints exactly what the HTTP API returns. Source: `--source local`
reads the event store directly (no server needed); `--source http` uses
`--url`/`--token` (or ABP_URL / DASHBOARD_TOKEN); the default picks local when the
store exists.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Optional

from . import queries
from .transport import LocalTransport, TransportError, choose


# Which command exposes each service capability. tests/test_cicd_api.py fails if
# a capability is added to the service without a command here.
COMMAND_FOR = {"summary": "status", "runs": "runs", "run": "run", "explain": "explain", "step_stats": "steps",
               "decisions": "decisions", "workers": "workers", "events": "events", "chain": "verify"}


def _t(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def _table(rows: list[list[Any]], headers: list[str]) -> str:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip()  # noqa: E731
    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def render_summary(s: dict) -> str:
    chain = s["chain"]
    out = [f"events: {s['events']}    chain: {'OK' if chain['ok'] else 'BROKEN at seq ' + str(chain['first_bad_seq'])}"]
    rows = [[k, v["runs"], v["ok"], v["failed"], v["running"],
             f"{v['last']['status']} {_t(v['last']['started'])}" if v["last"] else "-"] for k, v in sorted(s["by_kind"].items())]
    out.append(_table(rows, ["kind", "runs", "ok", "failed", "running", "latest"]) if rows else "no runs recorded yet")
    if s["active"]:
        out.append("active: " + ", ".join(f"{r['kind']} {r['id']}" for r in s["active"]))
    if s["workers"]:
        out.append(_table([[w["worker"], w["state"], w["model"] or "-", w["queue"] if w["queue"] is not None else "-",
                            f"{w['age_s']}s ago"] for w in s["workers"]], ["worker", "state", "model", "queue", "seen"]))
    return "\n".join(out)


def render_runs(d: dict) -> str:
    rows = [[r["id"], r["kind"], r["status"], queries._fmt_ms(r["duration_ms"]), r["steps"], r["failed_steps"],
             r["attrs"].get("version") or r["attrs"].get("title") or "", _t(r["started"])] for r in d["runs"]]
    return _table(rows, ["id", "kind", "status", "took", "steps", "failed", "what", "started"]) if rows else "no runs"


def render_run(r: dict) -> str:
    out = [f"{r['kind']} {r['id']}  {r['status']}  {queries._fmt_ms(r['duration_ms'])}  started {_t(r['started'])}"]
    if r["summary"]:
        out.append(f"summary: {r['summary']}")
    out.append(_table([[s["name"], s["status"], queries._fmt_ms(s["duration_ms"]), s["attempts"],
                        (s["error"] or s["skipped_reason"] or s["detail"])[:70]] for s in r["steps"]],
                      ["step", "status", "took", "tries", "note"]) if r["steps"] else "no steps")
    for d in r["decisions"]:
        out.append(f"decision  {d.get('actor')}: {d.get('decision')} — {d.get('reason', '')}")
    for n in r["notes"]:
        out.append(f"note [{n.get('level')}] {n.get('message')}")
    return "\n".join(out)


def render_steps(d: dict) -> str:
    rows = [[n, s["n"], s["ok"], s["failed"], s["skipped"], queries._fmt_ms(s["p50_ms"]), queries._fmt_ms(s["p95_ms"]),
             queries._fmt_ms(s["max_ms"]), s["last_status"]] for n, s in sorted(d["steps"].items())]
    return _table(rows, ["step", "n", "ok", "fail", "skip", "p50", "p95", "max", "last"]) if rows else "no step data"


def render_decisions(d: dict) -> str:
    rows = [[_t(x["ts"]), x.get("run_id") or "", x.get("actor"), x.get("decision"), (x.get("reason") or "")[:60],
             "" if x.get("confidence") is None else f"{x['confidence']:.2f}"] for x in d["decisions"]]
    return _table(rows, ["when", "run", "actor", "decision", "reason", "conf"]) if rows else "no decisions recorded"


def render_workers(d: dict) -> str:
    rows = [[w["worker"], w["state"], w["model"] or "-", w["queue"] if w["queue"] is not None else "-", w["detail"][:50],
             f"{w['age_s']}s"] for w in d["workers"]]
    return _table(rows, ["worker", "state", "model", "queue", "detail", "age"]) if rows else "no workers have reported"


def render_event(e: dict) -> str:
    return f"{e['seq']:>6} {_t(e['ts'])} {e['kind']:<16} {e['run_id'] or '-':<22} {e['step'] or '-':<14} {json.dumps(e['data'], ensure_ascii=False)}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="abp-cicd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["auto", "local", "http"], default="auto")
    p.add_argument("--db", help="event store path (local source)")
    p.add_argument("--url", help="server URL (http source)")
    p.add_argument("--token", help="dashboard token (http source)")
    p.add_argument("--json", action="store_true", help="print the API's JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("runs")
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--kind")
    sub.add_parser("run").add_argument("id")
    sub.add_parser("explain").add_argument("id")
    s = sub.add_parser("steps")
    s.add_argument("name", nargs="?")
    s.add_argument("--kind")
    s.add_argument("--last", type=int, default=50)
    d = sub.add_parser("decisions")
    d.add_argument("--limit", type=int, default=30)
    d.add_argument("--run")
    sub.add_parser("workers")
    e = sub.add_parser("events")
    e.add_argument("--since", type=int, default=0)
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("--kind")
    e.add_argument("--run")
    e.add_argument("--follow", "-f", action="store_true")
    sub.add_parser("verify")
    sub.add_parser("export").add_argument("file")
    sub.add_parser("prune").add_argument("days", type=float)
    sub.add_parser("tui", help="interactive terminal dashboard")
    g = sub.add_parser("gui", help="desktop dashboard window (ABP_CI-CD_GUI)")
    g.add_argument("--tab", help="open on this panel (overview, runs, steps, decisions, workers, events, integrity)")
    return p


def _emit(args: argparse.Namespace, payload: Any, render) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else render(payload))


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.cmd in ("export", "prune"):
            store = LocalTransport(args.db).store        # control operations: the local store only
            if args.cmd == "export":
                print(f"wrote {store.export_jsonl(args.file)} events to {args.file}")
            else:
                print(f"pruned {store.prune(args.days)} events older than {args.days:g} days")
            return 0
        t = choose(args.source, args.db, args.url, args.token)
        if args.cmd == "tui":
            from .tui import run as run_tui
            run_tui(t)
            return 0
        if args.cmd == "gui":
            from .gui import launch
            return launch(t, tab=args.tab)
        if args.cmd == "status":
            _emit(args, t.summary(), render_summary)
        elif args.cmd == "runs":
            _emit(args, t.runs(args.limit, args.kind), render_runs)
        elif args.cmd == "run":
            run = t.run(args.id)
            if run is None:
                print(f"no run {args.id}", file=sys.stderr)
                return 2
            _emit(args, run, render_run)
        elif args.cmd == "explain":
            ex = t.explain(args.id)
            if ex is None:
                print(f"no run {args.id}", file=sys.stderr)
                return 2
            _emit(args, ex, lambda x: x["text"])
        elif args.cmd == "steps":
            _emit(args, t.step_stats(args.name, args.kind, args.last), render_steps)
        elif args.cmd == "decisions":
            _emit(args, t.decisions(args.limit, args.run), render_decisions)
        elif args.cmd == "workers":
            _emit(args, t.workers(), render_workers)
        elif args.cmd == "events":
            since = args.since
            while True:
                batch = t.events(since, args.limit, args.kind, args.run)
                for ev in batch["events"]:
                    print(json.dumps(ev, ensure_ascii=False) if args.json else render_event(ev))
                since = batch["last_seq"]
                if not args.follow:
                    break
                sys.stdout.flush()
                time.sleep(1.0)
        elif args.cmd == "verify":
            res = t.chain()
            _emit(args, res, lambda r: f"chain OK ({r['count']} events)" if r["ok"]
                  else f"chain BROKEN at seq {r['first_bad_seq']}: {r['reason']}")
            return 0 if res["ok"] else 3
        return 0
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
