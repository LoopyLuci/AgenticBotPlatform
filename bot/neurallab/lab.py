"""Neural Lab runs: train a spec on the GPU (bot/neurallab/nn_worker.py in the training environment), watch it,
stop it, compare runs, search designs.

    <lab home>/runs/<id>/{job.json, status.json, worker.log, best.pt, export/}
    <lab home>/designs/<name>.json            saved specs (from the GUI's designer, BrainBuilder, KotMoE, templates)

The lab home is <localai home>/lab (E:\\ABP-LocalAI\\lab on this machine). Training shares the GPU with ABP's other
GPU work: one run at a time, queued otherwise.
"""
from __future__ import annotations

import json
import random
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from bot.localai import train as lt
from bot.localai.paths import LocalAIError, cpu_threads, guard, home
from bot.neurallab import spec as specs

WORKER = Path(__file__).with_name("nn_worker.py")
ACTIVE = ("starting", "loading", "training", "stopping")


def root() -> Path:
    p = home() / "lab"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _dir(kind: str) -> Path:
    p = root() / kind
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- designs ------------------------------------------------------------------------------------------------------- #

def designs() -> list[dict]:
    out = []
    for f in sorted(_dir("designs").glob("*.json")):
        try:
            s = specs.validate(specs.load(f))
            out.append({"name": f.stem, "task": s["task"], "params": s["stats"]["params"],
                        "active_params": s["stats"]["active_params"], "origin": s.get("origin", {}), "path": str(f)})
        except (ValueError, OSError, KeyError) as e:
            out.append({"name": f.stem, "error": str(e), "path": str(f)})
    return out


def save_design(spec: dict, name: str = "") -> dict:
    s = specs.validate(spec)
    name = name or s["name"]
    p = specs.save({k: v for k, v in specs.normalise(spec).items()}, _dir("designs") / f"{_slug(name)}.json")
    return {"name": p.stem, "path": str(p), "stats": s["stats"]}


def design(name: str) -> dict:
    p = _dir("designs") / f"{_slug(name)}.json"
    if not p.exists():
        raise LocalAIError(f"no design named {name}")
    return specs.load(p)


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")[:80] or "design"


# ---- runs ---------------------------------------------------------------------------------------------------------- #

