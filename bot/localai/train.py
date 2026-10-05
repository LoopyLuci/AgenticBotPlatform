"""Fine-tuning on this machine: what Unsloth does, built into ABP.

    env        the training environment, <home>/venv-train: PyTorch for the GPU (AMD's ROCm wheels from repo.amd.com on
               Windows/Linux with a Radeon, the CUDA wheels with an NVIDIA card) and transformers, peft, datasets
    start      a run: a base model (a Hugging Face checkpoint — a repo id in the HF cache, or a folder), data (JSONL /
               JSON / Parquet in OpenAI, ShareGPT, Alpaca, prompt/completion or preference formats, or ABP Studio's
               sft.jsonl / prefs.jsonl), method sft or dpo, LoRA settings. The work happens in bot/localai/train_worker.py,
               a separate process in the training environment, on the GPU
    runs       every run with its live status (loss, learning rate, step, ETA, GPU memory); stop one (it saves what it
               has learned so far)
    export     merged weights -> GGUF (llama.cpp's convert_hf_to_gguf.py) -> quantized (llama-quantize) -> a model in
               ABP's store, runnable at once from the Ollama-compatible API; the LoRA alone -> a GGUF adapter
               (Modelfile ADAPTER)

Run directories: <home>/train/<run>/{job.json, status.json, worker.log, adapter/, merged/, export/}.
Training never uses the CPU (unless a job says allow_cpu); conversion and quantizing use at most CPU_THREADS threads.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx

from bot.localai import engine, models
from bot.localai.paths import LocalAIError, cpu_threads, guard, home, sub
from bot.sandbox_ns.cell import Cell, cell_for, new_cell
from bot.sandbox_ns.spawn import spawn as ns_spawn

OWNER = "localai.train"
WORKER = Path(__file__).with_name("train_worker.py")
AMD_INDEX = "https://repo.amd.com/rocm/whl/{arch}/"       # AMD's official PyTorch wheels (ROCm 7.x, Windows and Linux)
LIBS = ["transformers", "peft", "datasets", "accelerate", "safetensors", "sentencepiece", "protobuf", "numpy"]
QUANTS = ["q4_k_m", "q5_k_m", "q6_k", "q8_0", "q4_0", "q3_k_m", "q2_k", "iq4_xs", "f16", "bf16"]
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_BELOW_NORMAL = 0x00004000 if sys.platform == "win32" else 0
Log = Callable[[str], None]

#: The 'worker' cell per run id, for the training workers this process started. A run is hours of
#: GPU work in one process, so it gets the heavy preset (memory, CPU rate, the processors ABP must
#: keep off) and a cell whose kill really is its whole tree. The worker itself still stops the way
#: it always has - by noticing <run>/stop and saving its checkpoints - so stop() only writes that
#: sentinel; this is here so the run is recorded, bounded, and visible on the diagnostics page.
_worker_cells: dict[str, Cell] = {}
_worker_lock = threading.Lock()


# ---- the training environment ------------------------------------------------------------------------------------- #

def venv() -> Path:
    return home() / "venv-train"


def python() -> Path:
    return venv() / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _gfx_family() -> str:
    """AMD's wheel index is per GPU family: gfx110X-all (RX 7000), gfx120X-all (RX 9000), gfx1151 (Strix Halo)..."""
    names = " ".join(g["name"] for g in engine.gpus()).lower()
    if re.search(r"rx\s*9\d{3}", names):
        return "gfx120X-all"
    if "8060s" in names or "8050s" in names:
        return "gfx1151"
    if re.search(r"rx\s*7\d{3}|w7\d{3}", names):
        return "gfx110X-all"
    return "gfx110X-all"


def env_status() -> dict:
    py = python()
    if not py.exists():
        return {"installed": False, "path": str(venv())}
    code = ("import json,torch;ok=torch.cuda.is_available();print(json.dumps({'torch':torch.__version__,'gpu':ok,"
            "'device':torch.cuda.get_device_name(0) if ok else '','hip':getattr(torch.version,'hip',None),"
            "'cuda':torch.version.cuda,'vram_gb':round(torch.cuda.get_device_properties(0).total_memory/2**30,1) if ok else 0}))")
    try:
        r = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
        info = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"error": r.stderr[-800:]}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as e:
        info = {"error": str(e)}
    libs = {}
    try:
        r = subprocess.run([str(py), "-m", "pip", "list", "--format=json"], capture_output=True, text=True, timeout=120,
                           creationflags=_NO_WINDOW)
        have = {p["name"].lower(): p["version"] for p in json.loads(r.stdout)}
        libs = {n: have.get(n) for n in ["torch", "transformers", "peft", "datasets", "accelerate", "gguf"]}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"installed": True, "path": str(venv()), **info, "libraries": libs}


