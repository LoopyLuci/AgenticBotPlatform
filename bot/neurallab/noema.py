"""NOEMA as a Neural Lab model family: its checkpoints, their configs, and one rollout from one.

NOEMA (X:/Projects/NOEMA, the owner's own byte-level language model) keeps no index of what it has
trained: a checkpoint is a `.pt` in a folder, and the shape of the model it goes with is a YAML beside
the code, not inside the weights. So the three questions the lab answers for every other family - what
models exist, what is one of them, what does it say - are answered here by reading NOEMA's own files
(no import from it, so nothing here needs torch) and, for the third, by running NOEMA's own CLI.

    repo()                     the checkout: $NOEMA_HOME, else what catalog/noema says it is
    checkpoints()              the checkpoint files, newest first (noema/paths.py's own order:
                               $NOEMA_CKPT_DIR, then X:/NOEMA/checkpoints, then the repo's checkpoints/)
    configs() / config()       the YAML configs, as the three layers noema.core.config reads
    generate()                 one rollout, as a sandbox_ns worker: NOEMA's CLI under the training
                               environment's interpreter, windowless and below normal priority

Nothing here trains, and nothing is written into the checkout. A generation runs the project's own
`python -m noema.cli.main`, which is what its `generate.run` operation runs too: the point is to ask a
checkpoint for bytes, not to reimplement NOEMA. `ckpt=""` is NOEMA's own default and means random
weights, which is how a rollout can be exercised on a machine whose shipped checkpoint has drifted
away from the code that reads it.
"""
from __future__ import annotations

import ast
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import yaml

from bot.localai.paths import LocalAIError, cpu_threads
from bot.sandbox_ns import spawn
from bot.sandbox_ns.cell import cell_for

#: NOEMA writes checkpoints under two suffixes: `.pt` (its own list_checkpoints' filter) and `.ckpt`,
#: which train.py's _save_ckpt() takes as a path argument. Both are model weights on disk, so both are
#: listed here - a filter that hid 29 of the 34 files in X:/NOEMA/checkpoints would be a quiet lie.
CKPT_SUFFIXES = (".pt", ".pth", ".ckpt", ".bin", ".safetensors")
DEFAULT_CONFIG = "configs/tiny_8m.yaml"          # noema/cli/main.py's own argparse default
MAX_NEW = 4096                                  # a byte-level rollout is O(n^2); this is a ceiling, not a default


def _root_available(p: Path) -> bool:
    """True when the drive a path sits on is really there (noema/paths.py's own check)."""
    try:
        drive = p.drive
        if drive and not Path(f"{drive}\\").exists():
            return False
    except (OSError, ValueError):
        return False
    return True


