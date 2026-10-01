"""The Neural Lab's GPU worker: builds a spec (bot/neurallab/spec.py) as a PyTorch model, trains it, evaluates it and
exports it. Runs in the training environment (<localai home>/venv-train), never in ABP's own process:

    python nn_worker.py <run dir>          reads <run>/job.json; progress as JSON lines and in <run>/status.json

Data (job["data"]):
    {"path": "x.csv" | "x.jsonl" | "x.npz", "target": "col", "features": [...]}     features / regress / classify
    {"path": "x.npz"}                     arrays X (N,F) or (N,T,F) and y; anomaly needs X only
    {"path": "corpus.txt", "tokenizer": "byte" | "<kotmoe .bpe file>" | "<HF tokenizer folder>"}   language models
Exports (in <run>/export/): weights.safetensors, weights.npz (for ABP's numpy inference of small models), model.json
(the spec, input normalisation, labels, metrics), and model.kmoe when the design is KotMoE-gen (KotMoE's own checkpoint
format, loadable by KotMoE's Kotlin runtime).

Standalone (no ABP imports). CPU threads capped; a GPU is required unless the job says allow_cpu.
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import struct
import sys
import time
from pathlib import Path

RUN = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
STATUS = RUN / "status.json"
STOP = RUN / "stop"


def report(**kv) -> None:
    kv["time"] = time.time()
    try:
        cur = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    cur.update(kv)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, default=float), encoding="utf-8")
    os.replace(tmp, STATUS)
    print(json.dumps(kv, default=float), flush=True)


import torch                                   # noqa: E402  (after report: import errors still reach the status)
import torch.nn as nn                          # noqa: E402
import torch.nn.functional as F                # noqa: E402


# ---- layers -------------------------------------------------------------------------------------------------------- #

def act_fn(name: str):
    return {"gelu": lambda x: F.gelu(x, approximate="tanh"), "relu": F.relu, "silu": F.silu, "tanh": torch.tanh,
            "sigmoid": torch.sigmoid, "leaky_relu": F.leaky_relu, "softplus": F.softplus}[name]


class MLP(nn.Module):
    def __init__(self, d, h, o, act="gelu"):
        super().__init__()
        self.fc, self.proj, self.act = nn.Linear(d, h), nn.Linear(h, o), act_fn(act)

    def forward(self, x):
        return self.proj(self.act(self.fc(x)))


class MoE(nn.Module):
    """Top-k routing over expert MLPs, gates renormalised over the chosen experts, and the Switch/GShard balance loss
    (aux = coef * E * sum_e f_e * P_e, f = share of rows whose first choice is e, P = mean router probability) — the
    same maths as KotMoE's Kotlin model, so weights move between them."""

    def __init__(self, d, h, experts, top_k=2, out=None, aux=0.01, shared_expert=False, act="gelu", router_bias=False):
        super().__init__()
        o = out or d
        self.e, self.k, self.aux_coef = experts, top_k, aux
        self.router = nn.Linear(d, experts, bias=router_bias)
        self.experts = nn.ModuleList(MLP(d, h, o, act) for _ in range(experts))
        self.shared = MLP(d, h, o, act) if shared_expert else None
        self.aux = torch.zeros(())
        self.load = torch.zeros(experts)

    def forward(self, x):
        shp = x.shape
        flat = x.reshape(-1, shp[-1])
        probs = torch.softmax(self.router(flat).float(), -1)
        topv, topi = probs.topk(self.k, -1)
        gates = (topv / topv.sum(-1, keepdim=True)).to(x.dtype)
        out = None
        for e, expert in enumerate(self.experts):
            rows, slot = (topi == e).nonzero(as_tuple=True)
            if rows.numel() == 0:
                continue
            y = expert(flat[rows]) * gates[rows, slot].unsqueeze(-1)
            if out is None:
                out = torch.zeros(flat.shape[0], y.shape[-1], dtype=y.dtype, device=y.device)
            out.index_add_(0, rows, y)
        if out is None:
            out = torch.zeros(flat.shape[0], self.experts[0].proj.out_features, dtype=x.dtype, device=x.device)
        if self.shared is not None:
            out = out + self.shared(flat)
        f = torch.bincount(topi[:, 0], minlength=self.e).float() / flat.shape[0]
        self.aux = self.aux_coef * self.e * (f * probs.mean(0)).sum()
        self.load = f.detach()
        return out.reshape(*shp[:-1], out.shape[-1])