def setup_env(log: Log = lambda m: None, vendor: str = "") -> dict:
    """Create the training environment: Python 3.11-3.13 venv, PyTorch for this GPU, the training libraries."""
    vendor = vendor or next((g["vendor"] for g in engine.gpus() if g["vendor"] in ("amd", "nvidia")), "")
    if not vendor:
        raise LocalAIError("no AMD or NVIDIA GPU found: fine-tuning needs one (this machine's CPU must not train)")
    # One cell for the whole setup: these five commands are one unit of work, and a half-installed
    # venv is what a stop halfway through leaves behind.
    with cell_for("worker", name="train env setup", owner=OWNER) as cell:
        _setup_env(cell, log, vendor)
    return env_status()


def _setup_env(cell: Cell, log: Log, vendor: str) -> None:
    if not python().exists():
        base = _base_python()
        log(f"creating {venv()} with {base}")
        _run([base, "-m", "venv", str(venv())], log, cell=cell)
    _run([str(python()), "-m", "pip", "install", "--upgrade", "pip"], log, cell=cell)
    if vendor == "amd":
        idx = AMD_INDEX.format(arch=_gfx_family())
        log(f"installing PyTorch (ROCm) from AMD's index {idx}")
        _run([str(python()), "-m", "pip", "install", "--index-url", idx, "torch"], log, cell=cell)
    else:
        log("installing PyTorch (CUDA 12.8) from download.pytorch.org")
        _run([str(python()), "-m", "pip", "install", "--index-url", "https://download.pytorch.org/whl/cu128", "torch"],
             log, cell=cell)
    _run([str(python()), "-m", "pip", "install", *LIBS], log, cell=cell)


def _base_python() -> str:
    cands = []
    if sys.platform == "win32":
        for v in ("3.12", "3.11", "3.13"):
            try:
                r = subprocess.run(["py", f"-{v}", "-c", "import sys;print(sys.executable)"], capture_output=True, text=True,
                                   timeout=30, creationflags=_NO_WINDOW)
                if r.returncode == 0:
                    cands.append(r.stdout.strip())
            except OSError:
                pass
    if sys.version_info[:2] in ((3, 11), (3, 12), (3, 13)):
        cands.append(sys.executable)
    if not cands:
        raise LocalAIError("PyTorch's GPU wheels need Python 3.11, 3.12 or 3.13; install one of those first")
    return cands[0]


def _run(cmd: list[str], log: Log, cwd: Optional[Path] = None, env: Optional[dict] = None,
         cell: Optional[Cell] = None) -> None:
    """One conversion or install command, its output streamed into `log`.

    `cell` is the one the caller's unit of work made (an export's convert + quantize, a venv
    setup): these commands start children of their own and none of them had a timeout at all, so
    without a cell a hung pip left a process nobody was watching. With one, the 'worker' preset
    applies and the whole sequence is contained."""
    p = ns_spawn(cmd, cell=cell, preset="worker", cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 text=True, creationflags=_NO_WINDOW | _BELOW_NORMAL, encoding="utf-8", errors="replace",
                 name=Path(cmd[0]).name, owner=OWNER)
    guard(p.pid)
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-30:]
        if line and not line.startswith(("  ", "Requirement already")):
            log(line[:300])
    if p.wait():
        raise LocalAIError(f"{Path(cmd[0]).name} {' '.join(cmd[1:3])} failed:\n" + "\n".join(tail[-12:]))


def _cpu_env() -> dict:
    env = dict(os.environ)
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[k] = str(cpu_threads())
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return env


# ---- base models and data ----------------------------------------------------------------------------------------- #

TOKENIZER_FILES = ["tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "tokenizer.model",
                   "special_tokens_map.json", "generation_config.json", "chat_template.jinja"]


def has_tokenizer(folder: Path) -> bool:
    return (folder / "tokenizer.json").exists() or (folder / "tokenizer.model").exists() or \
        ((folder / "vocab.json").exists() and (folder / "merges.txt").exists())


