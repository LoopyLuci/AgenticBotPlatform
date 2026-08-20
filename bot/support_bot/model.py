"""A small, custom, dependency-free text-classification model.

Deliberately not backed by numpy/sklearn/torch/Ollama/any external
inference service — the user explicitly wants a model that's genuinely
built into BotServer and runs on nothing but the local machine's own
Python interpreter. This is real, if simple, machine learning: TF-IDF
vectors computed from bot/support_bot/training_data.py, one centroid
vector per intent, classification by cosine similarity to the nearest
centroid. All arithmetic is plain Python (math.log/math.sqrt over dicts) —
no array library required.

Retraining is just re-importing this module: TfidfCentroidModel builds its
vocabulary/IDF/centroids once at construction time from EXAMPLES, so adding
phrases to training_data.py and restarting the server is the whole
"training" workflow.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Optional

from bot.support_bot.training_data import EXAMPLES

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Below this cosine similarity, nothing said looks close enough to any
# known intent to safely act on — better to say "I don't understand" than
# to guess and run the wrong management action.
CONFIDENCE_THRESHOLD = 0.22


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


Vector = dict[str, float]


def _dot(a: Vector, b: Vector) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(v * b[k] for k, v in a.items() if k in b)


def _norm(a: Vector) -> float:
    return math.sqrt(sum(v * v for v in a.values())) or 1.0


def _cosine(a: Vector, b: Vector) -> float:
    return _dot(a, b) / (_norm(a) * _norm(b))


class TfidfCentroidModel:
    def __init__(self, examples: list[tuple[str, str]]) -> None:
        self._idf: dict[str, float] = {}
        self._centroids: dict[str, Vector] = {}
        self._train(examples)

    def _train(self, examples: list[tuple[str, str]]) -> None:
        docs = [_tokenize(text) for text, _ in examples]
        n_docs = len(docs)

        df: Counter[str] = Counter()
        for tokens in docs:
            df.update(set(tokens))
        # Smoothed IDF (add-1 in both numerator and denominator) so a term
        # seen in every single example still gets a small positive weight
        # instead of exactly zero.
        self._idf = {term: math.log((1 + n_docs) / (1 + count)) + 1.0 for term, count in df.items()}

        sums: dict[str, Vector] = {}
        counts: Counter[str] = Counter()
        for (text, intent), tokens in zip(examples, docs):
            vec = self._vectorize(tokens)
            counts[intent] += 1
            bucket = sums.setdefault(intent, {})
            for term, weight in vec.items():
                bucket[term] = bucket.get(term, 0.0) + weight

        self._centroids = {
            intent: {term: total / counts[intent] for term, total in vec.items()}
            for intent, vec in sums.items()
        }

    def _vectorize(self, tokens: list[str]) -> Vector:
        tf = Counter(tokens)
        total = sum(tf.values()) or 1
        return {term: (count / total) * self._idf.get(term, 1.0) for term, count in tf.items()}

    def predict(self, text: str) -> tuple[str, float]:
        """Returns (intent, confidence). intent is "unknown" when nothing
        clears CONFIDENCE_THRESHOLD."""
        vec = self._vectorize(_tokenize(text))
        if not vec:
            return "unknown", 0.0
        best_intent: Optional[str] = None
        best_score = -1.0
        for intent, centroid in self._centroids.items():
            score = _cosine(vec, centroid)
            if score > best_score:
                best_intent, best_score = intent, score
        if best_intent is None or best_score < CONFIDENCE_THRESHOLD:
            return "unknown", max(best_score, 0.0)
        return best_intent, best_score


# Trained once at import time — cheap (a few hundred short strings) and the
# training set only changes when someone edits training_data.py and
# restarts the process, so there's no benefit to lazy/repeated training.
model = TfidfCentroidModel(EXAMPLES)
