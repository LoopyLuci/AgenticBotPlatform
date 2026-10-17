"""Fine-tuning through ModelMistress's Forge (the hub operation `forge.run`), ABP's default trainer.

ABP's own PEFT trainer (train.py + train_worker.py) stays as the fallback. config `training.backend`:
    auto        (default) the Forge when a ModelMistress hub is up and its Forge is installed, else the in-process trainer
    forge       only the Forge; a clear error when there is no hub / no Forge / the request needs something it lacks
    inprocess   only ABP's own trainer
A Forge run still gets an ABP run directory (<home>/train/<id>/{job.json, status.json}) so runs(), status(), stop() and
the UI see it like any other run; job.json carries backend="forge" and the Forge run id, and status.json is refreshed
from `forge.run.get` whenever the run is read.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from bot.localai.paths import LocalAIError

BACKENDS = ("auto", "forge", "inprocess")
MIN_FREE_VRAM_GB = 2.0
#: ABP training settings the Forge's lora_train understands (ABP name -> Forge name); everything else is dropped.
_ARG_MAP = {"rank": "rank", "alpha": "alpha", "dropout": "dropout", "learning_rate": "learning_rate", "epochs": "epochs",
            "batch_size": "batch_size", "grad_accum": "grad_accum", "max_seq_length": "max_seq_length",
            "warmup_steps": "warmup_steps", "weight_decay": "weight_decay", "gradient_checkpointing": "gradient_checkpointing",
            "rslora": "rslora", "dora": "dora", "seed": "seed", "eval_fraction": "eval_fraction", "eval_every": "eval_every",
            "save_every": "save_every", "target_modules": "target_modules", "merge": "merge", "max_grad_norm": "max_grad_norm"}
_STATE = {"running": "training", "done": "done", "failed": "failed", "cancelled": "failed", "interrupted": "failed"}
_ACTIVE = ("starting", "loading", "training", "merging", "stopping")


def _cfg() -> dict:
    try:
        from bot.config import config
        return config.current.get("training") or {}
    except Exception:  # noqa: BLE001 - config trouble must not stop a run from starting
        return {}


def backend() -> str:
    b = str(_cfg().get("backend") or "auto").strip().lower()
    return b if b in BACKENDS else "auto"


def _client():
    from bot.modelmistress import client
    return client


def forge_ready() -> tuple[bool, str]:
    """(usable, why not): a hub answers and its Forge is installed. Never raises."""
    c = _client()
    try:
        hub = c.find()
        if hub is None:
            return False, "no ModelMistress hub is running"
        if hub.remote:
            return False, "the hub is a linked machine's (training needs the local hub)"
        st = c.call("forge.status", {}, hub=hub, timeout=30) or {}
    except Exception as e:  # noqa: BLE001 - the hub missing the op or dying is a fallback case
        return False, f"the ModelMistress Forge is not reachable: {e}"
    if not st.get("installed"):
        return False, "the ModelMistress Forge is not installed (run `forge install` on the hub)"
    return True, ""


def choose(method: str = "sft") -> str:
    """'forge' or 'inprocess' for a new run; raises LocalAIError when backend=forge cannot be honoured."""
    b = backend()
    if b == "inprocess":
        return "inprocess"
    ok, why = forge_ready()
    if ok and method != "sft":
        ok, why = False, f"the Forge trains sft only, not {method}"
    if ok:
        return "forge"
    if b == "forge":
        raise LocalAIError(f"training.backend is 'forge' but {why}")
    return "inprocess"


def map_args(job: dict, out_dir: str) -> tuple[str, dict]:
    """ABP's job -> (Forge step, its args object)."""
    data = job["data"]
    args: dict[str, Any] = {"base_model": job["base"], "data": data[0] if len(data) == 1 else list(data), "out_dir": out_dir}
    for k, f in _ARG_MAP.items():
        if job.get(k) is not None:
            args[f] = job[k]
    if job.get("max_steps"):
        args["max_steps"] = int(job["max_steps"])
        args.pop("epochs", None)
    if job.get("allow_cpu"):
        args["device"] = "cpu"
    if job.get("resume_adapter"):
        args["resume_adapter"] = job["resume_adapter"]
    return ("qlora_train" if job.get("load_in_4bit") or job.get("qlora") else "lora_train"), args


def _check_vram() -> None:
    from bot import gpu
    need = float(_cfg().get("min_free_vram_gb", MIN_FREE_VRAM_GB))
    free = gpu.free_vram_gb()
    if free is not None and free < need:
        raise LocalAIError(f"only {free:.1f} GiB of GPU memory is free (training needs at least {need:g}): stop what is using it")


