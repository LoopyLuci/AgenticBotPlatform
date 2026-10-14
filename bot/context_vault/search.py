"""Keyword (BM25-ish) scoring and vector helpers, pure Python so the vault needs nothing beyond the standard library."""
from __future__ import annotations

import math
import re
import struct
from collections import Counter
from typing import Iterable, Optional

_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_STOP = frozenset("the a an and or of to in on for is are was be it this that with as at by from we you i".split())


def tokens(text: str) -> list[str]:
    return [w for w in (m.lower() for m in _WORD.findall(text or "")) if w not in _STOP]


def bm25(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    q = list(dict.fromkeys(tokens(query)))
    if not q or not docs:
        return [0.0] * len(docs)
    toks = [Counter(tokens(d)) for d in docs]
    lens = [sum(t.values()) or 1 for t in toks]
    avg = sum(lens) / len(lens)
    n = len(docs)
    df = {w: sum(1 for t in toks if w in t) for w in q}
    out = []
    for t, ln in zip(toks, lens):
        s = 0.0
        for w in q:
            f = t.get(w, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * ln / avg))
        out.append(s)
    return out


def jaccard(a: str, b: str) -> float:
    x, y = set(tokens(a)), set(tokens(b))
    return len(x & y) / len(x | y) if x and y else 0.0


def pack(vec: Optional[Iterable[float]]) -> Optional[bytes]:
    if vec is None:
        return None
    v = [float(x) for x in vec]
    return struct.pack(f"<{len(v)}f", *v) if v else None


def unpack(blob: Optional[bytes]) -> Optional[list[float]]:
    if not blob:
        return None
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
