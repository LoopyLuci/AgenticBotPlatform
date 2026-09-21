"""Compare wordings by running the suite under each (roadmap P8).

    python -m abp_agenteval compare --variants variants.json --live --provider openrouter --model qwen/qwen3.8-27b:free

`variants.json` is a list of named settings overlays for `native_agent`; the first is the baseline:

    [{"name": "baseline", "config": {}},
     {"name": "terse-edit-tool", "config": {"tool_descriptions": {"edit_file": "Replace text in a file. Read it first."}}},
     {"name": "extra-guidance", "config": {"prompt": {"extra": "Check your work by running the tests before you finish."}}}]

The suite runs once per variant with that overlay applied to every task, and the output shows, per variant, how many tasks
passed, the tokens and steps used, and which tasks changed against the baseline. It is how wording gets chosen by evidence
instead of taste - and it is only evidence **with a live model**: a scripted run replays fixed trajectories, so every variant
scores the same and the command says so. One run per variant is also a small sample: models are not deterministic, so a
difference of a task or two is noise, and the report marks such differences as "within noise" rather than as wins.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from .runner import _merge, run_suite
from .scripted import ScriptedTransport

NOISE_TASKS = 2      # a change of this many tasks or fewer is treated as noise


def load_variants(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data or not all(isinstance(v, dict) and v.get("name") for v in data):
        raise ValueError("variants must be a non-empty list of objects with a name")
    names = [v["name"] for v in data]
    if len(set(names)) != len(names):
        raise ValueError("variant names must be unique")
    return data


def apply_variant(tasks: list, variant: dict) -> list:
    """Copies of `tasks` with the variant's overlay merged into each task's own settings (the task's win on a clash)."""
    out = []
    for t in tasks:
        c = copy.copy(t)
        c.config = _merge(variant.get("config") or {}, t.config or {})
        out.append(c)
    return out


def compare(tasks: list, variants: list[dict], make, *, mode: str, model: str) -> dict:
    reports = [(v, run_suite(apply_variant(tasks, v), make, mode=mode, model=model)) for v in variants]
    base_v, base = reports[0]
    base_pass = {r["id"]: r["passed"] for r in base["results"]}
    rows = []
    for v, rep in reports:
        passes = {r["id"]: r["passed"] for r in rep["results"]}
        gained = sorted(i for i, ok in passes.items() if ok and not base_pass.get(i))
        lost = sorted(i for i, ok in passes.items() if not ok and base_pass.get(i))
        delta = rep["passed"] - base["passed"]
        verdict = "baseline" if v is base_v else ("within noise" if abs(delta) <= NOISE_TASKS and mode == "live" else
                                                  "no information (scripted)" if mode != "live" else ("better" if delta > 0 else "worse"))
        rows.append({"name": v["name"], "passed": rep["passed"], "total": rep["total"], "score": rep["score"], "tokens": rep["tokens"],
                     "iterations": sum(r["iterations"] for r in rep["results"]), "gained": gained, "lost": lost, "verdict": verdict})
    return {"mode": mode, "model": model, "variants": rows,
            "note": None if mode == "live" else "scripted runs replay fixed trajectories, so variants cannot differ; use --live"}


def render(result: dict) -> str:
    lines = [f"compare - {result['mode']} - {result['model']}", "",
             f"{'variant':<24} {'passed':>9} {'tokens':>9} {'steps':>7}  verdict"]
    for r in result["variants"]:
        lines.append(f"{r['name']:<24} {r['passed']:>4}/{r['total']:<4} {r['tokens']:>9} {r['iterations']:>7}  {r['verdict']}"
                     + (f"  (+{', '.join(r['gained'])})" if r["gained"] else "") + (f"  (-{', '.join(r['lost'])})" if r["lost"] else ""))
    if result["note"]:
        lines += ["", result["note"]]
    return "\n".join(lines)


def main(args, tasks: list) -> int:
    from .__main__ import _live_transport

    try:
        variants = load_variants(args.variants)
    except (OSError, ValueError) as exc:
        print(f"variants: {exc}", file=sys.stderr)
        return 2
    if args.task:
        tasks = [t for t in tasks if t.id in set(args.task)]
    if args.live:
        if not args.model:
            print("--live needs --model", file=sys.stderr)
            return 2
        make, mode, model = (lambda _t: _live_transport(args.provider, args.model)), "live", f"{args.provider}/{args.model}"
    else:
        make, mode, model = (lambda t: ScriptedTransport(t.script)), "scripted", "scripted"
    result = compare(tasks, variants, make, mode=mode, model=model)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(render(result))
    return 0
