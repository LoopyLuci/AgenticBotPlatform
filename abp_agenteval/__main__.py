"""python -m abp_agenteval list | run [--live --provider P --model M] [--task ID ...]
[--out FILE] [--baseline FILE] [--tolerance N] [--keep]

Exit status: 0 = all good, 1 = a task failed or (with --baseline) a regression, 2 = usage error."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import report as rep
from .runner import run_suite
from .scripted import ScriptedTransport
from .suite import seed_suite


def _live_transport(provider: str, model: str):
    if provider == "anthropic":
        from bot.agent_runtime.transports.anthropic import AnthropicTransport

        return AnthropicTransport()
    from bot.agent_runtime.subagents import _resolve_named_backend

    return _resolve_named_backend(provider, model).transport


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="abp_agenteval", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the tasks in the suite")
    run = sub.add_parser("run", help="run the suite")
    run.add_argument("--live", action="store_true", help="use a real model (costs tokens); default is scripted")
    run.add_argument("--provider", default="anthropic", help="live only: 'anthropic' or a provider in config/providers.yaml")
    run.add_argument("--model", default="", help="live only: model id")
    run.add_argument("--task", action="append", help="run only this task id (repeatable)")
    run.add_argument("--out", help="write the JSON report here")
    run.add_argument("--baseline", help="compare with this earlier report; exit 1 on a regression")
    run.add_argument("--tolerance", type=float, default=0.0, help="allowed score drop in points (default 0)")
    run.add_argument("--keep", action="store_true", help="keep each task's workspace for inspection")
    run.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    tasks = seed_suite()
    if args.cmd == "list":
        for t in tasks:
            print(f"{t.id:<22} {t.category:<8} {t.title}")
        return 0

    if args.task:
        unknown = set(args.task) - {t.id for t in tasks}
        if unknown:
            print(f"unknown task(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        tasks = [t for t in tasks if t.id in set(args.task)]
    if args.live:
        if not args.model:
            print("--live needs --model", file=sys.stderr)
            return 2
        make = lambda _t: _live_transport(args.provider, args.model)  # noqa: E731
        mode, model = "live", f"{args.provider}/{args.model}"
    else:
        make = lambda t: ScriptedTransport(t.script)  # noqa: E731
        mode, model = "scripted", "scripted"

    report = run_suite(tasks, make, mode=mode, model=model, keep=args.keep)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else rep.render(report))

    status = 0 if report["passed"] == report["total"] else 1
    if args.baseline:
        cmp = rep.compare(report, rep.load(args.baseline), tolerance=args.tolerance)
        print(f"\nvs baseline: regressed={cmp['regressed']} fixed={cmp['fixed']} new={cmp['new']} "
              f"score_drop={cmp['score_drop']}")
        if not cmp["ok"]:
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
