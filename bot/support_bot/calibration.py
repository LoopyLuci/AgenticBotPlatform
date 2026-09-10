"""Confidence calibration for the Support Bot's classifiers — Phase 3 of
the next-generation modular hybrid plan.

Neither sub-model's raw confidence score is a real probability: the
TF-IDF model's is a cosine similarity (bounded [0, 1] by construction,
but not calibrated to "P(this classification is correct)"), and the
neural network's is a softmax output (a genuine probability distribution
over its OWN training classes, but still not necessarily well-calibrated
against real-world correctness, especially trained on a small corpus).
The 0.22/0.35 thresholds in model.py/nn_model.py are hand-picked cutoffs
on these raw scores, not principled probability thresholds.

This module fits a monotonic (isotonic) mapping from raw confidence to
"empirically observed P(correct) at around this raw score," using
Pool Adjacent Violators — pure Python, no numpy/scipy/sklearn, same
dependency-free ethos as model.py. It does not change what a raw score
IS; it changes how a caller INTERPRETS one, e.g. for the tiered cascade's
own tier-escalation decision ("is this confident enough to skip Tier 1?"),
by consulting a calibrated probability instead of comparing a raw cosine
similarity or softmax score against an arbitrary cutoff.

A missing/empty calibration map is never an error — apply_calibration()
falls back to returning the raw score unchanged, so a module that hasn't
been retrained-with-eval yet (no calibration data collected) behaves
exactly as if this module didn't exist.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CalibrationMap:
    # Parallel arrays: thresholds[i] is the largest raw score in bin i;
    # calibrated[i] is that bin's fitted P(correct). Both non-decreasing
    # by construction (that's the whole point of isotonic regression).
    thresholds: list[float] = field(default_factory=list)
    calibrated: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"thresholds": list(self.thresholds), "calibrated": list(self.calibrated)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationMap":
        return cls(thresholds=list(data.get("thresholds", [])), calibrated=list(data.get("calibrated", [])))


def fit_isotonic(pairs: list[tuple[float, bool]]) -> CalibrationMap:
    """Pool Adjacent Violators, unweighted: given (raw_confidence,
    was_correct) pairs from holdout evaluation, fits the best-fitting
    non-decreasing step function from raw_confidence to P(correct) —
    the standard isotonic-regression solution under squared error.

    Algorithm: sort by raw score, treat every point as its own bin
    (level = 0 or 1), then repeatedly merge a bin into its predecessor
    whenever the predecessor's level exceeds it (a "violation" of
    monotonicity), replacing both with one bin at their pooled mean.
    Merging can cascade backward, hence the while loop re-checking the
    new top-of-stack against ITS predecessor after every merge."""
    if not pairs:
        return CalibrationMap()

    ordered = sorted(pairs, key=lambda p: p[0])
    # Each stack entry: [sum_of_y, count, level(=mean), max_x_in_bin]
    blocks: list[list[float]] = []
    for x, y in ordered:
        y_val = 1.0 if y else 0.0
        blocks.append([y_val, 1.0, y_val, x])
        while len(blocks) > 1 and blocks[-2][2] > blocks[-1][2]:
            b2 = blocks.pop()
            b1 = blocks.pop()
            sum_y = b1[0] + b2[0]
            count = b1[1] + b2[1]
            blocks.append([sum_y, count, sum_y / count, b2[3]])

    thresholds = [b[3] for b in blocks]
    calibrated = [b[2] for b in blocks]
    return CalibrationMap(thresholds=thresholds, calibrated=calibrated)


def apply_calibration(calib: CalibrationMap, raw_score: float) -> float:
    """Piecewise-constant lookup: the calibrated value of whichever bin
    raw_score falls into (the first bin whose threshold is >= raw_score;
    scores above every fitted threshold use the highest bin — isotonic
    regression can't extrapolate beyond its training range, so the
    edge value is the most defensible estimate). Returns raw_score
    unchanged if calib has no data at all — never crashes, never blocks
    classification on calibration having been run."""
    if not calib.thresholds:
        return raw_score
    idx = bisect.bisect_left(calib.thresholds, raw_score)
    if idx >= len(calib.thresholds):
        idx = len(calib.thresholds) - 1
    return calib.calibrated[idx]
