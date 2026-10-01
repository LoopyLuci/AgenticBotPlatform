"""The Neural Lab's model description: one format every project in the toolkit converts to and from.

A spec is JSON:

    {"name": "transfer-tuner", "task": "regress",               classify | regress | lm | anomaly
     "input": {"kind": "features", "size": 12},                  or {"kind": "tokens", "vocab": 4096, "ctx": 256}
                                                                 or {"kind": "sequence", "steps": 32, "size": 6}
     "nodes": [{"id": "h", "op": "linear", "in": "input", "params": {"out": 64}}, ...],
     "output": "head",                                           the node whose output is the model's output
     "train": {"loss": "mse", "lr": 1e-3, "epochs": 20, ...},
     "origin": {"project": "brainbuilder", "path": "..."}}       where it came from (BrainBuilder, KotMoE, ...)

or, for a plain stack, "layers": [{"op": "linear", "out": 64}, {"op": "gelu"}, ...] instead of "nodes" (each layer
takes the previous one's output). "in" may be a list for add / concat. Shapes are inferred node by node (sizes
like a linear layer's input come from what feeds it), so a spec only says what it means to change.

validate(spec) -> the normalised spec with every node's resolved params and output shape, the parameter count, the
parameters a token/row actually uses (mixture-of-experts layers run only top_k experts) and a FLOPs estimate.
All of this is plain Python: ABP checks and sizes a design without PyTorch; bot/neurallab/nn_worker.py builds and
trains the same spec on the GPU.
"""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

TASKS = ("classify", "regress", "lm", "anomaly")
LOSSES = {"classify": "cross_entropy", "regress": "mse", "lm": "cross_entropy", "anomaly": "mse"}
ACTS = ("gelu", "relu", "silu", "tanh", "sigmoid", "leaky_relu", "softplus")


class SpecError(ValueError):
    """A design that can't be built, with what to change."""


# ---- the op library: parameters, shape rule, parameter count ------------------------------------------------------- #
# shape: a tuple without the batch dimension: ("F",)=(features,), (T, D) a sequence of vectors, ("tok", T) token ids.

def _last(shape) -> int:
    if shape and shape[0] == "tok":
        raise SpecError("token ids need an embedding first")
    return int(shape[-1])


def _lin(p, shape):
    i = _last(shape)
    o = int(p["out"])
    return shape[:-1] + (o,), i * o + (o if p.get("bias", True) else 0), i * o


def _mlp(p, shape):
    i = _last(shape)
    h, o = int(p["hidden"]), int(p.get("out") or i)
    return shape[:-1] + (o,), i * h + h + h * o + o, i * h + h * o


def _moe(p, shape):
    """A mixture of experts in place of an MLP: a router picks top_k of `experts` small MLPs per row/token."""
    d = _last(shape)
    e, k, h = int(p["experts"]), int(p.get("top_k", 2)), int(p["hidden"])
    o = int(p.get("out") or d)
    if not 1 <= k <= e:
        raise SpecError("moe: top_k must be between 1 and experts")
    one = d * h + h + h * o + o
    shared = one if p.get("shared_expert") else 0
    return shape[:-1] + (o,), d * e + e * one + shared, d * e + k * (d * h + h * o) + (shared and d * h + h * o)


def _attn(p, shape):
    if len(shape) != 2:
        raise SpecError("attention needs a sequence (steps x features): put it after an embedding or on sequence input")
    d = int(shape[-1])
    hds = int(p.get("heads", 4))
    if d % hds:
        raise SpecError(f"attention: {d} features don't split into {hds} heads")
    t = int(shape[0])
    return shape, 4 * d * d + 4 * d, 4 * d * d + 2 * t * d


def _block(p, shape):
    """A pre-norm transformer block: attention, then an MLP or a mixture of experts."""
    s, pa, fa = _attn(p, shape)
    d = int(shape[-1])
    ff = {"hidden": p.get("hidden", 4 * d), "out": d}
    if p.get("experts"):
        _, pm, fm = _moe({**ff, "experts": p["experts"], "top_k": p.get("top_k", 2), "shared_expert": p.get("shared_expert")}, shape)
    else:
        _, pm, fm = _mlp(ff, shape)
    return s, pa + pm + 4 * d, fa + fm


def _embed(p, shape):
    if not shape or shape[0] != "tok":
        raise SpecError("embedding needs token ids as input")
    v, d = int(p["vocab"]), int(p["dim"])
    return (shape[1], d), v * d + (int(shape[1]) * d if p.get("positions", True) else 0), d


