"""A benchmark: the eval suite, live, across several models, repeated, with results anyone can check (roadmap P9).

    python -m abp_agenteval bench run --models auto --repeats 3 --dir docs/benchmarks/runs
    python -m abp_agenteval bench run --models openrouter/qwen/qwen3-coder:free,ollama/qwen3:4b --repeats 3 --dir ...
    python -m abp_agenteval bench summary --dir docs/benchmarks/runs            # the leaderboard, as text or --json
    python -m abp_agenteval page --bench docs/benchmarks/runs --out docs/benchmarks/index.html

**What makes a result worth publishing**, and what this does about each:

* *One run is noise.* A model that passes a task once may fail it the next time. Each model is run `--repeats` times
  (3 by default). The leaderboard shows the mean score, the lowest and highest, the tasks passed in every run
  ("reliable") and the tasks passed in at least one.
* *A score must say what produced it.* Every run file records the ABP version, the git commit, the date, the model, and
  a fingerprint of the suite: its tasks, prompts, fixtures and graders. Runs of a different suite are never averaged
  together; the leaderboard uses the newest fingerprint and says how many older runs it left out.
* *Long runs break.* Each finished run is written as it completes, and `bench run` skips runs that already exist for this
  suite, so it can be stopped and started again. When a model's rate limit or free allowance runs out, that run is
  marked incomplete (it never counts) and the benchmark moves on to the next model.
* *Honesty about scope.* `--models auto` uses the model router's candidates: free models from configured providers, never
  Claude unless listed there. Nothing here runs Claude Code, OpenCode, Hermes or OpenClaw, so nothing compares ABP with
  them. The suite measures ABP's agent with each model, on ABP's own tasks.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .runner import LIMIT_PATTERN

LIMIT_PATTERNS = LIMIT_PATTERN          # one pattern, two jobs: this module judges a whole run by it
INCOMPLETE_SHARE = 0.5          # a run where at least half the tasks hit a limit measures the limit, not the model


def fingerprint(tasks: list) -> str:
    """A short hash of everything that decides a score: task ids, prompts, fixtures, settings and grader code."""
    h = hashlib.sha256()
    for t in sorted(tasks, key=lambda t: t.id):
        grader_src = []
        for g in t.graders:
            code = getattr(g, "__code__", None)
            closure = [c.cell_contents for c in (getattr(g, "__closure__", None) or ()) if _plain(c.cell_contents)]
            grader_src.append(f"{getattr(g, '__qualname__', repr(g))}:{code.co_code.hex() if code else ''}:{closure!r}")
        h.update(json.dumps({"id": t.id, "prompt": t.prompt, "files": t.files, "outside": t.outside_files, "config": t.config,
                             "approvals": t.approvals, "mode": t.permission_mode, "max": t.max_iterations,
                             "graders": grader_src}, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def _plain(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, tuple, list, dict, type(None)))


def provenance() -> dict:
    from bot import __version__

    commit = ""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10,
                           cwd=Path(__file__).resolve().parent.parent)
        commit = r.stdout.strip() if r.returncode == 0 else ""
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=10,
                               cwd=Path(__file__).resolve().parent.parent).stdout.strip()
        if commit and dirty:
            commit += "+changes"
    except (OSError, subprocess.SubprocessError):
        pass
    return {"abp_version": __version__, "commit": commit, "python": sys.version.split()[0]}


def slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_")[:120]


def limited(report: dict) -> bool:
    hits = sum(1 for r in report["results"] if r.get("error") and LIMIT_PATTERNS.search(r["error"]))
    return bool(report["results"]) and hits / len(report["results"]) >= INCOMPLETE_SHARE


def finished(path: Path) -> bool:
    """A run on disk that counts. A run cut short by a limit is redone on the next `bench run`, so a long benchmark
    really does resume (it used to be skipped for good once written)."""
    try:
        return path.is_file() and not json.loads(path.read_text(encoding="utf-8")).get("incomplete")
    except (OSError, ValueError):
        return False


def run(models: list[str], repeats: int, directory: Path, tasks: list, make_transport: Callable[[str], tuple],
        *, log: Callable[[str], None] = print, probe: Callable[[Callable, str], str] | None = None) -> dict:
    """Run every model `repeats` times, skipping runs already finished for this suite. `make_transport(model)` returns
    (the per-task transport factory, the model id to send to the provider). `probe(factory, api_model)`, if given, is
    asked once per model before its first run: "" when it answers, "limited: ..." when its allowance is out right now
    (tried again next time), or why it cannot be used (a model that does not exist is left out, not scored 0)."""
    from .runner import run_suite

    directory.mkdir(parents=True, exist_ok=True)
    fp = fingerprint(tasks)
    meta = provenance()
    done = {"ran": 0, "skipped": 0, "incomplete": 0, "unavailable": 0, "limited": 0}
    for model in models:
        factory, api_model = None, None
        for k in range(1, repeats + 1):
            path = directory / f"{slug(model)}__{fp}__r{k}.json"
            if finished(path):
                done["skipped"] += 1
                continue
            if factory is None:
                try:
                    factory, api_model = make_transport(model)
                except Exception as exc:  # noqa: BLE001 — one model that cannot start must not stop the others
                    log(f"{model}: could not start ({exc}); skipped")
                    done["unavailable"] += 1
                    break
                why = probe(factory, api_model) if probe else ""
                if why.startswith("limited"):
                    log(f"{model}: {why}; nothing run, it will be tried again next time")
                    done["limited"] += 1
                    break
                if why:
                    log(f"{model}: not available ({why}); left out")
                    done["unavailable"] += 1
                    break
            log(f"{model}: run {k} of {repeats} ...")
            started = time.time()
            report = run_suite(tasks, factory, mode="live", model=model, api_model=api_model)
            report.update({"bench": {"fingerprint": fp, "repeat": k, "started": started, **meta},
                           "incomplete": limited(report)})
            path.write_text(json.dumps(report, indent=1), encoding="utf-8")
            done["ran"] += 1
            if report["incomplete"]:
                done["incomplete"] += 1
                log(f"{model}: its rate limit or allowance ran out; this run does not count. Moving on to the next model.")
                break
            log(f"{model}: run {k}: {report['passed']}/{report['total']}")
    return done


def load(directory: Path) -> list[dict]:
    out = []
    for f in sorted(Path(directory).glob("*__r*.json")):
        try:
            report = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and "bench" in report:
            out.append(report)
    return out


def summarize(reports: list[dict]) -> dict:
    """The leaderboard: per model, over the newest suite's complete runs."""
    complete = [r for r in reports if not r.get("incomplete")]
    if not complete:
        return {"fingerprint": None, "models": [], "left_out": len(reports), "incomplete": len(reports) - len(complete)}
    newest = max(complete, key=lambda r: r["bench"].get("started", r.get("when", 0)))["bench"]["fingerprint"]
    current = [r for r in complete if r["bench"]["fingerprint"] == newest]
    rows = []
    for model in sorted({r["model"] for r in current}):
        runs = [r for r in current if r["model"] == model]
        scores = [r["score"] for r in runs]
        ids = sorted({x["id"] for r in runs for x in r["results"]})
        passed_by = {i: [any(x["id"] == i and x["passed"] for x in r["results"]) for r in runs] for i in ids}
        cats: dict[str, list[float]] = {}
        for r in runs:
            per: dict[str, list[bool]] = {}
            for x in r["results"]:
                per.setdefault(x.get("category", "general"), []).append(x["passed"])
            for c, v in per.items():
                cats.setdefault(c, []).append(100.0 * sum(v) / len(v))
        rows.append({
            "model": model, "runs": len(runs), "tasks": len(ids),
            "mean": round(statistics.mean(scores), 1), "min": min(scores), "max": max(scores),
            "stdev": round(statistics.stdev(scores), 1) if len(scores) > 1 else None,
            "reliable": sum(all(v) for v in passed_by.values()), "ever": sum(any(v) for v in passed_by.values()),
            "tokens": round(statistics.mean(r["tokens"] for r in runs)),
            "minutes": round(statistics.mean(r["duration_ms"] for r in runs) / 60000, 1),
            "categories": {c: round(statistics.mean(v), 1) for c, v in sorted(cats.items())},
            "flaky": sorted(i for i, v in passed_by.items() if any(v) and not all(v)),
        })
    rows.sort(key=lambda r: (-r["mean"], -r["reliable"], r["model"]))
    sample = current[0]["bench"]
    return {"fingerprint": newest, "models": rows, "abp_version": sample.get("abp_version"), "commit": sample.get("commit"),
            "from": min(r["bench"]["started"] for r in current), "to": max(r["bench"]["started"] for r in current),
            "left_out": len(complete) - len(current), "incomplete": len(reports) - len(complete)}


