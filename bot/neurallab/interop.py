"""The toolkit's projects, read and written in their own formats so work moves between them and the Neural Lab:

    BrainBuilder  graphs (*.bbir.edn: nodes of components, edges between ports, a training block) <-> specs; its
                  examples and saved graphs listed; a lab design written back as a graph BrainBuilder opens
    KotMoE        the auto-training registry's MoE classifier designs -> specs; kotmoe-gen checkpoints (model.kmoe:
                  the generative MoE transformer) -> a spec plus the weights, so training continues on the GPU here and
                  the result goes back as model.kmoe; its MNIST data (IDX files) -> a lab dataset
    Amethyst      a fine-tuned LoRA (bot/localai/train.py) -> a module package (.apkg: manifest.json, weights/, templates/,
                  tests/) and installed into Amethyst with its own CLI; its installed modules listed
    Kestrion      shares ABP's model store (KESTRION_MODEL_DIRS, set when ABP starts it) and ABP reads ~/.kestrion/models
                  (bot/localai/discover.py)

Projects are found at ABP_PROJECTS_DIR (default: the folder holding ABP's checkout), each by its usual folder name.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

from bot.localai.paths import LocalAIError
from bot.neurallab import edn
from bot.neurallab import spec as specs

PROJECTS = {"brainbuilder": "BrainBuilder", "kotmoe": "KotMoE", "amethyst": "AmethystModularScalableModelArchitechture",
            "kestrion": "Kestrion"}
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def project_dir(key: str) -> Optional[Path]:
    from bot.envfile import PROJECT_ROOT
    base = Path(os.environ.get("ABP_PROJECTS_DIR") or PROJECT_ROOT.parent)
    p = base / PROJECTS[key]
    return p if p.is_dir() else None


def projects() -> list[dict]:
    out = []
    for key, folder in PROJECTS.items():
        p = project_dir(key)
        out.append({"id": key, "folder": folder, "path": str(p) if p else None, "present": bool(p)})
    return out


# ---- BrainBuilder ------------------------------------------------------------------------------------------------- #

_BB_TO_OP = {"linear": "linear", "gelu": "gelu", "relu": "relu", "dropout": "dropout", "layernorm": "layernorm",
             "embedding": "embedding", "attention": "attention", "select_last": "select_last", "add": "add",
             "scale": "scale", "lora_linear": "lora_linear", "low_rank_markov_head": "low_rank_markov_head",
             "confidence_head": "confidence_head"}
_OP_TO_BB = {v: k for k, v in _BB_TO_OP.items()}


def _bb_params(comp: str, hp: dict) -> dict:
    hp = {str(k): v for k, v in (hp or {}).items()}
    if comp in ("linear", "lora_linear"):
        out = {"out": hp.get("out_features", hp.get("out", 1))}
        if comp == "lora_linear":
            out["rank"] = hp.get("rank", 8)
        return out
    if comp == "embedding":
        return {"vocab": hp.get("vocab_size", hp.get("vocab", 1000)), "dim": hp.get("embedding_dim", hp.get("dim", 64)),
                "positions": False}
    if comp == "attention":
        return {"heads": hp.get("num_heads", 4), "causal": False}
    if comp == "dropout":
        return {"p": hp.get("p", 0.1)}
    if comp == "low_rank_markov_head":
        return {"rank": hp.get("rank", 32), "vocab": hp.get("vocab", 1000)}
    if comp == "confidence_head":
        return {"proj": hp.get("proj", 128)}
    return {}


def from_bbir(path: str | Path) -> dict:
    """A BrainBuilder graph file -> a spec (BrainBuilder's training block becomes the spec's train settings)."""
    g = edn.loads(Path(path).read_text(encoding="utf-8"))
    nodes, edges = g.get("nodes") or [], g.get("edges") or []
    incoming: dict[str, list[str]] = {}
    for e in edges:
        incoming.setdefault(e["to-node"], []).append(e["from-node"])
    tr = g.get("training") or {}
    ds = tr.get("data_source") or {}
    hp = tr.get("hyperparams") or {}
    first = next((n for n in nodes if not incoming.get(n["id"])), None)
    if first is None:
        raise LocalAIError(f"{path}: the graph has no starting node")
    if ds.get("source-type") == "text_sequence" or first["component"] == "embedding":
        emb = _bb_params("embedding", first.get("hyperparams"))
        # the embedding's size is what the graph can take (BrainBuilder's data block may list a larger vocabulary)
        inp = {"kind": "tokens", "vocab": int(emb["vocab"]), "ctx": int(ds.get("sequence-length") or 16)}
        task = "classify"                         # BrainBuilder's next-word graphs: the last word -> the next one
    else:
        size = (first.get("hyperparams") or {}).get("in_features") or 1
        inp = {"kind": "features", "size": int(size)}
        task = "regress" if tr.get("loss") in ("mse", "l1", "huber") else "classify"
    unsupported = sorted({n["component"] for n in nodes if n["component"] not in _BB_TO_OP})
    if unsupported:
        raise LocalAIError(f"{path}: the lab can't build {', '.join(unsupported)} yet")
    order, done = [], set()
    pending = list(nodes)
    while pending:
        progressed = False
        for n in list(pending):
            if all(i in done for i in incoming.get(n["id"], [])):
                order.append(n)
                done.add(n["id"])
                pending.remove(n)
                progressed = True
        if not progressed:
            raise LocalAIError(f"{path}: the graph has a cycle")
    out_nodes = []
    for n in order:
        ins = incoming.get(n["id"]) or ["input"]
        out_nodes.append({"id": _ident(n["id"]), "op": _BB_TO_OP[n["component"]], "in": [_ident(i) if i != "input" else i for i in ins]
                          if len(ins) > 1 else (_ident(ins[0]) if ins[0] != "input" else "input"),
                          "params": _bb_params(n["component"], n.get("hyperparams")), "label": n.get("label", "")})
    sinks = [n["id"] for n in out_nodes if not any((n["id"] == m["in"] or n["id"] in (m["in"] if isinstance(m["in"], list) else []))
                                                   for m in out_nodes)]
    return {"name": g.get("name") or Path(path).stem.split(".")[0], "task": task, "input": inp, "nodes": out_nodes,
            "output": sinks[-1], "train": {"lr": hp.get("lr", 1e-3), "epochs": hp.get("epochs", 20),
                                           "batch_size": ds.get("batch-size") or 256, "optimizer": tr.get("optimizer", "adam")},
            "origin": {"project": "brainbuilder", "path": str(path), "graph_id": g.get("graph-id"),
                       "data": ds.get("path-or-uri"), "data_kind": ds.get("source-type")}}


def _ident(s: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s if s[:1].isalpha() or s[:1] == "_" else f"n_{s}"


def to_bbir(spec: dict, path: str | Path) -> Path:
    """A spec -> a BrainBuilder graph file (the ops BrainBuilder has components for)."""
    s = specs.validate(spec)
    nodes, edges = [], []
    prev_dim = {"input": s["input"].get("size")}
    for i, n in enumerate(s["nodes"]):
        comp = _OP_TO_BB.get(n["op"])
        if not comp:
            raise LocalAIError(f"BrainBuilder has no component for {n['op']} (node {n['id']})")
        p, hp = n["params"], {}
        ins = n["in"] if isinstance(n["in"], list) else [n["in"]]
        d_in = prev_dim.get(ins[0])
        if comp in ("linear", "lora_linear"):
            hp = {"in_features": d_in, "out_features": p["out"], **({"rank": p.get("rank", 8)} if comp == "lora_linear" else {})}
        elif comp == "embedding":
            hp = {"vocab_size": p["vocab"], "embedding_dim": p["dim"]}
        elif comp == "attention":
            hp = {"features": d_in, "num_heads": p.get("heads", 4)}
        elif comp == "layernorm":
            hp = {"features": d_in}
        elif comp == "dropout":
            hp = {"p": p.get("p", 0.1)}
        elif comp in ("low_rank_markov_head", "confidence_head"):
            hp = {k: v for k, v in p.items() if not k.startswith("_")}
        prev_dim[n["id"]] = n["shape"][-1]
        nodes.append({"id": n["id"], "component": comp, "label": n.get("label") or n["id"], "hyperparams": hp,
                      "position": {"x": 80 + 240 * i, "y": 150}})
        for j, src in enumerate(ins):
            if src != "input":
                edges.append({"from-node": src, "from-port": "output", "to-node": n["id"],
                              "to-port": ("a", "b")[j] if comp == "add" else "input"})
    tr = s.get("train", {})
    graph = {"schema-version": 1, "graph-id": hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()[:32],
             "name": s["name"], "nodes": nodes, "edges": edges,
             "training": {"loss": tr.get("loss", "cross_entropy"), "optimizer": "adam", "trainer_type": "standard",
                          "hyperparams": {"epochs": int(tr.get("epochs", 20)), "lr": float(tr.get("lr", 1e-3))},
                          "data_source": None, "reproducibility": None}}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(edn.dumps(graph), encoding="utf-8")
    return p


def brainbuilder_graphs() -> list[dict]:
    root = project_dir("brainbuilder")
    if not root:
        return []
    out = []
    for f in sorted(list((root / "gui" / "examples").glob("*.bbir.edn")) + list((root / "models" / "custom").rglob("*.bbir.edn"))):
        try:
            s = specs.validate(from_bbir(f))
            out.append({"path": str(f), "name": s["name"], "task": s["task"], "params": s["stats"]["params"]})
        except (LocalAIError, specs.SpecError, edn.EDNError, KeyError, ValueError) as e:
            out.append({"path": str(f), "error": str(e)})
    return out


# ---- KotMoE ------------------------------------------------------------------------------------------------------- #

_REG_COLS = ["id", "experts", "hidden", "top_k", "capacity", "batch", "accum", "epochs", "lr", "weight_decay", "aux",
             "dropout", "best_acc", "last_acc", "epoch", "expanded", "model_path", "inference_path", "status"]


def kotmoe_registry() -> list[dict]:
    root = project_dir("kotmoe")
    f = root / "auto_training_registry.json" if root else None
    if not f or not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 13:
            out.append(dict(zip(_REG_COLS, parts)))
    return out


def from_kotmoe_registry(row: dict) -> dict:
    """KotMoE's MoE classifier (784 inputs -> dense -> experts -> 10 classes) as a lab design (same architecture and
    hyperparameters; KotMoE's .bin weights are its own Java serialisation and stay in KotMoE)."""
    h, e, k = int(row["hidden"]), int(row["experts"]), int(row["top_k"])
    return {"name": f"kotmoe-{row['id']}", "task": "classify", "input": {"kind": "features", "size": 784},
            "nodes": [{"id": "dense", "op": "linear", "in": "input", "params": {"out": h}},
                      {"id": "act", "op": "relu", "in": "dense"},
                      {"id": "drop", "op": "dropout", "in": "act", "params": {"p": float(row.get("dropout") or 0)}},
                      {"id": "experts", "op": "moe", "in": "drop", "params": {"experts": e, "top_k": k, "hidden": h,
                                                                               "aux": float(row.get("aux") or 0.01)}},
                      {"id": "out", "op": "linear", "in": "experts", "params": {"out": 10}}],
            "output": "out", "train": {"lr": float(row["lr"]), "weight_decay": float(row["weight_decay"]),
                                       "epochs": int(row["epochs"]), "batch_size": int(row["batch"])},
            "origin": {"project": "kotmoe", "registry_id": row["id"], "kotmoe_best_accuracy": float(row.get("best_acc") or 0)}}


