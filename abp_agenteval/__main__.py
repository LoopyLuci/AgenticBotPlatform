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
    run.add_argument("--record", action="store_true", help="live only: store the pass rate per task category for this model, for the model router")
    page = sub.add_parser("page", help="write a static results page from one or more reports, or from a benchmark folder")
    page.add_argument("reports", nargs="*")
    page.add_argument("--bench", help="a `bench run` folder: the page shows its leaderboard")
    page.add_argument("--out", required=True)
    bench = sub.add_parser("bench", help="the suite, live, across several models and repeated (see abp_agenteval/bench.py)")
    bsub = bench.add_subparsers(dest="bench_cmd", required=True)
    brun = bsub.add_parser("run")
    brun.add_argument("--models", required=True, help="'auto' (the router's free candidates) or provider/model,provider/model")
    brun.add_argument("--repeats", type=int, default=3)
    brun.add_argument("--dir", required=True, help="where run files go (existing runs of this suite are skipped)")
    brun.add_argument("--task", action="append", help="only this task id (repeatable)")
    bsum = bsub.add_parser("summary")
    bsum.add_argument("--dir", required=True)
    bsum.add_argument("--json", action="store_true")
    cmp_ = sub.add_parser("compare", help="run the suite once per variant of prompt / tool wording and compare (see abp_agenteval/compare.py)")
    cmp_.add_argument("--variants", required=True, help="JSON file: a list of {name, config: {native_agent overrides}}")
    cmp_.add_argument("--live", action="store_true")
    cmp_.add_argument("--provider", default="anthropic")
    cmp_.add_argument("--model", default="")
    cmp_.add_argument("--task", action="append")
    cmp_.add_argument("--out", help="write the comparison as JSON here")
    args = ap.parse_args(argv)
    # A report carries whatever the provider said, in whatever language it said it: printing it on a
    # Windows console (still cp1252 by default) would end the run that produced it with
    # UnicodeEncodeError. Same guard as abp_import's CLI.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    if args.cmd == "page":
        from . import page as page_mod

        return page_mod.main(args.reports, args.out, bench_dir=args.bench)
    tasks = seed_suite()
    if args.cmd == "bench":
        from . import bench as bench_mod

        if args.bench_cmd == "summary":
            summary = bench_mod.summarize(bench_mod.load(Path(args.dir)))
            print(json.dumps(summary, indent=1) if args.json else bench_mod.render(summary))
            return 0
        if args.task:
            tasks = [t for t in tasks if t.id in set(args.task)]
        models = bench_mod.resolve_models(args.models)
        if not models:
            print("no models: add a provider on the Models page, or list provider/model names", file=sys.stderr)
            return 2
        if args.repeats < 1:
            print("--repeats must be at least 1", file=sys.stderr)
            return 2
        done = bench_mod.run(models, args.repeats, Path(args.dir), tasks, bench_mod.live_factory, probe=bench_mod.live_probe)
        print(f"ran {done['ran']}, skipped {done['skipped']} already done, {done['incomplete']} cut short by a limit, "
              f"{done['limited']} model(s) out of allowance (tried again next time), {done['unavailable']} not available\n")
        print(bench_mod.render(bench_mod.summarize(bench_mod.load(Path(args.dir)))))
        return 0
    if args.cmd == "compare":
        from . import compare

        return compare.main(args, tasks)
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

    report = run_suite(tasks, make, mode=mode, model=model, keep=args.keep, api_model=args.model if args.live else None)
    if args.out:
        rep.save(args.out, report)
    print(json.dumps(report, indent=2) if args.json else rep.render(report))
    if args.record:
        if not args.live:
            print("--record only stores live runs (a scripted run measures the harness, not a model)", file=sys.stderr)
        else:
            from bot import model_router

            print("recorded:", model_router.record_eval_scores(model, report))

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