def complete_base(repo: str, snapshot: Path, log: Log = lambda m: None) -> list[str]:
    """A cached checkpoint missing its tokenizer (a partial download): fetch only those small files, at the same
    revision, so they land in the same snapshot folder as the weights."""
    if not python().exists():
        raise LocalAIError("the training environment is not set up")
    code = ("import sys,json\nfrom huggingface_hub import hf_hub_download, list_repo_files\n"
            f"want={TOKENIZER_FILES!r}\nrepo,rev=sys.argv[1],sys.argv[2]\nhave=set(list_repo_files(repo,revision=rev))\n"
            "got=[f for f in want if f in have and hf_hub_download(repo,f,revision=rev)]\nprint(json.dumps(got))\n")
    log(f"fetching {repo}'s tokenizer files from huggingface.co (revision {snapshot.name[:10]})")
    r = subprocess.run([str(python()), "-c", code, repo, snapshot.name], capture_output=True, text=True, timeout=600,
                       env=_cpu_env(), creationflags=_NO_WINDOW)
    if r.returncode or not has_tokenizer(snapshot):
        raise LocalAIError(f"could not fetch {repo}'s tokenizer: {r.stderr[-600:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def resolve_base(base: str, fetch_missing: bool = True) -> str:
    """A base model for training: a folder with config.json + safetensors, or a repo id already in the HF cache."""
    p = Path(base)
    if p.is_dir() and (p / "config.json").exists():
        if not has_tokenizer(p):
            raise LocalAIError(f"{p} has no tokenizer files (tokenizer.json, or vocab.json + merges.txt)")
        return str(p)
    if p.is_dir() and (p / "merged" / "config.json").exists():          # a previous ABP run: keep training from it
        return str(p / "merged")
    from bot.localai import discover
    for it in discover.scan()["found"]:
        if it["kind"] == "safetensors" and it.get("repo", "").lower() == base.lower():
            if it.get("layout") == "trees" and not (Path(it["path"]) / "config.json").exists():
                discover.materialize_trees(Path(it["path"]).parent.parent)     # the standard snapshot folder, linked
            if not has_tokenizer(Path(it["path"])):
                if not fetch_missing:
                    raise LocalAIError(f"{base} in the Hugging Face cache has no tokenizer files")
                complete_base(it["repo"], Path(it["path"]))
            return it["path"]
    run = sub("train") / base
    if (run / "merged" / "config.json").exists():
        return str(run / "merged")
    raise LocalAIError(f"{base!r} is not a training checkpoint on this machine (a folder with config.json and "
                       f"*.safetensors, a Hugging Face repo in the cache, or an earlier run). GGUF files can't be trained.")


def datasets() -> list[dict]:
    """Training data ABP already has: Studio's kept changes and preferences, plus files in <home>/datasets."""
    from bot.envfile import PROJECT_ROOT
    out = []
    for d, origin in ((PROJECT_ROOT / "data" / "studio" / "datasets", "Studio"), (sub("datasets"), "Local AI")):
        for f in sorted(d.glob("*")) if d.is_dir() else []:
            if f.suffix.lower() in (".jsonl", ".json", ".parquet"):
                rows = sum(1 for _ in f.open(encoding="utf-8", errors="ignore")) if f.suffix.lower() == ".jsonl" else None
                out.append({"path": str(f), "origin": origin, "name": f.name, "rows": rows, "size": f.stat().st_size,
                            "method": "dpo" if "pref" in f.name.lower() else "sft"})
    return out


# ---- runs --------------------------------------------------------------------------------------------------------- #

DEFAULTS = {"method": "sft", "rank": 16, "alpha": 16, "dropout": 0.0, "learning_rate": None, "epochs": 1, "max_steps": 0,
            "batch_size": 2, "grad_accum": 4, "max_seq_length": 2048, "warmup_steps": None, "weight_decay": 0.0,
            "gradient_checkpointing": True, "rslora": False, "dora": False, "seed": 3407, "eval_fraction": 0.0,
            "eval_every": 0, "save_every": 0, "dpo_beta": 0.1, "merge": True, "target_modules": None}


def start(base: str, data: list[str], name: str = "", export: str = "q4_k_m", **hp) -> dict:
    """Start a fine-tuning run on the GPU; returns the run (poll runs() / status())."""
    if not python().exists():
        raise LocalAIError("the training environment is not set up: `abp ai train setup` or the Local AI page")
    for d in data:
        if not Path(d).is_file():
            raise LocalAIError(f"no such data file: {d}")
    bad = set(hp) - set(DEFAULTS) - {"allow_cpu", "resume_adapter"}
    if bad:
        raise LocalAIError(f"unknown training settings: {', '.join(sorted(bad))}")
    if export and export not in QUANTS:
        raise LocalAIError(f"export must be one of {', '.join(QUANTS)} (or empty for none)")
    if name:
        models.parse_name(name)
    if any(r["state"] in ("starting", "loading", "training", "merging") for r in runs()):
        raise LocalAIError("a run is already training (one at a time: they share the GPU)")
    rid = time.strftime("%Y%m%d-%H%M%S")
    run = sub("train") / rid
    run.mkdir(parents=True, exist_ok=True)
    job = {**{k: v for k, v in DEFAULTS.items() if v is not None}, **{k: v for k, v in hp.items() if v is not None},
           "base": resolve_base(base), "base_name": base, "data": [str(Path(d).resolve()) for d in data],
           "cpu_threads": cpu_threads(), "export": export, "name": name or f"abp/{_slug(Path(base).name)}-{rid}:{export or 'f16'}"}
    (run / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
    (run / "status.json").write_text(json.dumps({"state": "starting", "time": time.time()}), encoding="utf-8")
    logf = open(run / "worker.log", "ab")
    # One 'worker' cell for the whole run: hours of training in one process, and the cell is what
    # bounds it (memory, CPU rate, the processors ABP keeps off) and contains whatever it starts.
    cell = new_cell("worker", name=f"train {rid}", owner=OWNER)
    try:
        proc = ns_spawn([str(python()), str(WORKER), str(run)], cell=cell, stdout=logf, stderr=subprocess.STDOUT,
                        cwd=str(run), env=_cpu_env(), creationflags=_NO_WINDOW | _BELOW_NORMAL,
                        name=f"train_worker {rid}", owner=OWNER)
    except OSError as exc:
        cell.close()
        raise LocalAIError(f"could not start the training worker: {exc}") from None
    finally:
        logf.close()             # the worker has its own copy of the handle; ours is done with it
    guard(proc.pid)
    with _worker_lock:
        _worker_cells[rid] = cell
    (run / "pid").write_text(str(proc.pid))
    return {"id": rid, "dir": str(run), "pid": proc.pid, **{k: job[k] for k in ("base", "method", "name", "export")}}


def worker_cell(rid: str) -> Optional[Cell]:
    """The cell this process started run `rid`'s worker in, or None once that worker is gone (or
    for a run an earlier ABP started, whose cell died with it)."""
    with _worker_lock:
        cell = _worker_cells.get(rid)
        if cell is not None and cell.closed:
            _worker_cells.pop(rid, None)
            cell = None
        return cell


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-.")[:60] or "model"


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                           creationflags=_NO_WINDOW)
        return f'"{pid}"' in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(rid: str) -> dict:
    run = sub("train") / rid
    if not (run / "job.json").exists():
        raise LocalAIError(f"no training run {rid}")
    job = json.loads((run / "job.json").read_text(encoding="utf-8"))
    try:
        st = json.loads((run / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    pid = int((run / "pid").read_text()) if (run / "pid").exists() else 0
    active = st.get("state") in ("starting", "loading", "training", "merging", "stopping")
    if active and pid and not _alive(pid):
        st = {**st, "state": "failed", "error": st.get("error") or "the worker exited unexpectedly: see worker.log"}
    out = {"id": rid, "dir": str(run), "base": job.get("base_name"), "method": job.get("method"), "name": job.get("name"),
           "export_quant": job.get("export"), "data": job.get("data"), **st}
    exp = run / "export.json"
    if exp.exists():
        out["exported"] = json.loads(exp.read_text(encoding="utf-8"))
    return out


def runs() -> list[dict]:
    root = sub("train")
    return [status(d.name) for d in sorted(root.iterdir(), reverse=True) if (d / "job.json").exists()]


def stop(rid: str) -> dict:
    run = sub("train") / rid
    (run / "stop").write_text("stop")
    return status(rid)


def log_tail(rid: str, lines: int = 80) -> str:
    p = sub("train") / rid / "worker.log"
    try:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


# ---- export: GGUF, quantize, register ----------------------------------------------------------------------------- #

def converter(log: Log = lambda m: None) -> Path:
    """llama.cpp's Python conversion scripts (convert_hf_to_gguf.py, convert_lora_to_gguf.py and gguf-py), from the
    source of the same release as the installed engine."""
    tag = engine.engine()["build"]
    dest = home() / "engines" / f"{tag}-src"
    script = next(iter(dest.rglob("convert_hf_to_gguf.py")), None) if dest.exists() else None
    if script and (script.parent / ".abp-complete").exists():
        return script.parent
    if dest.exists():
        shutil.rmtree(dest)                       # an older, partial extraction
    url = f"https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.zip"
    log(f"downloading llama.cpp {tag}'s conversion scripts ({url})")
    zp = home() / "engines" / f"{tag}-src.zip"
    with httpx.Client(timeout=120, follow_redirects=True) as c, c.stream("GET", url) as r:
        r.raise_for_status()
        with open(zp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    # the scripts and the packages they import (recent builds split the converter into conversion/)
    keep = ("/gguf-py/", "/conversion/", "/requirements", "/models/templates/")
    with zipfile.ZipFile(zp) as z:
        for n in z.namelist():
            top_py = n.count("/") == 1 and n.endswith(".py")
            if top_py or any(k in "/" + n for k in keep):
                z.extract(n, dest)
    zp.unlink()
    script = next(iter(dest.rglob("convert_hf_to_gguf.py")), None)
    if not script:
        raise LocalAIError(f"llama.cpp {tag}'s source has no convert_hf_to_gguf.py")
    (script.parent / ".abp-complete").write_text(tag)
    return script.parent


def _conv_env(src: Path) -> dict:
    env = _cpu_env()
    env["PYTHONPATH"] = str(src / "gguf-py") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def convert_to_gguf(hf_dir: str, name: str, quant: str = "q4_k_m", log: Log = lambda m: None, out_dir: Optional[Path] = None) -> dict:
    """A Hugging Face checkpoint folder -> GGUF (f16/bf16) -> quantized -> a model in ABP's store under `name`."""
    if not python().exists():
        raise LocalAIError("converting needs the training environment (PyTorch); set it up first")
    hf = Path(hf_dir)
    src = converter(log)
    out_dir = out_dir or sub("exports") / _slug(name.replace("/", "-").replace(":", "-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    full = "bf16" if quant == "bf16" else "f16"
    f16 = out_dir / f"model-{full}.gguf"
    # Convert and quantize are one unit of work (hours on a big checkpoint), so they share a cell.
    with cell_for("worker", name=f"convert {name}", owner=OWNER) as cell:
        log(f"converting {hf} to GGUF ({full})")
        _run([str(python()), str(src / "convert_hf_to_gguf.py"), str(hf), "--outfile", str(f16), "--outtype", full],
             log, env=_conv_env(src), cell=cell)
        final = f16
        if quant not in ("f16", "bf16"):
            final = out_dir / f"model-{quant}.gguf"
            n = cpu_threads()
            log(f"quantizing to {quant.upper()} ({n} threads)")
            _run([engine.tool("llama-quantize"), str(f16), str(final), quant.upper(), str(n)], log, cell=cell)
            f16.unlink()
    rec = models.import_file(name, str(final))
    log(f"registered {rec['name']} ({final.stat().st_size >> 20} MiB)")
    return {"name": rec["name"], "gguf": str(final), "size": final.stat().st_size, "quant": quant}


def export(rid: str, quant: str = "", name: str = "", log: Log = lambda m: None, adapter_gguf: bool = True) -> dict:
    """A finished run -> a runnable model (merged, GGUF, quantized, in the store) and its LoRA as a GGUF adapter."""
    st = status(rid)
    run = Path(st["dir"])
    job = json.loads((run / "job.json").read_text(encoding="utf-8"))
    if st.get("state") != "done":
        raise LocalAIError(f"run {rid} is {st.get('state')}, not done")
    quant = quant or job.get("export") or "q4_k_m"
    name = name or job["name"]
    out = {}
    if (run / "merged" / "config.json").exists():
        out = convert_to_gguf(str(run / "merged"), name, quant, log, run / "export")
    if adapter_gguf and (run / "adapter").is_dir():
        src = converter(log)
        lora = run / "export" / "adapter-f16.gguf"
        lora.parent.mkdir(exist_ok=True)
        log("converting the LoRA adapter to GGUF")
        try:
            with cell_for("worker", name=f"convert adapter {rid}", owner=OWNER) as cell:
                _run([str(python()), str(src / "convert_lora_to_gguf.py"), str(run / "adapter"), "--base", job["base"],
                      "--outfile", str(lora), "--outtype", "f16"], log, env=_conv_env(src), cell=cell)
            out["adapter"] = str(lora)
        except LocalAIError as e:
            out["adapter_error"] = str(e)[:400]
    out["time"] = time.time()
    (run / "export.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def tick(log: Log = lambda m: None) -> list[dict]:
    """Export runs that finished and asked for it (called by ABP's background loop)."""
    done = []
    for r in runs():
        if r.get("state") == "done" and r.get("export_quant") and "exported" not in r:
            try:
                done.append(export(r["id"], log=log))
            except LocalAIError as e:
                (Path(r["dir"]) / "export.json").write_text(json.dumps({"error": str(e)[:2000], "time": time.time()}))
                done.append({"id": r["id"], "error": str(e)})
    return done
