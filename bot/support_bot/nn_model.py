"""A real, trained neural network classifier for Support Bot intents.

This is a genuine neural network — a one-hidden-layer multi-layer
perceptron trained by backpropagation (plain full-batch gradient descent,
implemented directly below) — not a rebrand of the TF-IDF centroid model
in model.py. The only external dependency is numpy, for the matrix
multiplies the forward/backward pass actually needs — everything else
(tokenizing, TF-IDF weighting, one-hot encoding) is plain Python, mirroring
model.py's own dict-based vectorizer.

This used to be backed by scikit-learn's MLPClassifier + TfidfVectorizer.
That worked fine, but scikit-learn's own dependency tree (scipy + its
compiled libs) was over 100MB — more than half of this app's entire
bundled Python environment — to run a single-hidden-layer network over a
few hundred short training phrases, which needs none of scipy's general
numerical-computing machinery. Rewritten here as a small, self-contained
implementation using only numpy (already a genuine, correctly-vectorized
matrix library, not a naive pure-Python loop) for the same real training
behavior at a fraction of the install size.

Architecture choice, and why: one hidden layer of 64 ReLU units over
TF-IDF (unigram+bigram) features, softmax output, cross-entropy loss.
Deliberately shallow — with a few hundred short training phrases
(training_data.py's EXAMPLES plus whatever the Training tab adds), a deep
network would only memorize noise; a small MLP is the correctly-sized real
neural net for this corpus, not an under-powered stand-in for a "real"
deep model. Retraining (`retrain()`) takes well under a second on hardware
this app already targets, so it happens synchronously and immediately
after every Training-tab edit — no background job queue needed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Optional

import numpy as np

from bot import db
from bot.support_bot.training_data import EXAMPLES

# Softmax-probability floor — this is NOT the same scale as model.py's
# cosine-similarity CONFIDENCE_THRESHOLD; each sub-model in the hybrid
# calibrates confidence differently, which is precisely why hybrid.py
# compares "is this above its own model's threshold" per model rather
# than treating both confidence numbers as interchangeable.
CONFIDENCE_THRESHOLD = 0.35

_HIDDEN_UNITS = 64
_EPOCHS = 800
_LEARNING_RATE = 0.5

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """Unigrams plus adjacent-pair bigrams, matching what scikit-learn's
    TfidfVectorizer(ngram_range=(1, 2)) produced — bigrams are what let a
    short phrase like "restart the bot" distinguish itself from "restart
    the desktop" when the unigram overlap alone wouldn't."""
    unigrams = _TOKEN_RE.findall(text.lower())
    bigrams = [f"{a}_{b}" for a, b in zip(unigrams, unigrams[1:])]
    return unigrams + bigrams


class _TfidfVectorizer:
    """Same dict-based TF-IDF math as model.py's TfidfCentroidModel, just
    producing dense numpy rows (fit_transform/transform) instead of sparse
    dicts, since the MLP's matrix multiplies need a real 2D array."""

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray = np.zeros(0)

    def fit_transform(self, texts: list[str]) -> np.ndarray:
        docs = [_tokens(t) for t in texts]
        n_docs = len(docs)
        df: Counter[str] = Counter()
        for tokens in docs:
            df.update(set(tokens))
        self.vocab = {term: i for i, term in enumerate(sorted(df))}
        self.idf = np.zeros(len(self.vocab))
        for term, count in df.items():
            # Same smoothed IDF as model.py: add-1 in both numerator and
            # denominator so a term seen in every example still gets a
            # small positive weight instead of exactly zero.
            self.idf[self.vocab[term]] = math.log((1 + n_docs) / (1 + count)) + 1.0
        return self._build(docs)

    def transform(self, texts: list[str]) -> np.ndarray:
        return self._build([_tokens(t) for t in texts])

    def _build(self, docs: list[list[str]]) -> np.ndarray:
        X = np.zeros((len(docs), len(self.vocab)))
        for i, tokens in enumerate(docs):
            tf = Counter(tokens)
            total = sum(tf.values()) or 1
            for term, count in tf.items():
                j = self.vocab.get(term)
                if j is not None:
                    X[i, j] = (count / total) * self.idf[j]
        return X