def start(run: Path, job: dict) -> dict:
    """Start the Forge run for an ABP run directory whose job.json is written; returns {'forge_run', 'step'}."""
    if not job.get("allow_cpu"):
        _check_vram()
    step, args = map_args(job, str(run))
    try:
        res = _client().call("forge.run", {"step": step, "args": args}, timeout=60) or {}
    except Exception as e:  # noqa: BLE001
        raise LocalAIError(f"the Forge refused the run: {e}") from None
    rid = res.get("run")
    if not rid:
        raise LocalAIError(f"the Forge did not return a run id: {str(res)[:300]}")
    job.update(backend="forge", forge_run=rid, forge_step=step)
    (run / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
    write_status(run, {"state": "starting", "time": time.time(), "backend": "forge", "forge_run": rid})
    return {"forge_run": rid, "step": step}


def write_status(run: Path, st: dict) -> None:
    tmp = run / "status.json.tmp"
    tmp.write_text(json.dumps(st), encoding="utf-8")
    tmp.replace(run / "status.json")


def _last(metrics: Any, name: str) -> Optional[float]:
    """The latest value of a metric, from whatever shape forge.run.get gives (name -> {last}|[points]|value, or a
    list of {name, value, step} points)."""
    if isinstance(metrics, dict):
        for key in ("summary", "points"):
            if isinstance(metrics.get(key), (dict, list)):
                r = _last(metrics[key], name)
                if r is not None:
                    return r
        v = metrics.get(name)
        if isinstance(v, dict):
            v = v.get("last", v.get("value"))
        elif isinstance(v, list) and v:
            v = v[-1].get("value") if isinstance(v[-1], dict) else v[-1]
        return float(v) if isinstance(v, (int, float)) else None
    if isinstance(metrics, list):
        pts = [p for p in metrics if isinstance(p, dict) and p.get("name") == name]
        if pts:
            v = pts[-1].get("value")
            return float(v) if isinstance(v, (int, float)) else None
    return None


def map_status(g: dict, prev: dict, run: Optional[Path] = None) -> dict:
    """A forge.run.get answer -> ABP's status.json fields (state, step, loss, lr, eta, gpu_mem_gb, error...)."""
    st = {**prev, "state": _STATE.get(str(g.get("status")), "training"), "backend": "forge"}
    m = g.get("metrics")
    for name in ("loss", "lr", "eval_loss", "gpu_mem_gb", "grad_norm"):
        v = _last(m, name)
        if v is not None:
            st[name] = v
    prog = g.get("progress")
    if isinstance(prog, dict):
        if prog.get("done") is not None:
            st["step"] = prog["done"]
        if prog.get("total"):
            st["total_steps"] = prog["total"]
        if prog.get("note"):
            st["note"] = prog["note"]
    if g.get("seconds") is not None:
        st["elapsed"] = g["seconds"]
        if st.get("step") and st.get("total_steps"):
            st["eta"] = round(float(g["seconds"]) / max(1, st["step"]) * (st["total_steps"] - st["step"]), 1)
    if st["state"] == "failed":
        st["error"] = str(g.get("error") or g.get("message") or f"the Forge run {g.get('status')}")[:2000]
    if st["state"] == "done" and run is not None:
        st["adapter"] = str(run / "adapter")
    return st


def refresh(run: Path, job: dict, st: dict) -> dict:
    """Poll the Forge run behind `run` while it is active; the new status (also written to status.json)."""
    if st.get("state") not in _ACTIVE:
        return st
    c = _client()
    try:
        g = c.call("forge.run.get", {"run": job["forge_run"], "log_lines": 0, "max_points": 20}, timeout=20) or {}
    except Exception as e:  # noqa: BLE001
        if c.find(refresh=True) is None:
            new = {**st, "state": "failed", "error": f"the ModelMistress hub went away: {e}"[:600]}
            write_status(run, new)
            return new
        return {**st, "note": f"the hub did not answer ({e})"[:200]}
    new = map_status(g, st, run)
    new["time"] = time.time()
    if {k: v for k, v in new.items() if k != "time"} != {k: v for k, v in st.items() if k != "time"}:
        write_status(run, new)
    return new


def stop(job: dict) -> None:
    try:
        _client().call("forge.run.cancel", {"run": job["forge_run"]}, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise LocalAIError(f"could not cancel the Forge run: {e}") from None


def log_tail(job: dict, lines: int = 80) -> str:
    try:
        g = _client().call("forge.run.get", {"run": job["forge_run"], "log_lines": int(lines), "max_points": 2}, timeout=20) or {}
    except Exception as e:  # noqa: BLE001
        return f"(the Forge log is not reachable: {e})"
    log = g.get("log")
    if isinstance(log, list):
        return "\n".join(x.get("message", str(x)) if isinstance(x, dict) else str(x) for x in log)
    return str(log or "")


def export(run: Path, job: dict, name: str, quant: str, log) -> dict:
    """A finished Forge run -> GGUF through the Forge's lora_to_gguf, imported into ABP's store (the hub serves it)."""
    from bot.localai import models
    out_dir = run / "export"
    out_dir.mkdir(exist_ok=True)
    args = {"base_model": job["base"], "adapter": str(run / "adapter"), "out_dir": str(out_dir), "name": name.replace("/", "-"),
            "outtypes": ["f16"], "quantize": [quant.upper()] if quant not in ("f16", "bf16") else []}
    log("converting through the ModelMistress Forge (lora_to_gguf)")
    try:
        res = _client().call("forge.run", {"step": "lora_to_gguf", "args": args, "wait": True}, timeout=6 * 3600) or {}
    except Exception as e:  # noqa: BLE001
        raise LocalAIError(f"the Forge could not convert the run: {e}") from None
    if res.get("status") not in (None, "done"):
        raise LocalAIError(f"the Forge conversion {res.get('status')}: {str(res.get('error') or res.get('message') or '')[:400]}")
    files = sorted(out_dir.rglob("*.gguf"), key=lambda p: p.stat().st_mtime)
    pick = [f for f in files if quant.lower() in f.name.lower()] or files
    if not pick:
        raise LocalAIError("the Forge conversion produced no GGUF file")
    final = pick[-1]
    rec = models.import_file(name, str(final))
    log(f"registered {rec['name']} ({final.stat().st_size >> 20} MiB)")
    return {"name": rec["name"], "gguf": str(final), "size": final.stat().st_size, "quant": quant, "backend": "forge"}