class Attention(nn.Module):
    def __init__(self, d, heads, causal=True):
        super().__init__()
        self.h, self.causal = heads, causal
        self.qkv, self.proj = nn.Linear(d, 3 * d), nn.Linear(d, d)

    def forward(self, x):
        b, t, d = x.shape
        q, k, v = self.qkv(x).split(d, -1)
        sh = lambda z: z.view(b, t, self.h, d // self.h).transpose(1, 2)
        y = F.scaled_dot_product_attention(sh(q), sh(k), sh(v), is_causal=self.causal)
        return self.proj(y.transpose(1, 2).reshape(b, t, d))


class Block(nn.Module):
    def __init__(self, d, heads, hidden, experts=0, top_k=2, causal=True, aux=0.01, shared_expert=False):
        super().__init__()
        self.ln1, self.attn, self.ln2 = nn.LayerNorm(d, eps=1e-5), Attention(d, heads, causal), nn.LayerNorm(d, eps=1e-5)
        self.ff = MoE(d, hidden, experts, top_k, d, aux, shared_expert) if experts else MLP(d, hidden, d)

    def forward(self, x):
        x = x + self.attn(self.ln1(x))
        return x + self.ff(self.ln2(x))


class Embed(nn.Module):
    def __init__(self, vocab, dim, ctx, positions=True):
        super().__init__()
        self.wte = nn.Embedding(vocab, dim)
        self.wpe = nn.Embedding(ctx, dim) if positions else None

    def forward(self, ids):
        x = self.wte(ids)
        if self.wpe is not None:
            x = x + self.wpe(torch.arange(ids.shape[1], device=ids.device))
        return x


class LoRALinear(nn.Module):
    def __init__(self, i, o, rank=8, alpha=None):
        super().__init__()
        self.base = nn.Linear(i, o)
        self.a = nn.Parameter(torch.randn(rank, i) / math.sqrt(i))
        self.b = nn.Parameter(torch.zeros(o, rank))
        self.s = (alpha or rank) / rank

    def forward(self, x):
        return self.base(x) + (x @ self.a.t() @ self.b.t()) * self.s


class GRU(nn.Module):
    def __init__(self, i, h, sequence=False):
        super().__init__()
        self.rnn, self.seq = nn.GRU(i, h, batch_first=True), sequence

    def forward(self, x):
        y, hN = self.rnn(x)
        return y if self.seq else hN[-1]


class Conv1d(nn.Module):
    def __init__(self, c, o, k=3, stride=1, same=True):
        super().__init__()
        self.conv = nn.Conv1d(c, o, k, stride, padding=k // 2 if same else 0)

    def forward(self, x):
        return self.conv(x.transpose(1, 2)).transpose(1, 2)


class RMSNorm(nn.Module):
    def __init__(self, d, eps=1e-6):
        super().__init__()
        self.w, self.eps = nn.Parameter(torch.ones(d)), eps

    def forward(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps) * self.w


class Fn(nn.Module):
    def __init__(self, f):
        super().__init__()
        self.f = f

    def forward(self, x):
        return self.f(x)


class Scale(nn.Module):
    def __init__(self, d):
        super().__init__()
        self.w = nn.Parameter(torch.ones(d))

    def forward(self, x):
        return x * self.w


class LMHead(nn.Module):
    def __init__(self, d, vocab, tied_to=None):
        super().__init__()
        self.tied = tied_to
        self.w = None if tied_to is not None else nn.Linear(d, vocab, bias=False)

    def forward(self, x):
        return x @ self.tied.weight.t() if self.tied is not None else self.w(x)


class Markov(nn.Module):
    def __init__(self, d, rank, vocab):
        super().__init__()
        self.down, self.up = nn.Linear(d, rank, bias=False), nn.Linear(rank, vocab)

    def forward(self, x):
        return self.up(self.down(x))


class Graph(nn.Module):
    """The spec's nodes, run in order; each reads its inputs from earlier outputs."""

    def __init__(self, spec: dict):
        super().__init__()
        self.spec = spec
        self.order, self.inputs = [], {}
        self.mods = nn.ModuleDict()
        shapes = {"input": _in_shape(spec["input"])}
        embed = None
        for n in spec["nodes"]:
            nid, op, p = n["id"], n["op"], n["params"]
            ins = [n["in"]] if isinstance(n["in"], str) else n["in"]
            sh = shapes[ins[0]]
            d = int(sh[-1]) if sh and sh[0] != "tok" else 0
            m = None
            if op == "linear":
                m = nn.Linear(d, int(p["out"]), bias=p.get("bias", True))
            elif op == "lora_linear":
                m = LoRALinear(d, int(p["out"]), int(p.get("rank", 8)), p.get("alpha"))
            elif op == "mlp":
                m = MLP(d, int(p["hidden"]), int(p.get("out") or d), p.get("act", "gelu"))
            elif op == "moe":
                m = MoE(d, int(p["hidden"]), int(p["experts"]), int(p.get("top_k", 2)), p.get("out"), float(p.get("aux", 0.01)),
                        bool(p.get("shared_expert")), p.get("act", "gelu"))
            elif op == "attention":
                m = Attention(d, int(p.get("heads", 4)), bool(p.get("causal", spec["task"] == "lm")))
            elif op == "transformer_block":
                m = Block(d, int(p.get("heads", 4)), int(p.get("hidden", 4 * d)), int(p.get("experts", 0)), int(p.get("top_k", 2)),
                          bool(p.get("causal", spec["task"] == "lm")), float(p.get("aux", 0.01)), bool(p.get("shared_expert")))
            elif op == "embedding":
                m = embed = Embed(int(p["vocab"]), int(p["dim"]), int(sh[1]), p.get("positions", True))
            elif op == "layernorm":
                m = nn.LayerNorm(d, eps=float(p.get("eps", 1e-5)))
            elif op == "rmsnorm":
                m = RMSNorm(d)
            elif op == "conv1d":
                m = Conv1d(d, int(p["out"]), int(p.get("kernel", 3)), int(p.get("stride", 1)), p.get("same", True))
            elif op == "gru":
                m = GRU(d, int(p["hidden"]), bool(p.get("sequence")))
            elif op == "select_last":
                m = Fn(lambda x: x[:, -1])
            elif op == "mean_pool":
                m = Fn(lambda x: x.mean(1))
            elif op == "flatten":
                m = Fn(lambda x: x.flatten(1))
            elif op == "dropout":
                m = nn.Dropout(float(p.get("p", 0.1)))
            elif op == "scale":
                m = Scale(d)
            elif op == "lm_head":
                m = LMHead(d, int(p["vocab"]), embed.wte if p.get("tied", True) and embed is not None else None)
            elif op == "low_rank_markov_head":
                m = Markov(d, int(p.get("rank", 32)), int(p["vocab"]))
            elif op == "confidence_head":
                m = nn.Sequential(nn.Linear(d, int(p.get("proj", 128))), nn.GELU(), nn.Linear(int(p.get("proj", 128)), 1), nn.Sigmoid())
            elif op in ("add", "concat"):
                m = None
            else:
                m = Fn(act_fn(op))
            if m is not None:
                self.mods[nid] = m
            self.order.append((nid, op))
            self.inputs[nid] = ins
            shapes[nid] = tuple(n["shape"])
        self.output = spec["output"]
        if embed is not None:
            self._gpt_init(sum(1 for _, op in self.order if op in ("transformer_block", "attention")))

    def _gpt_init(self, layers: int) -> None:
        """KotMoE's (GPT-2's) initialisation for token models: N(0, 0.02), residual projections scaled by
        1/sqrt(2 * layers), norms at 1, biases at 0. (PyTorch's N(0, 1) embedding would make a tied head's first
        logits huge: a starting loss far above ln(vocab).)"""
        proj_std = 0.02 / math.sqrt(2 * max(1, layers))
        for name, p in self.named_parameters():
            if name.endswith(".bias") or name.endswith(".b") and p.dim() == 1:
                nn.init.zeros_(p)
            elif p.dim() == 1:
                nn.init.ones_(p)
            elif name.endswith("proj.weight"):
                nn.init.normal_(p, 0.0, proj_std)
            else:
                nn.init.normal_(p, 0.0, 0.02)

    def forward(self, x):
        vals = {"input": x}
        for nid, op in self.order:
            ins = [vals[i] for i in self.inputs[nid]]
            if op == "add":
                y = ins[0]
                for z in ins[1:]:
                    y = y + z
            elif op == "concat":
                y = torch.cat(ins, -1)
            else:
                y = self.mods[nid](ins[0])
            vals[nid] = y
        return vals[self.output]

    def aux_loss(self):
        tot = 0.0
        for m in self.modules():
            if isinstance(m, MoE):
                tot = tot + m.aux
        return tot

    def expert_load(self) -> dict:
        return {n: [round(float(v), 4) for v in m.load] for n, m in self.named_modules() if isinstance(m, MoE)}


def _in_shape(inp):
    k = inp.get("kind", "features")
    return (int(inp["size"]),) if k == "features" else ("tok", int(inp["ctx"])) if k == "tokens" else (int(inp["steps"]), int(inp["size"]))


# ---- KotMoE-gen checkpoints (KotMoE's Kotlin format, big-endian floats) ------------------------------------------- #

def kmoe_order(model: Graph):
    """KotMoE's parameter order: wte, wpe, per block (ln1, qkv, attproj, ln2, router, experts fc/proj), lnf."""
    emb = next(m for m in model.modules() if isinstance(m, Embed))
    out = [emb.wte.weight, emb.wpe.weight]
    for b in (m for m in model.modules() if isinstance(m, Block)):
        out += [b.ln1.weight, b.ln1.bias, b.attn.qkv.weight, b.attn.qkv.bias, b.attn.proj.weight, b.attn.proj.bias,
                b.ln2.weight, b.ln2.bias, b.ff.router.weight]
        for e in b.ff.experts:
            out += [e.fc.weight, e.fc.bias, e.proj.weight, e.proj.bias]
    lnf = [m for m in model.modules() if isinstance(m, nn.LayerNorm)][-1]
    return out + [lnf.weight, lnf.bias]


def kmoe_config(spec: dict) -> list[int]:
    blocks = [n for n in spec["nodes"] if n["op"] == "transformer_block"]
    emb = next(n for n in spec["nodes"] if n["op"] == "embedding")
    p = blocks[0]["params"]
    dim = int(emb["params"]["dim"])
    return [int(emb["params"]["vocab"]), int(spec["input"]["ctx"]), dim, len(blocks), int(p.get("heads", 4)),
            int(p["experts"]), int(p.get("top_k", 2)), int(p.get("hidden", 4 * dim))]


def save_kmoe(model: Graph, spec: dict, path: Path, step: int) -> None:
    aux = float(next(n for n in spec["nodes"] if n["op"] == "transformer_block")["params"].get("aux", 0.01))
    with open(path, "wb") as f:
        f.write(struct.pack(">ii", 0x4B4D4F45, 1))
        f.write(struct.pack(">8i", *kmoe_config(spec)))
        f.write(struct.pack(">fi", aux, step))
        for t in kmoe_order(model):
            f.write(t.detach().float().cpu().contiguous().numpy().astype(">f4").tobytes())


def load_kmoe(model: Graph, spec: dict, path: Path) -> int:
    import numpy as np
    data = Path(path).read_bytes()
    magic, ver = struct.unpack(">ii", data[:8])
    if magic != 0x4B4D4F45 or ver != 1:
        raise ValueError(f"{path} is not a KotMoE checkpoint")
    cfg = list(struct.unpack(">8i", data[8:40]))
    if cfg != kmoe_config(spec):
        raise ValueError(f"{path} is {cfg}, the spec is {kmoe_config(spec)} (vocab ctx dim layers heads experts topK hidden)")
    _aux, step = struct.unpack(">fi", data[40:48])
    off = 48
    with torch.no_grad():
        for t in kmoe_order(model):
            n = t.numel()
            arr = np.frombuffer(data, dtype=">f4", count=n, offset=off).astype("float32")
            t.copy_(torch.from_numpy(arr).view_as(t))
            off += 4 * n
    if off != len(data):
        raise ValueError(f"{path}: {len(data) - off} bytes left over (not the same design)")
    return step


# ---- tokenizers ---------------------------------------------------------------------------------------------------- #

class ByteTok:
    vocab = 256

    def encode(self, s):
        return list(s.encode("utf-8"))

    def decode(self, ids):
        return bytes(i for i in ids if i < 256).decode("utf-8", "replace")


class KotMoETok:
    """KotMoE's byte-level BPE (kotmoe-bpe 1 files), the same pieces and merges as Tokenizer.kt."""

    def __init__(self, path):
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        if lines[0] != "kotmoe-bpe 1":
            raise ValueError(f"{path} is not a KotMoE tokenizer")
        self.merges = [tuple(map(int, l.split())) for l in lines[1:] if l.strip()]
        self.rank = {m: 256 + i for i, m in enumerate(self.merges)}
        self.bytes = [bytes([i]) for i in range(256)]
        for a, b in self.merges:
            self.bytes.append(self.bytes[a] + self.bytes[b])
        self.vocab = 256 + len(self.merges)

    @staticmethod
    def pieces(text):
        out, i, n = [], 0, len(text)
        w = lambda c: c.isalnum() or c == "_"
        while i < n:
            start, c = i, text[i]
            if c == " " and i + 1 < n and w(text[i + 1]):
                i += 1
                while i < n and w(text[i]):
                    i += 1
            elif w(c):
                while i < n and w(text[i]):
                    i += 1
            elif c in " \t":
                while i < n and text[i] in " \t":
                    i += 1
                if i < n and w(text[i]) and text[i - 1] == " " and i - 1 > start:
                    i -= 1
            else:
                i += 1
            if i == start:
                i += 1
            out.append(text[start:i])
        return out

    def _piece(self, bs):
        ids = list(bs)
        while len(ids) > 1:
            best = min(((self.rank.get((ids[j], ids[j + 1]), 1 << 30), j) for j in range(len(ids) - 1)), default=(1 << 30, -1))
            if best[0] >= 1 << 30:
                break
            r, out, j = best[0], [], 0
            while j < len(ids):
                if j < len(ids) - 1 and self.rank.get((ids[j], ids[j + 1])) == r:
                    out.append(r)
                    j += 2
                else:
                    out.append(ids[j])
                    j += 1
            ids = out
        return ids

    def encode(self, s):
        cache, out = {}, []
        for p in self.pieces(s):
            if p not in cache:
                cache[p] = self._piece(p.encode("utf-8"))
            out += cache[p]
        return out

    def decode(self, ids):
        return b"".join(self.bytes[i] for i in ids if i < len(self.bytes)).decode("utf-8", "replace")


class HFTok:
    def __init__(self, path):
        from transformers import AutoTokenizer
        self.t = AutoTokenizer.from_pretrained(path)
        self.vocab = len(self.t)

    def encode(self, s):
        return self.t(s, add_special_tokens=False)["input_ids"]

    def decode(self, ids):
        return self.t.decode(ids)


class WordTok:
    """Word-level, as BrainBuilder's text_sequence data: the vocab-1 most frequent words, 0 for any other."""

    def __init__(self, text, vocab):
        import collections
        import re
        self.split = lambda s: re.findall(r"[A-Za-z0-9']+|[^\sA-Za-z0-9']", s.lower())
        common = collections.Counter(self.split(text)).most_common(vocab - 1)
        self.words = ["<unk>"] + [w for w, _ in common]
        self.ids = {w: i for i, w in enumerate(self.words)}
        self.vocab = vocab

    def encode(self, s):
        return [self.ids.get(w, 0) for w in self.split(s)]

    def decode(self, ids):
        return " ".join(self.words[i] for i in ids if i < len(self.words))


def tokenizer(name, text="", vocab=0):
    if name == "words":
        return WordTok(text, vocab)
    if not name or name == "byte":
        return ByteTok()
    p = Path(name)
    if p.is_file():
        return KotMoETok(p)
    return HFTok(name)


# ---- data ---------------------------------------------------------------------------------------------------------- #

def load_tabular(d: dict, spec: dict):
    import numpy as np
    p = Path(d["path"])
    labels = None
    if p.suffix == ".npz":
        z = np.load(p, allow_pickle=False)
        X = z["X"].astype("float32")
        y = z["y"] if "y" in z.files else None
        names = list(z["names"]) if "names" in z.files else []
    else:
        if p.suffix == ".csv":
            with open(p, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        else:
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        tgt = d.get("target")
        names = d.get("features") or [k for k in rows[0] if k != tgt]
        X = np.array([[float(r.get(k) or 0) for k in names] for r in rows], dtype="float32")
        y = [r[tgt] for r in rows] if tgt else None
    if y is not None and spec["task"] == "classify":
        y = [str(v) for v in (y.tolist() if hasattr(y, "tolist") else y)]
        labels = sorted(set(y), key=lambda v: (len(v), v))
        idx = {v: i for i, v in enumerate(labels)}
        y = np.array([idx[v] for v in y], dtype="int64")
    elif y is not None:
        y = np.asarray(y, dtype="float32")
        if y.ndim == 1:
            y = y[:, None]
    flat = X.reshape(-1, X.shape[-1])
    mu, sd = flat.mean(0), flat.std(0)
    sd = np.where(sd < 1e-3, 1.0, sd).astype("float32")     # a feature that never varied: don't blow up other values
    X = (X - mu) / sd
    norm = {"mean": mu.tolist(), "std": sd.tolist()}
    if y is not None and spec["task"] == "regress":
        ym, ys = y.mean(0), y.std(0) + 1e-6
        y = (y - ym) / ys
        norm["y_mean"], norm["y_std"] = ym.tolist(), ys.tolist()
    if spec["task"] == "anomaly":
        y = X.reshape(X.shape[0], -1).copy()
    return torch.from_numpy(X), (torch.from_numpy(y) if y is not None else None), norm, labels, names


def load_text(d: dict, spec: dict):
    text = "".join(Path(p).read_text(encoding="utf-8", errors="replace") for p in ([d["path"]] if isinstance(d["path"], str) else d["path"]))
    tok = tokenizer(d.get("tokenizer", "byte"), text, int(spec["input"]["vocab"]))
    if tok.vocab > int(spec["input"]["vocab"]):
        raise ValueError(f"the tokenizer has {tok.vocab} tokens but the model's vocab is {spec['input']['vocab']}")
    ids = torch.tensor(tok.encode(text), dtype=torch.long)
    return ids, tok


def load_next_token(d: dict, spec: dict):
    """Token-sequence classification (BrainBuilder's next-word graphs): every window of ctx tokens -> the next one."""
    ids, tok = load_text(d, spec)
    ctx = int(spec["input"]["ctx"])
    if len(ids) <= ctx:
        raise ValueError(f"the text is {len(ids)} tokens: too short for windows of {ctx}")
    X = ids.unfold(0, ctx, 1)[:-1].contiguous()
    y = ids[ctx:].contiguous()
    return X, y, tok


# ---- the run ------------------------------------------------------------------------------------------------------- #

def main() -> int:
    job = json.loads((RUN / "job.json").read_text(encoding="utf-8"))
    torch.set_num_threads(int(job.get("cpu_threads", 4)))
    spec = job["spec"]
    tr = {**spec.get("train", {}), **job.get("train", {})}
    seed = int(tr.get("seed", 1337))
    random.seed(seed)
    torch.manual_seed(seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cpu" and not job.get("allow_cpu"):
        report(state="failed", error="no GPU visible to PyTorch (training on this machine's CPU is not allowed)")
        return 2
    report(state="loading", device=torch.cuda.get_device_name(0) if dev == "cuda" else "cpu")
    model = Graph(spec).to(dev)
    if job.get("init_kmoe"):
        report(state="loading", note=f"weights from KotMoE checkpoint {job['init_kmoe']}")
        load_kmoe(model, spec, Path(job["init_kmoe"]))
    if job.get("init_weights"):
        model.load_state_dict(torch.load(job["init_weights"], map_location=dev), strict=False)
    nparams = sum(p.numel() for p in model.parameters())
    lm = spec["task"] == "lm"
    meta = {"task": spec["task"]}
    if lm:
        ids, tok = load_text(job["data"], spec)
        ctx = int(spec["input"]["ctx"])
        n_val = max(ctx + 1, int(len(ids) * float(tr.get("val_fraction", 0.05))))
        train_ids, val_ids = ids[:-n_val], ids[-n_val:]
        if len(train_ids) <= ctx + 1:
            report(state="failed", error=f"the text is {len(ids)} tokens: too short for a context of {ctx}")
            return 2
        meta["tokenizer"] = job["data"].get("tokenizer", "byte")
        bs = int(tr.get("batch_size", 16))
        steps_per_epoch = max(1, len(train_ids) // (bs * ctx))

        def batch(src):
            ix = torch.randint(0, len(src) - ctx - 1, (bs,))
            x = torch.stack([src[i:i + ctx] for i in ix])
            y = torch.stack([src[i + 1:i + 1 + ctx] for i in ix])
            return x.to(dev), y.to(dev)
    else:
        if spec["input"].get("kind") == "tokens":
            X, Y, tok = load_next_token(job["data"], spec)
            norm = {}
            meta.update({"tokenizer": job["data"].get("tokenizer", "byte"),
                         "words": getattr(tok, "words", None)})
        else:
            X, Y, norm, labels, names = load_tabular(job["data"], spec)
            meta.update({"norm": norm, "labels": labels, "features": names})
        n = X.shape[0]
        perm = torch.randperm(n, generator=torch.Generator().manual_seed(seed))
        n_val = max(1, int(n * float(tr.get("val_fraction", 0.15)))) if n > 10 else 0
        vi, ti = perm[:n_val], perm[n_val:]
        Xt, Yt, Xv, Yv = X[ti].to(dev), Y[ti].to(dev), X[vi].to(dev), Y[vi].to(dev)
        bs = min(int(tr.get("batch_size", 256)), len(ti))
        steps_per_epoch = max(1, math.ceil(len(ti) / bs))
    epochs = float(tr.get("epochs", 20))
    total = int(tr.get("max_steps") or max(1, int(steps_per_epoch * epochs)))
    lr = float(tr.get("lr", 1e-3))
    decay = [p for n_, p in model.named_parameters() if p.dim() >= 2]
    no_decay = [p for n_, p in model.named_parameters() if p.dim() < 2]
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": float(tr.get("weight_decay", 0.01))},
                             {"params": no_decay, "weight_decay": 0.0}], lr=lr, betas=(0.9, float(tr.get("beta2", 0.95 if lm else 0.999))))
    warm = int(tr.get("warmup_steps", max(1, total // 20)))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: (s + 1) / warm if s < warm else
                                              0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * (s - warm) / max(1, total - warm))))
    amp = dev == "cuda" and bool(tr.get("amp", lm))
    dt = torch.bfloat16 if amp and torch.cuda.is_bf16_supported() else torch.float16

    def loss_of(out, y):
        if spec["task"] in ("classify", "lm"):
            return F.cross_entropy(out.reshape(-1, out.shape[-1]).float(), y.reshape(-1))
        return F.mse_loss(out.float().reshape(y.shape), y.float())

    def evaluate():
        model.eval()
        with torch.no_grad(), torch.autocast(dev, dtype=dt, enabled=amp):
            if lm:
                ls = []
                for _ in range(int(tr.get("eval_batches", 20))):
                    x, y = batch(val_ids)
                    ls.append(loss_of(model(x), y).item())
                v = sum(ls) / len(ls)
                res = {"val_loss": round(v, 4), "val_perplexity": round(math.exp(min(v, 20)), 2)}
            elif n_val:
                out = torch.cat([model(Xv[i:i + 4096]) for i in range(0, len(Xv), 4096)])
                v = loss_of(out, Yv).item()
                res = {"val_loss": round(v, 5)}
                if spec["task"] == "classify":
                    res["val_accuracy"] = round((out.argmax(-1) == Yv).float().mean().item(), 4)
                elif spec["task"] == "regress":
                    yt = Yv.float().reshape(out.shape)
                    res["val_r2"] = round(1 - ((out.float() - yt) ** 2).sum().item() / max(1e-9, ((yt - yt.mean(0)) ** 2).sum().item()), 4)
                    if norm.get("y_std"):
                        res["val_mae"] = round(((out.float() - yt).abs().mean(0).cpu() * torch.tensor(norm["y_std"])).mean().item(), 5)
            else:
                res = {}
        model.train()
        return res

    report(state="training", params=nparams, total_steps=total, steps_per_epoch=steps_per_epoch, train_rows=None if lm else len(ti),
           val_rows=None if lm else n_val, tokens=len(ids) if lm else None)
    model.train()
    best, best_step, patience = float("inf"), 0, int(tr.get("patience", 0))
    t0, hist = time.time(), []
    eval_every = int(tr.get("eval_every", max(1, total // 20)))
    ckpt = RUN / "best.pt"
    step = 0
    while step < total:
        if lm:
            x, y = batch(train_ids)
        else:
            j = torch.randint(0, len(ti), (bs,), device=dev)
            x, y = Xt[j], Yt[j]
        with torch.autocast(dev, dtype=dt, enabled=amp):
            out = model(x)
            loss = loss_of(out, y)
            aux = model.aux_loss()
        (loss + aux).backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), float(tr.get("clip", 1.0)))
        opt.step()
        sched.step()
        opt.zero_grad(set_to_none=True)
        step += 1
        hist.append(loss.item())
        if step % eval_every == 0 or step == total:
            ev = evaluate()
            el = time.time() - t0
            v = ev.get("val_loss", sum(hist) / len(hist))
            if v < best:
                best, best_step = v, step
                torch.save(model.state_dict(), ckpt)
            info = {"step": step, "loss": round(sum(hist) / len(hist), 5), "aux": round(float(aux), 5), "lr": sched.get_last_lr()[0],
                    "elapsed": round(el, 1), "eta": round(el / step * (total - step), 1), **ev}
            if dev == "cuda":
                info["gpu_mem_gb"] = round(torch.cuda.max_memory_allocated() / 2**30, 3)
            load = model.expert_load()
            if load:
                info["expert_load"] = load
            hist = []
            report(state="training", **info)
            if patience and step - best_step >= patience * eval_every:
                report(state="training", note=f"early stop: no better validation loss for {patience} evaluations")
                break
            if STOP.exists():
                report(state="stopping", note="stop requested")
                break
    if ckpt.exists():
        model.load_state_dict(torch.load(ckpt, map_location=dev))
    final = evaluate()
    exp = RUN / "export"
    exp.mkdir(exist_ok=True)
    sd = {k: v.detach().float().cpu().contiguous() for k, v in model.state_dict().items()}
    import numpy as np
    np.savez(exp / "weights.npz", **{k: v.numpy() for k, v in sd.items()})
    try:
        from safetensors.torch import save_file
        seen, uniq = set(), {}
        for k, v in sd.items():                                   # tied weights: safetensors wants each storage once
            if v.data_ptr() not in seen:
                seen.add(v.data_ptr())
                uniq[k] = v
        save_file(uniq, str(exp / "weights.safetensors"))
    except ImportError:
        pass
    if lm and any(n_["op"] == "transformer_block" and n_["params"].get("experts") for n_ in spec["nodes"]) and job.get("kmoe", True):
        try:
            save_kmoe(model, spec, exp / "model.kmoe", best_step or step)
        except (StopIteration, AttributeError, ValueError) as e:
            report(note=f"no KotMoE checkpoint: {e}")
    if lm:
        x = torch.tensor([tok.encode(job.get("sample_prompt", "The "))[-int(spec["input"]["ctx"]):]], device=dev)
        model.eval()
        with torch.no_grad():
            for _ in range(int(job.get("sample_tokens", 60))):
                logits = model(x[:, -int(spec["input"]["ctx"]):])[:, -1].float() / 0.8
                x = torch.cat([x, torch.multinomial(torch.softmax(logits, -1), 1)], 1)
        final["sample"] = tok.decode(x[0].tolist())
    (exp / "model.json").write_text(json.dumps({"spec": spec, **meta, "metrics": final, "best_step": best_step,
                                                "params": nparams, "steps": step}, indent=1, default=float), encoding="utf-8")
    report(state="done", steps=step, best_step=best_step, final=final, export=str(exp))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                                       # noqa: BLE001 - the run's status says what broke
        import traceback
        report(state="failed", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-4000:])
        sys.exit(1)