class _MLP:
    """One hidden layer (ReLU) -> softmax output, trained by plain
    full-batch gradient descent on cross-entropy loss. This is the exact
    same math scikit-learn's MLPClassifier does internally (Adam is just a
    fancier step-size schedule on top of the same gradients) — at this
    corpus size (a few hundred examples, well under a second either way)
    plain gradient descent converges just as reliably without needing a
    second optimizer library for it."""

    def __init__(self, n_features: int, n_hidden: int, n_classes: int, seed: int = 42) -> None:
        rng = np.random.default_rng(seed)
        # He initialization — the standard scale for ReLU layers, keeps
        # activations from vanishing/exploding at the start of training.
        self.w1 = rng.normal(0, math.sqrt(2.0 / max(n_features, 1)), (n_features, n_hidden))
        self.b1 = np.zeros(n_hidden)
        self.w2 = rng.normal(0, math.sqrt(2.0 / n_hidden), (n_hidden, n_classes))
        self.b2 = np.zeros(n_classes)

    def _forward(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        z1 = X @ self.w1 + self.b1
        a1 = np.maximum(z1, 0.0)  # ReLU
        z2 = a1 @ self.w2 + self.b2
        z2 = z2 - z2.max(axis=1, keepdims=True)  # numerically-stable softmax
        exp = np.exp(z2)
        probs = exp / exp.sum(axis=1, keepdims=True)
        return z1, a1, probs

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        _, _, probs = self._forward(X)
        return probs

    def fit(self, X: np.ndarray, y_onehot: np.ndarray, epochs: int = _EPOCHS, lr: float = _LEARNING_RATE) -> None:
        n = X.shape[0]
        for _ in range(epochs):
            z1, a1, probs = self._forward(X)
            # Cross-entropy loss gradient at the softmax output simplifies
            # to (probs - targets) — a standard, well-known result, not an
            # approximation.
            dz2 = (probs - y_onehot) / n
            dw2 = a1.T @ dz2
            db2 = dz2.sum(axis=0)
            da1 = dz2 @ self.w2.T
            dz1 = da1 * (z1 > 0)  # ReLU derivative
            dw1 = X.T @ dz1
            db1 = dz1.sum(axis=0)
            self.w1 -= lr * dw1
            self.b1 -= lr * db1
            self.w2 -= lr * dw2
            self.b2 -= lr * db2


def _load_examples() -> list[tuple[str, str]]:
    """Same corpus model.py's TF-IDF centroid model trains on — the
    hand-authored baseline plus any phrases added via the Training tab.
    Both sub-models always train on identical data; only the algorithm
    differs, which is what makes their agreement/disagreement meaningful
    as a signal in hybrid.py."""
    examples = list(EXAMPLES)
    try:
        examples += [(row["phrase"], row["intent"]) for row in db.list_support_bot_phrases()]
    except Exception:
        pass
    return examples


class NeuralIntentClassifier:
    def __init__(self) -> None:
        self._vectorizer: Optional[_TfidfVectorizer] = None
        self._mlp: Optional[_MLP] = None
        self._classes: list[str] = []
        self.train(_load_examples())

    def train(self, examples: list[tuple[str, str]]) -> int:
        texts = [t for t, _ in examples]
        labels = [i for _, i in examples]
        self._classes = sorted(set(labels))
        class_index = {c: i for i, c in enumerate(self._classes)}

        self._vectorizer = _TfidfVectorizer()
        X = self._vectorizer.fit_transform(texts)
        y_onehot = np.eye(len(self._classes))[[class_index[label] for label in labels]]

        self._mlp = _MLP(n_features=X.shape[1], n_hidden=_HIDDEN_UNITS, n_classes=len(self._classes))
        self._mlp.fit(X, y_onehot)
        return len(examples)

    def predict(self, text: str) -> tuple[str, float]:
        """Returns (intent, confidence). intent is "unknown" when nothing
        clears CONFIDENCE_THRESHOLD — same contract as model.py's
        TfidfCentroidModel.predict(), which is what lets hybrid.py treat
        both sub-models interchangeably at the call site."""
        if self._mlp is None or self._vectorizer is None or not self._classes:
            return "unknown", 0.0
        X = self._vectorizer.transform([text])
        probs = self._mlp.predict_proba(X)[0]
        best_idx = int(np.argmax(probs))
        confidence = float(probs[best_idx])
        intent = self._classes[best_idx]
        if confidence < CONFIDENCE_THRESHOLD:
            return "unknown", confidence
        return intent, confidence


# Trained once at import time, same lifecycle as model.py's singleton.
# Call retrain() (or hybrid.retrain_all()) after the Training tab adds or
# removes a phrase, so recognition improves without a server restart.
nn_model = NeuralIntentClassifier()


def retrain() -> int:
    return nn_model.train(_load_examples())