def kmoe_header(path: str | Path) -> dict:
    with open(path, "rb") as f:
        head = f.read(48)
    if len(head) < 48 or struct.unpack(">ii", head[:8]) != (0x4B4D4F45, 1):
        raise LocalAIError(f"{path} is not a KotMoE-gen checkpoint")
    v = struct.unpack(">8i", head[8:40])
    aux, step = struct.unpack(">fi", head[40:48])
    return dict(zip(["vocab", "ctx", "dim", "layers", "heads", "experts", "top_k", "hidden"], v), aux=aux, step=step)


def from_kmoe(path: str | Path) -> dict:
    """A kotmoe-gen checkpoint -> its design (weights load at training time with init_kmoe)."""
    h = kmoe_header(path)
    s = specs.moe_lm(f"kotmoe-gen-{Path(path).parent.name}", h["vocab"], h["ctx"], h["dim"], h["layers"], h["heads"],
                     h["experts"], h["top_k"], h["hidden"])
    for n in s["nodes"]:
        if n["op"] == "transformer_block":
            n["params"]["aux"] = h["aux"]
    s["origin"] = {"project": "kotmoe", "design": "kotmoe-gen", "checkpoint": str(path), "step": h["step"]}
    return s


def kotmoe_checkpoints() -> list[dict]:
    out = []
    roots = [p for p in (project_dir("kotmoe"), Path("E:/kotmoe-gen")) if p and p.exists()]
    for r in roots:
        for f in r.rglob("model.kmoe"):
            try:
                out.append({"path": str(f), **kmoe_header(f)})
            except (LocalAIError, OSError):
                continue
    return out


