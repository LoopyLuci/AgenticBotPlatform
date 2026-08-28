# ADR-0002: Support Bot's neural classifier is NumPy from scratch, not scikit-learn

**Status:** Accepted
**Date:** 2026-08-27

## Context

The Support Bot's hybrid classifier's neural sub-model was originally
built on scikit-learn's `MLPClassifier` — a one-hidden-layer, 64-unit
network trained on a few hundred short phrases. scikit-learn pulls in
scipy and its compiled libraries as a transitive dependency, which
measured out to over 100MB — more than half of the entire bundled Python
environment shipped inside the desktop app's installer — to run
something this small.

## Decision

Rewrote the exact same architecture (He-initialized weights, ReLU hidden
layer, softmax output, full-batch gradient descent on cross-entropy loss)
directly on `numpy` alone, in `bot/support_bot/nn_model.py`. `numpy` is
the one numeric dependency this project accepts, deliberately isolated to
this one module.

## Consequences

Windows installer size dropped from 136MB to 72.7MB (MSI) — roughly
47% — with verified-identical behavior (100% fit on the training corpus,
correct out-of-domain rejection) and sub-second train time. The tradeoff
is that the training loop (backprop, gradient descent) is now
hand-maintained code instead of a battle-tested library's — acceptable
here because the model is small and fixed-shape, not a place future
growth is expected; a genuinely larger model would tip this calculus back
toward a real ML library.