def _norm(p, shape):
    d = _last(shape)
    return shape, 2 * d if p.get("_op") == "layernorm" else d, 4 * d


def _conv1d(p, shape):
    if len(shape) != 2:
        raise SpecError("conv1d needs a sequence (steps x channels)")
    t, c = int(shape[0]), int(shape[1])
    o, k = int(p["out"]), int(p.get("kernel", 3))
    stride = int(p.get("stride", 1))
    pad = k // 2 if p.get("same", True) else 0
    t2 = (t + 2 * pad - k) // stride + 1
    return (t2, o), c * o * k + o, t2 * c * o * k


def _gru(p, shape):
    if len(shape) != 2:
        raise SpecError("gru needs a sequence (steps x features)")
    t, i = int(shape[0]), int(shape[1])
    h = int(p["hidden"])
    out = (t, h) if p.get("sequence", False) else (h,)
    return out, 3 * (i * h + h * h + 2 * h), t * 3 * (i * h + h * h)


def _pool(p, shape):
    if len(shape) != 2:
        raise SpecError(f"{p['_op']} needs a sequence")
    return (shape[1],), 0, 0


def _flatten(p, shape):
    if shape and shape[0] == "tok":
        raise SpecError("flatten can't take token ids")
    return (int(math.prod(int(s) for s in shape)),), 0, 0


def _lm_head(p, shape):
    v = int(p["vocab"])
    tied = p.get("tied", True)
    return shape[:-1] + (v,), 0 if tied else _last(shape) * v, _last(shape) * v


def _lora(p, shape):
    i, o, r = _last(shape), int(p["out"]), int(p.get("rank", 8))
    return shape[:-1] + (o,), i * o + o + r * (i + o), i * o + r * (i + o)


def _same(p, shape):
    return shape, 0, 0


def _scale(p, shape):
    return shape, _last(shape), _last(shape)


def _markov(p, shape):
    d, r, v = _last(shape), int(p.get("rank", 32)), int(p["vocab"])
    return shape[:-1] + (v,), d * r + r * v + v, d * r + r * v


def _confidence(p, shape):
    d, h = _last(shape), int(p.get("proj", 128))
    return shape[:-1] + (1,), d * h + h + h + 1, d * h + h


OPS: dict[str, dict[str, Any]] = {
    "linear": {"rule": _lin, "need": ["out"], "doc": "y = xW + b"},
    "lora_linear": {"rule": _lora, "need": ["out"], "doc": "a linear layer with a trainable low-rank update (rank)"},
    "mlp": {"rule": _mlp, "need": ["hidden"], "doc": "linear -> activation -> linear"},
    "moe": {"rule": _moe, "need": ["experts", "hidden"], "doc": "mixture of experts: router picks top_k expert MLPs"},
    "attention": {"rule": _attn, "need": [], "doc": "multi-head self-attention (causal when causal=true)"},
    "transformer_block": {"rule": _block, "need": [], "doc": "pre-norm attention + MLP (or MoE when experts>0)"},
    "embedding": {"rule": _embed, "need": ["vocab", "dim"], "doc": "token ids -> vectors (+ learned positions)"},
    "layernorm": {"rule": _norm, "need": [], "doc": "layer normalisation"},
    "rmsnorm": {"rule": _norm, "need": [], "doc": "RMS normalisation"},
    "conv1d": {"rule": _conv1d, "need": ["out"], "doc": "1-D convolution over steps"},
    "gru": {"rule": _gru, "need": ["hidden"], "doc": "a GRU over steps (last state, or every step with sequence=true)"},
    "select_last": {"rule": _pool, "need": [], "doc": "the last step of a sequence"},
    "mean_pool": {"rule": _pool, "need": [], "doc": "the mean over steps"},
    "flatten": {"rule": _flatten, "need": [], "doc": "everything into one vector"},
    "dropout": {"rule": _same, "need": [], "doc": "dropout (p)"},
    "scale": {"rule": _scale, "need": [], "doc": "a learned per-feature scale"},
    "lm_head": {"rule": _lm_head, "need": ["vocab"], "doc": "vectors -> next-token logits (tied to the embedding)"},
    "low_rank_markov_head": {"rule": _markov, "need": ["vocab"], "doc": "BrainBuilder DSpark: low-rank next-token head"},
    "confidence_head": {"rule": _confidence, "need": [], "doc": "BrainBuilder DSpark: how sure a draft is (0..1)"},
    "add": {"rule": None, "need": [], "doc": "sum of inputs (a residual connection)"},
    "concat": {"rule": None, "need": [], "doc": "inputs side by side"},
    **{a: {"rule": _same, "need": [], "doc": f"{a} activation"} for a in ACTS},
}