def repo() -> Optional[Path]:
    """The checkout, or None when NOEMA is not on this machine. $NOEMA_HOME wins; then what ABP's own
    catalog overlay declares (`repo = "local: X:/Projects/NOEMA"` in catalog/noema/abp-module.toml), so
    the catalogue stays the one place that says where it is; then a sibling of ABP's checkout."""
    env = os.environ.get("NOEMA_HOME", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    declared = ""
    try:
        from bot.modules import registry

        m = registry.modules().get("noema")
        declared = (m.repo or "").removeprefix("local: ").strip() if m else ""
    except Exception:  # noqa: BLE001 - the module registry is not importable everywhere a worker runs
        declared = ""
    if declared:
        p = Path(declared).expanduser()
        if p.is_dir():
            return p
    from bot.envfile import PROJECT_ROOT

    sibling = Path(os.environ.get("ABP_PROJECTS_DIR") or PROJECT_ROOT.parent) / "NOEMA"
    return sibling if sibling.is_dir() else None


def python() -> Path:
    """The interpreter NOEMA needs: the training environment's, because that is where torch is."""
    from bot.localai import train as lt

    return lt.python()


def ckpt_dir() -> Optional[Path]:
    """Where NOEMA's checkpoints are, in the order noema/paths.py resolves them: the env override, then
    the external X:/NOEMA/checkpoints when that drive is there, then the repo's own folder."""
    r = repo()
    override = os.environ.get("NOEMA_CKPT_DIR", "").strip()
    if override:
        p = Path(override).expanduser()
        return r / p if r and not p.is_absolute() else p
    external = Path("X:/NOEMA/checkpoints")
    if _root_available(external):
        return external
    return (r / "checkpoints") if r else None


def checkpoints(limit: int = 100) -> list[dict]:
    """The checkpoint files on disk, newest first, with their sizes - what the lab lists as NOEMA's models."""
    d = ckpt_dir()
    if not d or not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in CKPT_SUFFIXES:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "path": str(p), "bytes": st.st_size, "mb": round(st.st_size / 1e6, 3),
                    "mtime": st.st_mtime, "mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                    "age_s": round(time.time() - st.st_mtime, 1)})
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out[:limit]


def checkpoint_path(name: str) -> Path:
    """A checkpoint by name in the resolved folder, by a repo-relative path, or by an absolute path -
    the three ways NOEMA's own ops name them (`checkpoints/phase3.pt` beside the code is a fourth)."""
    p = Path(name).expanduser()
    if p.is_absolute():
        return p
    r, d = repo(), ckpt_dir()
    for cand in ([r / p, r / "checkpoints" / p.name] if r else []) + ([d / p.name] if d else []):
        if cand.is_file():
            return cand
    return p


def configs() -> list[dict]:
    """The model configs NOEMA ships, as names and paths."""
    r = repo()
    folder = r / "configs" if r else None
    if not folder or not folder.is_dir():
        return []
    return [{"name": f.stem, "path": str(f), "bytes": f.stat().st_size} for f in sorted(folder.glob("*.yaml"))]


def _config_path(name: str) -> Path:
    p = Path(name).expanduser()
    if p.is_absolute() or p.is_file():
        return p
    r = repo()
    if r and (r / p).is_file():
        return r / p
    if r and (r / "configs" / f"{p.stem}.yaml").is_file():
        return r / "configs" / f"{p.stem}.yaml"
    return p


def summary(cfg: dict) -> dict:
    """The numbers that say which model a config describes: width, depth, experts, patch and thought
    limits, and the byte vocabulary. Read out of the three layers, so a missing key is simply absent."""
    l1, l2, l3 = (cfg.get("layer1") or {}), (cfg.get("layer2") or {}), (cfg.get("layer3") or {})
    out = {"d_model": l1.get("d_model"), "vocab_size": l1.get("vocab_size"), "max_patch_size": l1.get("max_patch_size"),
           "num_local_layers": l1.get("num_local_layers"), "patch_pooling": l1.get("patch_pooling"),
           "boundary_k": l1.get("boundary_k"), "num_layers": l2.get("num_layers"),
           "num_attention_layers": l2.get("num_attention_layers"), "num_experts": l2.get("num_experts"),
           "active_experts": l2.get("active_experts"), "use_moe": l2.get("use_moe"),
           "max_thought_steps": l3.get("max_thought_steps")}
    return {k: v for k, v in out.items() if v is not None}


def config(name: str = DEFAULT_CONFIG) -> dict:
    """One model config, read with PyYAML the way NOEMA's load_config reads it: three layers, each a
    mapping of scalars. `name` is a config's stem, a repo-relative path, or an absolute path."""
    p = _config_path(name)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise LocalAIError(f"no NOEMA config at {p} ({e}); its configs are {configs()}") from e
    except yaml.YAMLError as e:
        raise LocalAIError(f"{p} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise LocalAIError(f"{p} is not a NOEMA config (expected layer1, layer2 and layer3)")
    return {"name": p.stem, "path": str(p), "config": {k: v for k, v in raw.items() if isinstance(v, dict)},
            "summary": summary(raw)}


def status() -> dict:
    """Whether NOEMA is here at all, and what it has: the family the lab lists next to its own runs."""
    r = repo()
    py = python()
    return {"present": bool(r), "path": str(r) if r else None, "interpreter": str(py), "interpreter_present": py.is_file(),
            "checkpoints_dir": str(ckpt_dir()) if ckpt_dir() else None, "checkpoints": len(checkpoints(1000)),
            "configs": len(configs()), "trainable": False}


def _generated(output: str) -> Optional[bytes]:
    """The bytes out of NOEMA's own CLI. It prints `Generated: b'...'` (a repr, because the model emits
    raw bytes and a cp1252 console would choke on them); stderr is merged in, so the last such line wins."""
    for line in reversed(output.splitlines()):
        if line.startswith("Generated: "):
            try:
                val = ast.literal_eval(line[len("Generated: "):].strip())
            except (SyntaxError, ValueError):
                return None
            return val if isinstance(val, bytes) else None
    return None


def generate(ckpt: str = "", prompt: str = "Once upon a time", max_new: int = 32, config_name: str = DEFAULT_CONFIG,
             timeout: float = 900.0) -> dict:
    """One rollout: NOEMA's own CLI, as a sandbox_ns worker (windowless, below normal priority, in a
    cell that dies with the call). `ckpt` is a name from checkpoints(), a path, or "" for the random
    weights NOEMA's CLI starts from when given no checkpoint."""
    r = repo()
    if not r:
        raise LocalAIError("NOEMA is not on this machine: set NOEMA_HOME to its checkout (X:/Projects/NOEMA here)")
    if not 1 <= int(max_new) <= MAX_NEW:
        raise LocalAIError(f"max_new is 1..{MAX_NEW}: a byte-level rollout is O(n^2) with no cache")
    cfg = _config_path(config_name)
    if not cfg.is_file():
        raise LocalAIError(f"no NOEMA config at {cfg}")
    ck = checkpoint_path(ckpt) if str(ckpt).strip() else None
    if ck is not None and not ck.is_file():
        raise LocalAIError(f"no NOEMA checkpoint at {ck}; checkpoints(): {checkpoints(5)}")
    py = python()
    if not py.is_file():
        raise LocalAIError(f"the training environment is not set up: {py} (`abp ai train setup`) is where torch is")
    threads = cpu_threads()
    argv = [str(py), "-m", "noema.cli.main", "--config", str(cfg), "--ckpt", str(ck) if ck else "",
            "--prompt", str(prompt), "--max-new", str(int(max_new))]
    # NOEMA caps its own threads before torch is imported; so does every process ABP starts for it,
    # and ABP's cap moves with the stability policy (bot/localai/paths.py: cpu_threads).
    env = {**os.environ, "PYTHONPATH": str(r / "src"), "PYTHONIOENCODING": "utf-8",
           "OMP_NUM_THREADS": str(threads), "MKL_NUM_THREADS": str(threads), "NOEMA_THREADS": str(threads),
           "NOEMA_AUTOGUARD": "1", "NOEMA_FORCE_CPU": "1"}
    label = f"noema-generate:{ck.name if ck else 'random'}"
    started = time.time()
    with cell_for("worker", name=label, owner="neurallab.noema") as cell:
        try:
            done = spawn.run(argv, cell=cell, cwd=str(r), env=env, timeout=timeout, name=label, owner="neurallab.noema",
                             stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired as e:
            raise LocalAIError(f"NOEMA generation passed its {timeout:g}s limit and its cell was killed") from e
        notes = list(cell.notes)
    out = done.stdout or ""
    data = _generated(out)
    result = {"ok": done.returncode == 0 and data is not None, "exit_code": done.returncode, "config": str(cfg),
              "checkpoint": str(ck) if ck else "", "prompt": str(prompt), "max_new": int(max_new),
              "threads": threads, "seconds": round(time.time() - started, 2), "output": out[-4000:]}
    if notes:
        result["cell_notes"] = notes
    if done.returncode:
        result["error"] = out[-800:]
        return result
    if data is None:
        result["error"] = "NOEMA printed no Generated: line"
        return result
    return {**result, "bytes": list(data), "n_bytes": len(data), "prompt_bytes": len(str(prompt).encode()),
            "new_bytes": max(0, len(data) - len(str(prompt).encode())), "bytes_hex": data.hex(),
            "text": data.decode("utf-8", errors="replace")}