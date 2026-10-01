"""Run a small exported lab model inside ABP with numpy (no PyTorch, no GPU): what the system models use to make a
decision in microseconds. Covers the ops small feature models use: linear, mlp, moe, layernorm, rmsnorm, scale,
activations, dropout (a no-op here), add, concat, flatten, lora_linear.

    m = Model.load(<run>/export)            reads model.json + weights.npz
    m.predict(rows)                         rows: dicts of feature values, or an (N, F) array; returns the outputs
                                            in the original units (the training normalisation is undone)
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from bot.localai.paths import LocalAIError

_SQ2PI = math.sqrt(2 / math.pi)
ACT = {"gelu": lambda x: 0.5 * x * (1 + np.tanh(_SQ2PI * (x + 0.044715 * x ** 3))), "relu": lambda x: np.maximum(x, 0),
       "silu": lambda x: x / (1 + np.exp(-x)), "tanh": np.tanh, "sigmoid": lambda x: 1 / (1 + np.exp(-x)),
       "leaky_relu": lambda x: np.where(x > 0, x, 0.01 * x), "softplus": lambda x: np.log1p(np.exp(x))}


class Model:
    def __init__(self, meta: dict, w: dict[str, np.ndarray]):
        self.meta, self.w = meta, w
        self.spec = meta["spec"]
        if self.spec["input"].get("kind", "features") != "features":
            raise LocalAIError("numpy inference covers feature models; run sequence/token models with PyTorch")
        norm = meta.get("norm") or {}
        self.mu = np.asarray(norm.get("mean", 0.0), dtype=np.float32)
        self.sd = np.asarray(norm.get("std", 1.0), dtype=np.float32)
        self.y_mu = np.asarray(norm["y_mean"], dtype=np.float32) if "y_mean" in norm else None
        self.y_sd = np.asarray(norm["y_std"], dtype=np.float32) if "y_std" in norm else None
        self.features = meta.get("features") or []
        self.labels = meta.get("labels")

    @classmethod
    def load(cls, export_dir: str | Path) -> "Model":
        d = Path(export_dir)
        try:
            meta = json.loads((d / "model.json").read_text(encoding="utf-8"))
            with np.load(d / "weights.npz") as z:
                w = {k: z[k].astype(np.float32) for k in z.files}
        except (OSError, ValueError, KeyError) as e:
            raise LocalAIError(f"{d} is not a lab model export: {e}") from e
        return cls(meta, w)

    def _p(self, nid: str, name: str) -> np.ndarray:
        return self.w[f"mods.{nid}.{name}"]

    def _lin(self, x, pre: str):
        y = x @ self.w[pre + ".weight"].T
        b = self.w.get(pre + ".bias")
        return y + b if b is not None else y

    def _moe(self, x, nid: str, p: dict):
        e, k = int(p["experts"]), int(p.get("top_k", 2))
        pre = f"mods.{nid}"
        logits = x @ self.w[pre + ".router.weight"].T
        if pre + ".router.bias" in self.w:
            logits = logits + self.w[pre + ".router.bias"]
        logits = logits - logits.max(-1, keepdims=True)
        probs = np.exp(logits)
        probs /= probs.sum(-1, keepdims=True)
        top = np.argsort(-probs, -1)[:, :k]
        gates = np.take_along_axis(probs, top, -1)
        gates /= gates.sum(-1, keepdims=True)
        act = ACT[p.get("act", "gelu")]
        out = None
        for ex in range(e):
            rows, slot = np.nonzero(top == ex)
            if rows.size == 0:
                continue
            ep = f"{pre}.experts.{ex}"
            y = self._lin(act(self._lin(x[rows], ep + ".fc")), ep + ".proj") * gates[rows, slot][:, None]
            if out is None:
                out = np.zeros((x.shape[0], y.shape[1]), dtype=np.float32)
            np.add.at(out, rows, y)
        if pre + ".shared.fc.weight" in self.w:
            out = out + self._lin(act(self._lin(x, pre + ".shared.fc")), pre + ".shared.proj")
        return out, np.bincount(top[:, 0], minlength=e) / max(1, x.shape[0])

    def forward(self, X: np.ndarray, with_routing: bool = False):
        vals: dict[str, Any] = {"input": X}
        routing = {}
        for n in self.spec["nodes"]:
            nid, op, p = n["id"], n["op"], n["params"]
            ins = [n["in"]] if isinstance(n["in"], str) else n["in"]
            x = vals[ins[0]]
            if op == "linear":
                y = self._lin(x, f"mods.{nid}")
            elif op == "lora_linear":
                y = self._lin(x, f"mods.{nid}.base") + (x @ self._p(nid, "a").T @ self._p(nid, "b").T) * \
                    ((p.get("alpha") or p.get("rank", 8)) / p.get("rank", 8))
            elif op == "mlp":
                y = self._lin(ACT[p.get("act", "gelu")](self._lin(x, f"mods.{nid}.fc")), f"mods.{nid}.proj")
            elif op == "moe":
                y, routing[nid] = self._moe(x, nid, p)
            elif op == "layernorm":
                m, v = x.mean(-1, keepdims=True), x.var(-1, keepdims=True)
                y = (x - m) / np.sqrt(v + float(p.get("eps", 1e-5))) * self._p(nid, "weight") + self._p(nid, "bias")
            elif op == "rmsnorm":
                y = x / np.sqrt((x ** 2).mean(-1, keepdims=True) + 1e-6) * self._p(nid, "w")
            elif op == "scale":
                y = x * self._p(nid, "w")
            elif op == "dropout":
                y = x
            elif op == "flatten":
                y = x.reshape(x.shape[0], -1)
            elif op == "add":
                y = sum(vals[i] for i in ins)
            elif op == "concat":
                y = np.concatenate([vals[i] for i in ins], -1)
            elif op in ACT:
                y = ACT[op](x)
            else:
                raise LocalAIError(f"numpy inference has no {op} (node {nid})")
            vals[nid] = y
        out = vals[self.spec["output"]]
        return (out, routing) if with_routing else out

    def matrix(self, rows) -> np.ndarray:
        if isinstance(rows, np.ndarray):
            return rows.astype(np.float32)
        if isinstance(rows, dict):
            rows = [rows]
        return np.array([[float(r.get(f, 0.0)) for f in self.features] for r in rows], dtype=np.float32)

    def _norm(self, rows) -> np.ndarray:
        """Normalised as in training, clipped to +-8 spreads: a value far outside what the model saw (a drive size it
        never measured) can't saturate every layer."""
        sd = np.where(self.sd < 1e-3, 1.0, self.sd)
        return np.clip((self.matrix(rows) - self.mu) / sd, -8.0, 8.0)

    def predict(self, rows) -> np.ndarray:
        X = self._norm(rows)
        y = self.forward(X)
        if self.spec["task"] == "regress" and self.y_sd is not None:
            y = y * self.y_sd + self.y_mu
        elif self.spec["task"] == "classify":
            z = y - y.max(-1, keepdims=True)
            y = np.exp(z) / np.exp(z).sum(-1, keepdims=True)
        return y

    def anomaly(self, rows) -> np.ndarray:
        """Reconstruction error per row, in units of the training data's spread (for anomaly models)."""
        X = self._norm(rows)
        return np.sqrt(((self.forward(X) - X) ** 2).mean(-1))