def start(spec: dict, data: dict, train: Optional[dict] = None, label: str = "", **extra) -> dict:
    """Train a design on the GPU. data: {"path", "target"?, "features"?, "tokenizer"?}. extra: init_kmoe (a KotMoE
    checkpoint to start from), init_weights, kmoe (write KotMoE's format too), sample_prompt, allow_cpu."""
    if not lt.python().exists():
        raise LocalAIError("the training environment is not set up: `abp ai train setup` or the Local AI page")
    s = specs.validate(spec)
    paths = data.get("path")
    for p in ([paths] if isinstance(paths, str) else paths or []):
        if not Path(p).is_file():
            raise LocalAIError(f"no such data file: {p}")
    if any(r["state"] in ACTIVE for r in runs()) or any(r["state"] in ("starting", "loading", "training", "merging") for r in lt.runs()):
        raise LocalAIError("the GPU is busy with another training run (one at a time)")
    rid = time.strftime("%Y%m%d-%H%M%S") + f"-{random.randrange(16**3):03x}"
    run = _dir("runs") / rid
    run.mkdir()
    job = {"spec": s, "data": data, "train": train or {}, "label": label or s["name"], "cpu_threads": cpu_threads(), **extra}
    (run / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
    (run / "status.json").write_text(json.dumps({"state": "starting", "time": time.time()}), encoding="utf-8")
    logf = open(run / "worker.log", "ab")
    proc = subprocess.Popen([str(lt.python()), str(WORKER), str(run)], stdout=logf, stderr=subprocess.STDOUT, cwd=str(run),
                            env=lt._cpu_env(), creationflags=lt._NO_WINDOW | lt._BELOW_NORMAL)
    guard(proc.pid)
    (run / "pid").write_text(str(proc.pid))
    return {"id": rid, "dir": str(run), "pid": proc.pid, "name": s["name"], "params": s["stats"]["params"]}


def status(rid: str) -> dict:
    run = _dir("runs") / rid
    if not (run / "job.json").exists():
        raise LocalAIError(f"no lab run {rid}")
    job = json.loads((run / "job.json").read_text(encoding="utf-8"))
    try:
        st = json.loads((run / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    pid = int((run / "pid").read_text()) if (run / "pid").exists() else 0
    if st.get("state") in ACTIVE and pid and not lt._alive(pid):
        st = {**st, "state": "failed", "error": st.get("error") or "the worker exited unexpectedly: see worker.log"}
    return {"id": rid, "dir": str(run), "label": job.get("label"), "name": job["spec"]["name"], "task": job["spec"]["task"],
            "params": job["spec"]["stats"]["params"], "origin": job["spec"].get("origin", {}), **st}


def runs(limit: int = 100) -> list[dict]:
    out = []
    for d in sorted(_dir("runs").iterdir(), reverse=True)[:limit]:
        if (d / "job.json").exists():
            try:
                out.append(status(d.name))
            except (LocalAIError, ValueError, KeyError):
                continue
    return out


def stop(rid: str) -> dict:
    (_dir("runs") / rid / "stop").write_text("stop")
    return status(rid)


def wait(rid: str, timeout: float = 3600, poll: float = 2.0, on: Callable[[dict], None] = lambda s: None) -> dict:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        s = status(rid)
        if s.get("step") != last:
            on(s)
            last = s.get("step")
        if s["state"] not in ACTIVE:
            return s
        time.sleep(poll)
    raise LocalAIError(f"run {rid} still running after {timeout:.0f}s")


def export_of(rid: str) -> dict:
    p = _dir("runs") / rid / "export" / "model.json"
    if not p.exists():
        raise LocalAIError(f"run {rid} has no export (not finished?)")
    return {**json.loads(p.read_text(encoding="utf-8")), "dir": str(p.parent)}


def compare(ids: list[str]) -> list[dict]:
    keys = ("val_loss", "val_accuracy", "val_r2", "val_mae", "val_perplexity")
    out = []
    for rid in ids:
        s = status(rid)
        fin = s.get("final") or {}
        out.append({"id": rid, "name": s["name"], "params": s["params"], "state": s["state"], "elapsed": s.get("elapsed"),
                    **{k: fin.get(k) for k in keys if fin.get(k) is not None}})
    return out


# ---- design search ------------------------------------------------------------------------------------------------- #

def autotune(spec: dict, data: dict, space: dict, trials: int = 8, train: Optional[dict] = None, metric: str = "val_loss",
             log: Callable[[str], None] = lambda m: None, timeout_each: float = 1800) -> dict:
    """Random search over a design's settings, one GPU run at a time. space: {"<node>.<param>": [choices],
    "train.lr": [...]}; returns every trial and the best (lowest val_loss, or highest accuracy / r2)."""
    rnd = random.Random(int(time.time()))
    higher = metric in ("val_accuracy", "val_r2")
    trials_out, best = [], None
    tried = set()
    for t in range(trials):
        choice = {k: rnd.choice(v) for k, v in space.items()}
        key = json.dumps(choice, sort_keys=True)
        if key in tried:
            continue
        tried.add(key)
        s = specs.normalise(spec)
        tr = dict(train or {})
        for k, v in choice.items():
            node, _, param = k.partition(".")
            if node == "train":
                tr[param] = v
            else:
                n = next((n for n in s["nodes"] if n["id"] == node), None)
                if n is None:
                    raise LocalAIError(f"search space: no node {node}")
                n["params"][param] = v
        try:
            specs.validate(s)
        except specs.SpecError as e:
            trials_out.append({"choice": choice, "error": str(e)})
            continue
        r = start(s, data, tr, label=f"{spec.get('name', 'model')} trial {t + 1}")
        log(f"trial {t + 1}/{trials}: {choice} -> run {r['id']}")
        fin = wait(r["id"], timeout_each)
        score = (fin.get("final") or {}).get(metric)
        trials_out.append({"choice": choice, "run": r["id"], "state": fin["state"], metric: score, "params": r["params"]})
        log(f"  {fin['state']}, {metric} = {score}")
        if score is not None and (best is None or (score > best[metric] if higher else score < best[metric])):
            best = trials_out[-1]
    return {"metric": metric, "best": best, "trials": trials_out}