def idx_to_npz(images: str | Path, labels: str | Path, out: str | Path, limit: int = 0) -> dict:
    """MNIST-style IDX files (KotMoE's mnist_data) -> a lab dataset (X: pixels 0..1, y: the digit)."""
    import numpy as np
    def read(p):
        data = Path(p).read_bytes()
        magic = struct.unpack(">I", data[:4])[0]
        nd = magic & 0xFF
        dims = struct.unpack(">" + "I" * nd, data[4:4 + 4 * nd])
        return np.frombuffer(data, dtype=np.uint8, offset=4 + 4 * nd).reshape(dims)
    X = read(images).astype("float32") / 255.0
    y = read(labels).astype("int64")
    if limit:
        X, y = X[:limit], y[:limit]
    X = X.reshape(len(X), -1)
    np.savez(out, X=X, y=y)
    return {"path": str(out), "rows": int(len(X)), "features": int(X.shape[1])}


# ---- Amethyst ----------------------------------------------------------------------------------------------------- #

def _amethyst_python() -> Optional[str]:
    root = project_dir("amethyst")
    if not root:
        return None
    for v in (".venv", "venv"):
        p = root / v / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if p.exists():
            return str(p)
    # else the training environment, where Amethyst is installed beside the GPU build of PyTorch (`abp lab setup`)
    from bot.localai import train
    py = train.python()
    if py.exists():
        r = subprocess.run([str(py), "-c", "import amythest"], capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            return str(py)
    return None


def amethyst_setup(log=lambda m: None) -> dict:
    """Install Amethyst (editable, from its checkout) into the training environment: its modules then load on the GPU."""
    from bot.localai import train
    root = project_dir("amethyst")
    if not root:
        raise LocalAIError("Amethyst's checkout is not next to ABP's (set ABP_PROJECTS_DIR)")
    if not train.python().exists():
        raise LocalAIError("set up the training environment first (`abp ai train setup`)")
    train._run([str(train.python()), "-m", "pip", "install", "-e", str(root)], log)
    return {"python": _amethyst_python(), "project": str(root)}


def apkg_from_run(run_id: str, name: str, version: str = "1.0.0", module_type: str = "skill", description: str = "",
                  author: str = "ABP", out: Optional[Path] = None) -> dict:
    """A finished fine-tuning run's LoRA -> an Amethyst module package. Holds the adapter as PEFT saves it (Amethyst
    loads it with PeftModel), a few training examples as the module's benchmark, and the system prompt if any."""
    from bot.localai import train
    st = train.status(run_id)
    run = Path(st["dir"])
    ad = run / "adapter"
    if not (ad / "adapter_config.json").exists():
        raise LocalAIError(f"run {run_id} has no adapter (state: {st.get('state')})")
    job = json.loads((run / "job.json").read_text(encoding="utf-8"))
    cfg = json.loads((ad / "adapter_config.json").read_text(encoding="utf-8"))
    weights = next((ad / n for n in ("adapter_model.safetensors", "adapter_model.bin") if (ad / n).exists()), None)
    if not weights:
        raise LocalAIError(f"run {run_id}'s adapter has no weights file")
    base_cfg = {}
    try:
        base_cfg = json.loads((Path(job["base"]) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    tests = []
    for d in job.get("data", [])[:1]:
        for line in Path(d).read_text(encoding="utf-8").splitlines()[:20]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msgs = row.get("messages") or []
            q = next((m["content"] for m in msgs if m.get("role") == "user"), row.get("instruction") or row.get("prompt"))
            a = next((m["content"] for m in msgs if m.get("role") == "assistant"), row.get("output") or row.get("chosen"))
            if q and a:
                tests.append({"prompt": q, "expected": a})
    manifest = {"name": name, "version": version, "author": author,
                "description": description or f"Fine-tuned in ABP from {job.get('base_name')} ({job.get('method')}, run {run_id})",
                "type": module_type,
                "base_model": {"name": job.get("base_name", ""), "version": "main",
                               "architecture": (base_cfg.get("architectures") or [""])[0]},
                "dependencies": [], "injection_ports": [0, 4, 8, 12],
                "size_mb": round(weights.stat().st_size / 2**20, 2), "tags": ["abp", "lora", job.get("method", "sft")],
                "benchmark_score": None, "sha256": None}
    body = json.dumps(manifest, indent=2)
    manifest["sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()     # as Amethyst's writer computes it
    out = out or run / "export" / f"{name}-{version}.apkg"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.write(weights, f"weights/{weights.name}")
        z.writestr("weights/adapter_config.json", json.dumps(cfg))
        if tests:
            z.writestr("tests/benchmark.jsonl", "\n".join(json.dumps(t, ensure_ascii=False) for t in tests))
    return {"path": str(out), "manifest": manifest, "size": out.stat().st_size}


def amethyst(*args: str, timeout: int = 600) -> str:
    py = _amethyst_python()
    if not py:
        raise LocalAIError("Amethyst is not set up (its checkout, with .venv: build the module in ABP's Modules page)")
    r = subprocess.run([py, "-m", "amythest", *args], capture_output=True, text=True, timeout=timeout,
                       cwd=project_dir("amethyst"), creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    if r.returncode:
        raise LocalAIError(f"amythest {' '.join(args)} failed: {(r.stderr or r.stdout)[-800:]}")
    return r.stdout


def amethyst_install(apkg: str) -> str:
    return amethyst("install", apkg)


def amethyst_modules() -> str:
    return amethyst("list")


# ---- Kestrion ----------------------------------------------------------------------------------------------------- #

def kestrion_status() -> dict:
    """Whether Kestrion's inference server is up and which of its models come from ABP's store."""
    import httpx
    from bot.localai import models
    try:
        r = httpx.get("http://127.0.0.1:11435/api/tags", timeout=3)
        tags = r.json().get("models", []) if r.status_code == 200 else []
        up = r.status_code == 200
    except (httpx.HTTPError, ValueError):
        tags, up = [], False
    store = str(models.store_root())
    return {"up": up, "models": len(tags), "store_shared": store, "env": {"KESTRION_MODEL_DIRS": store},
            "note": "ABP starts Kestrion with KESTRION_MODEL_DIRS set to its store, so Kestrion runs ABP's models in place"}