def render(summary: dict) -> str:
    if not summary["models"]:
        return "No complete benchmark runs yet. Run: python -m abp_agenteval bench run --models auto --dir DIR"
    lines = [f"Suite {summary['fingerprint']}  (ABP {summary['abp_version']}, commit {summary['commit'] or '?'})",
             f"{'model':<48} {'runs':>4} {'mean':>6} {'range':>11} {'reliable':>9} {'tokens':>8}"]
    for r in summary["models"]:
        lines.append(f"{r['model'][:48]:<48} {r['runs']:>4} {r['mean']:>5}% {r['min']:>4}-{r['max']:<5}% {r['reliable']:>4}/{r['tasks']:<4} {r['tokens']:>8}")
    if summary["left_out"] or summary["incomplete"]:
        lines.append(f"(left out: {summary['left_out']} run(s) of an older suite, {summary['incomplete']} cut short by a rate limit)")
    return "\n".join(lines)


def resolve_models(spec: str) -> list[str]:
    if spec.strip() == "auto":
        from bot import model_router

        return model_router.candidate_models()
    return [m.strip() for m in spec.split(",") if m.strip()]


def live_probe(factory: Callable, api_model: str) -> str:
    """One tiny request: "" if the model answers, "limited: ..." if its allowance is out, else the reason it failed."""
    import asyncio

    transport = factory(None)

    def ask() -> None:
        asyncio.run(transport.send(model=api_model, history=[transport.user_message("Reply with OK.")], tool_schemas=[],
                                   max_tokens=16, timeout_s=90, system_prompt="Reply with OK.", effort=None))

    try:
        try:
            ask()
        except Exception as first:  # noqa: BLE001
            # An Unsloth Studio model that exists but is not loaded is loaded now, the way a bot would get it.
            from bot.unsloth import harness as studio

            if not studio.not_loaded_error(str(first)):
                raise
            studio.ensure_loaded(api_model)
            ask()
        return ""
    except Exception as exc:  # noqa: BLE001
        text = re.sub(r"\s+", " ", str(exc))[:240] or type(exc).__name__
        return f"limited: {text}" if LIMIT_PATTERNS.search(text) else text


