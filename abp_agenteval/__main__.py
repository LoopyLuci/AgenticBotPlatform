"""python -m abp_agenteval list | run [--live --provider P --model M] [--task ID ...]
[--out FILE] [--baseline FILE] [--tolerance N] [--keep]

Exit status: 0 = all good, 1 = a task failed or (with --baseline) a regression, 2 = usage error."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from . import report as rep
from .runner import run_suite
from .scripted import ScriptedTransport
from .suite import seed_suite


def _live_transport(provider: str, model: str, base_url: str = "", api_key_env: str = ""):
    if base_url:
        # Any OpenAI-compatible endpoint (a ModelMistress instance, llama.cpp, vLLM ...): no providers.yaml entry needed.
        from .factory import endpoint_transport

        return endpoint_transport(base_url, os.environ.get(api_key_env, "") if api_key_env else "")
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
    run.add_argument("--base-url", default="", help="live only: an OpenAI-compatible endpoint (http://host:port/v1) instead of a provider")
    run.add_argument("--api-key-env", default="", help="live with --base-url: the environment variable holding the endpoint's key (none by default)")
    run.add_argument("--context", type=int, default=0, help="live: tell the agent this model's context window in tokens (default: ABP's guess)")
    run.add_argument("--small-model", choices=("on", "off", "auto"), default="on",
                     help="live with --base-url: small-model mode on (default; such an endpoint is not detected as local), off, or auto")
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
    page.add_argument("--reports", dest="reports_dir", help="a folder of run reports and compare reports (docs/agents/eval-reports): a summary per run and a table across runs")
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
    fac = sub.add_parser("factory", help="the factory bench: real solved tasks from this repo's history, replayed in throwaway worktrees (see abp_agenteval/factory.py)")
    fsub = fac.add_subparsers(dest="factory_cmd", required=True)
    for name, text in (("list", "list the tasks"), ("verify", "check each task: its judge fails on the base commit and passes with the real change"),
                       ("run", "run the agent on every task against an OpenAI-compatible endpoint")):
        f = fsub.add_parser(name, help=text)
        f.add_argument("--task", action="append", help="only this task id (repeatable)")
        f.add_argument("--tasks", help="a task file (default abp_agenteval/factory_tasks.json)")
        f.add_argument("--repo", help="the git repository to replay (default: this one)")
        f.add_argument("--workdir", help="where the throwaway worktrees go (default: the system temp folder)")
        f.add_argument("--python", default=sys.executable, help="the Python that judges (needs pytest and ruff)")
        if name == "run":
            f.add_argument("--base-url", required=True, help="OpenAI-compatible endpoint, e.g. http://127.0.0.1:8080/v1")
            f.add_argument("--api-key-env", default="", help="environment variable holding the endpoint's key (none by default)")
            f.add_argument("--model", default="", help="the model id sent to the endpoint")
            f.add_argument("--label", default="", help="the name the report carries for the model")
            f.add_argument("--context", type=int, default=0, help="tell the agent the model's context window in tokens")
            f.add_argument("--timeout", type=float, default=0, help="seconds per task (default: each task's own, 900)")
            f.add_argument("--shell", choices=("none", "sandbox"), default="none",
                           help="none: the agent has no shell tool; sandbox: run_shell inside the windows_job sandbox without network")
            f.add_argument("--sandbox-path", action="append", help="a folder the sandboxed shell may read (the Python install and venv)")
            f.add_argument("--small-model", choices=("on", "off", "auto"), default="on",
                           help="small-model mode (core tools, compact prompt): on (default; a --base-url endpoint is not detected as local, so auto would "
                                "leave it off), off, or auto")
            f.add_argument("--knowledge", choices=("off", "km"), default="off",
                           help="km: add the project knowledge module (mmkm/1, built from each task's base commit) to the prompt (LF7)")
            f.add_argument("--fix-rounds", type=int, default=2,
                           help="after the agent's turn, check the change (edits landed, compiles, ruff, visible tests) and give it "
                                "back the problems for up to this many more turns (0 = off)")
            f.add_argument("--script-tool", choices=("on", "off"), default="on",
                           help="on: a task that needs a command gets run_script (the repo's generator scripts only, in the "
                                "windows_job sandbox without network); off: such a task is skipped without --shell sandbox")
            f.add_argument("--out", help="write the JSON report here")
            f.add_argument("--keep", action="store_true", help="keep each worktree for inspection (you remove them)")
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

        return page_mod.main(args.reports, args.out, bench_dir=args.bench, reports_dir=args.reports_dir)
    if args.cmd == "factory":
        from . import factory

        return factory.main(args, args.python)
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
    scope = contextlib.nullcontext()
    if args.live:
        if not args.model:
            print("--live needs --model", file=sys.stderr)
            return 2
        make = lambda _t: _live_transport(args.provider, args.model, args.base_url, args.api_key_env)  # noqa: E731
        mode, model = "live", f"{args.provider}/{args.model}" if not args.base_url else f"endpoint/{args.model}"
        if args.context or args.base_url:
            from .factory import agent_environment, small_model_overlay

            overlay = {"context_windows": {args.model: args.context}} if args.context else {}
            if args.base_url:
                overlay.update(small_model_overlay(args.small_model))
            scope = agent_environment(overlay)
    else:
        make = lambda t: ScriptedTransport(t.script)  # noqa: E731
        mode, model = "scripted", "scripted"

    with scope:
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