# ---- normalising and checking ------------------------------------------------------------------------------------- #

def _input_shape(inp: dict) -> tuple:
    kind = inp.get("kind", "features")
    if kind == "features":
        return (int(inp["size"]),)
    if kind == "tokens":
        return ("tok", int(inp["ctx"]))
    if kind == "sequence":
        return (int(inp["steps"]), int(inp["size"]))
    raise SpecError(f"input kind {kind!r}: use features, tokens or sequence")


def _flat_params(node: dict) -> dict:
    p = dict(node.get("params") or {})
    for k, v in node.items():
        if k not in ("id", "op", "in", "params", "label"):
            p.setdefault(k, v)
    return p


def normalise(spec: dict) -> dict:
    s = copy.deepcopy(spec)
    if "layers" in s and "nodes" not in s:
        nodes, prev = [], "input"
        for i, layer in enumerate(s.pop("layers")):
            nid = layer.get("id") or f"{layer['op']}{i}"
            nodes.append({"id": nid, "op": layer["op"], "in": layer.get("in", prev), "params": _flat_params(layer)})
            prev = nid
        s["nodes"] = nodes
        s.setdefault("output", prev)
    s.setdefault("task", "classify")
    if s["task"] not in TASKS:
        raise SpecError(f"task {s['task']!r}: use one of {', '.join(TASKS)}")
    s.setdefault("name", "model")
    s.setdefault("train", {})
    s["train"].setdefault("loss", LOSSES[s["task"]])
    for n in s.get("nodes", []):
        n["params"] = _flat_params(n)
        for k in list(n):
            if k not in ("id", "op", "in", "params", "label"):
                del n[k]
    return s


def validate(spec: dict) -> dict:
    """The spec checked end to end: every node's shape, the parameter count, active parameters and FLOPs per row."""
    s = normalise(spec)
    if not s.get("nodes"):
        raise SpecError("a model needs at least one layer")
    shapes = {"input": _input_shape(s.get("input") or {})}
    total = active = flops = 0
    seen = {"input"}
    for n in s["nodes"]:
        nid, op = n.get("id"), n.get("op")
        if not nid or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", nid):
            raise SpecError(f"node id {nid!r}: letters, digits and _ only")
        if nid in seen:
            raise SpecError(f"node id {nid!r} is used twice")
        if op not in OPS:
            raise SpecError(f"{nid}: unknown op {op!r} (known: {', '.join(sorted(OPS))})")
        ins = n.get("in", "input")
        ins = [ins] if isinstance(ins, str) else list(ins)
        for i in ins:
            if i not in shapes:
                raise SpecError(f"{nid}: input {i!r} is not an earlier node")
        p = n["params"]
        for need in OPS[op]["need"]:
            if need not in p:
                raise SpecError(f"{nid} ({op}) needs {need}")
        if op in ("add", "concat"):
            sh = [shapes[i] for i in ins]
            if len(ins) < 2:
                raise SpecError(f"{nid}: {op} takes two or more inputs")
            if op == "add":
                if any(x != sh[0] for x in sh):
                    raise SpecError(f"{nid}: add needs equal shapes, got {sh}")
                out, np_, fl = sh[0], 0, 0
            else:
                if any(x[:-1] != sh[0][:-1] for x in sh):
                    raise SpecError(f"{nid}: concat needs equal leading shapes, got {sh}")
                out, np_, fl = sh[0][:-1] + (sum(int(x[-1]) for x in sh),), 0, 0
        else:
            if len(ins) != 1:
                raise SpecError(f"{nid}: {op} takes one input")
            out, np_, fl = OPS[op]["rule"]({**p, "_op": op}, shapes[ins[0]])
            act = fl
            if op == "moe" or (op == "transformer_block" and p.get("experts")):
                e, k = int(p["experts"]), int(p.get("top_k", 2))
                d = _last(shapes[ins[0]])
                h = int(p.get("hidden", 4 * d))
                o = int(p.get("out") or d)
                act = np_ - (e - k) * (d * h + h + h * o + o)
            else:
                act = np_
            active += act
        total += np_
        rows = int(out[0]) if len(out) == 2 and out[0] != "tok" else 1
        flops += 2 * fl * (rows if op in ("linear", "mlp", "moe", "lora_linear", "lm_head", "scale") else 1)
        n["in"] = ins if len(ins) > 1 else ins[0]
        n["shape"] = list(out)
        shapes[nid] = out
        seen.add(nid)
    outn = s.get("output") or s["nodes"][-1]["id"]
    if outn not in shapes:
        raise SpecError(f"output {outn!r} is not a node")
    s["output"] = outn
    oshape = shapes[outn]
    if s["task"] == "lm" and s["input"].get("kind") != "tokens":
        raise SpecError("a language model takes token input")
    if s["task"] == "classify" and not s.get("classes"):
        s["classes"] = int(oshape[-1])
    for n in s["nodes"]:
        if n["op"] == "embedding" and s["input"].get("kind") == "tokens" and int(n["params"]["vocab"]) < int(s["input"]["vocab"]):
            raise SpecError(f"{n['id']}: the embedding has {n['params']['vocab']} rows but the input vocabulary is "
                            f"{s['input']['vocab']} (token ids past the table would crash the run)")
    tied = [n for n in s["nodes"] if n["op"] == "lm_head" and n["params"].get("tied", True)]
    if tied and not any(n["op"] == "embedding" for n in s["nodes"]):
        raise SpecError("a tied lm_head needs an embedding to tie to")
    s["stats"] = {"params": total, "active_params": active, "flops_per_row": flops, "output_shape": list(oshape),
                  "memory_mb_fp32": round(total * 4 * 4 / 2**20, 1)}       # weights + grads + 2 Adam moments
    return s


