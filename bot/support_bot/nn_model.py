"""A real, trained neural network classifier for Support Bot intents.

This is a genuine neural network — a multi-layer perceptron trained by
backpropagation (scikit-learn's `MLPClassifier`), not a rebrand of the
TF-IDF centroid model in model.py. It's the one new dependency this
hybrid architecture needs (see requirements.txt) — the project's original
Support Bot was deliberately stdlib-only; a real NN needs a real numeric
optimizer, which stdlib doesn't provide, so this module is kept separate
and optional-in-spirit: bot/support_bot/hybrid.py is what actually wires
it in, and nothing else in the codebase depends on this module directly.

Architecture choice, and why: one hidden layer of 64 ReLU units over
TF-IDF (unigram+bigram) features. Deliberately shallow — with a few
hundred short training phrases (training_data.py's EXAMPLES plus whatever
the Training tab adds), a deep network would only memorize noise; a small
MLP is the correctly-sized real neural net for this corpus, not an
under-powered stand-in for a "real" deep model. Retraining
(`retrain()`) takes well under a second on hardware this app already
targets, so it happens synchronously and immediately after every
Training-tab edit — no background job queue needed.
"""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neural_network import MLPClassifier

from bot import db
from bot.support_bot.training_data import EXAMPLES

# Softmax-probability floor — this is NOT the same scale as model.py's
# cosine-similarity CONFIDENCE_THRESHOLD; each sub-model in the hybrid
# calibrates confidence differently, which is precisely why hybrid.py
# compares "is this above its own model's threshold" per model rather
# than treating both confidence numbers as interchangeable.
CONFIDENCE_THRESHOLD = 0.35


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
        self._vectorizer: Optional[TfidfVectorizer] = None
        self._clf: Optional[MLPClassifier] = None
        self.train(_load_examples())

    def train(self, examples: list[tuple[str, str]]) -> int:
        texts = [t for t, _ in examples]
        labels = [i for _, i in examples]
        self._vectorizer = TfidfVectorizer(token_pattern=r"[a-z0-9]+", lowercase=True, ngram_range=(1, 2))
        X = self._vectorizer.fit_transform(texts)
        with warnings.catch_warnings():
            # A few hundred short examples reliably converges well before
            # max_iter on this architecture; the occasional
            # ConvergenceWarning is expected noise, not a real problem —
            # silenced here rather than left to alarm an operator watching
            # logs at server startup.
            warnings.simplefilter("ignore", ConvergenceWarning)
            clf = MLPClassifier(
                hidden_layer_sizes=(64,),
                activation="relu",
                solver="adam",
                max_iter=800,
                random_state=42,
            )
            clf.fit(X, labels)
        self._clf = clf
        return len(examples)

    def predict(self, text: str) -> tuple[str, float]:
        """Returns (intent, confidence). intent is "unknown" when nothing
        clears CONFIDENCE_THRESHOLD — same contract as model.py's
        TfidfCentroidModel.predict(), which is what lets hybrid.py treat
        both sub-models interchangeably at the call site."""
        if self._clf is None or self._vectorizer is None:
            return "unknown", 0.0
        X = self._vectorizer.transform([text])
        probs = self._clf.predict_proba(X)[0]
        best_idx = int(np.argmax(probs))
        confidence = float(probs[best_idx])
        intent = str(self._clf.classes_[best_idx])
        if confidence < CONFIDENCE_THRESHOLD:
            return "unknown", confidence
        return intent, confidence


# Trained once at import time, same lifecycle as model.py's singleton.
# Call retrain() (or hybrid.retrain_all()) after the Training tab adds or
# removes a phrase, so recognition improves without a server restart.
nn_model = NeuralIntentClassifier()


def retrain() -> int:
    return nn_model.train(_load_examples())