def live_factory(model_ref: str) -> tuple[Callable, str]:
    """(per-task transport factory, API model id) for "provider/model", the provider a name in config/providers.yaml."""
    from abp_run import core

    provider, model = core.split_model(model_ref, default_provider="")
    if not provider:
        raise ValueError(f"{model_ref!r} names no configured provider; use provider/model")
    transport = core.transport_for(provider, model)
    return (lambda _task: transport), local_exclusive(str(getattr(transport, "base_url", "") or ""), model)


def local_exclusive(base_url: str, model: str, *, log: Callable[[str], None] = print) -> str:
    """A model served on this machine (Ollama or Unsloth Studio) gets the GPU to itself: every other model loaded on
    either server is unloaded first, so two models never compete for VRAM and each is timed on its own. An Ollama model
    is sent as the name bots use (its `-abp` serving model, at a context that fits). Returns the model id to send."""
    from bot.ollama import client as ollama_client
    from bot.ollama import harness as ollama
    from bot.unsloth import client as studio_client
    from bot.unsloth import harness as studio

    o = ollama_client.find()
    s = studio_client.find()
    on_ollama = bool(o and base_url.startswith(o.root))
    on_studio = bool(s and base_url.startswith(s.root))
    if not (on_ollama or on_studio):
        return model
    api_model = ollama.serving_model(model) if on_ollama else model
    if o:
        for m in ollama_client.request("GET", "/api/ps").get("models") or []:
            name = m.get("name") or m.get("model")
            if name and not (on_ollama and name == api_model):
                log(f"unloading {name} from Ollama so {model} has the GPU to itself")
                ollama.unload(name)
    if s:
        for name in studio.loaded():
            if not (on_studio and name == model):
                log(f"unloading {name} from Unsloth Studio so {model} has the GPU to itself")
                studio.unload(name)
    if on_ollama:
        if "cloud" not in api_model:        # a cloud model runs on ollama.com, nothing to load here
            ollama.load(api_model)
    else:
        studio.ensure_loaded(model)
    return api_model