def describe(spec: dict) -> str:
    s = validate(spec)
    lines = [f"{s['name']} ({s['task']}): input {s['input']}"]
    for n in s["nodes"]:
        p = {k: v for k, v in n["params"].items() if not k.startswith("_")}
        lines.append(f"  {n['id']:<16} {n['op']:<18} <- {n['in']!s:<18} {tuple(n['shape'])}  {p if p else ''}")
    st = s["stats"]
    lines.append(f"  parameters {st['params']:,} (active per row {st['active_params']:,}), "
                 f"~{st['flops_per_row']:,} FLOPs per row, ~{st['memory_mb_fp32']} MB to train in fp32")
    return "\n".join(lines)


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(spec: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(spec, indent=1), encoding="utf-8")
    return p


# ---- building blocks for common designs ---------------------------------------------------------------------------- #

def moe_regressor(name: str, features: int, outputs: int = 1, dim: int = 64, experts: int = 8, top_k: int = 2,
                  hidden: int = 128, task: str = "regress") -> dict:
    """The shape the system models use: embed the features, a residual mixture-of-experts, a small head. Small and
    fast enough to run on the CPU at inference, specialised per expert (each learns a regime: a disk type, a load
    level...), trained on the GPU."""
    return {"name": name, "task": task, "input": {"kind": "features", "size": features},
            "nodes": [{"id": "inp", "op": "linear", "in": "input", "params": {"out": dim}},
                      {"id": "act", "op": "gelu", "in": "inp"},
                      {"id": "norm", "op": "layernorm", "in": "act"},
                      {"id": "experts", "op": "moe", "in": "norm", "params": {"experts": experts, "top_k": top_k, "hidden": hidden}},
                      {"id": "res", "op": "add", "in": ["act", "experts"]},
                      {"id": "norm2", "op": "layernorm", "in": "res"},
                      {"id": "head", "op": "linear", "in": "norm2", "params": {"out": outputs}}],
            "output": "head", "train": {"lr": 2e-3, "epochs": 60, "batch_size": 256, "weight_decay": 0.01}}


def moe_lm(name: str, vocab: int, ctx: int, dim: int = 256, layers: int = 4, heads: int = 4, experts: int = 8,
           top_k: int = 2, hidden: int = 512) -> dict:
    """A decoder-only mixture-of-experts language model (the KotMoE-gen design)."""
    nodes = [{"id": "emb", "op": "embedding", "in": "input", "params": {"vocab": vocab, "dim": dim}}]
    prev = "emb"
    for i in range(layers):
        nodes.append({"id": f"h{i}", "op": "transformer_block", "in": prev,
                      "params": {"heads": heads, "hidden": hidden, "experts": experts, "top_k": top_k, "causal": True}})
        prev = f"h{i}"
    nodes += [{"id": "lnf", "op": "layernorm", "in": prev}, {"id": "head", "op": "lm_head", "in": "lnf", "params": {"vocab": vocab}}]
    return {"name": name, "task": "lm", "input": {"kind": "tokens", "vocab": vocab, "ctx": ctx}, "nodes": nodes,
            "output": "head", "train": {"lr": 3e-4, "epochs": 1, "batch_size": 16, "weight_decay": 0.1}}


def ops_catalog() -> list[dict]:
    return [{"op": k, "needs": v["need"], "doc": v["doc"]} for k, v in sorted(OPS.items())]
