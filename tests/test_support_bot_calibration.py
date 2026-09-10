"""bot/support_bot/calibration.py — pure-Python isotonic confidence
calibration (Pool Adjacent Violators). Phase 3 of the next-generation
modular hybrid plan.
"""
from __future__ import annotations

from bot.support_bot.calibration import CalibrationMap, apply_calibration, fit_isotonic


def test_fit_isotonic_on_empty_input_returns_empty_map():
    calib = fit_isotonic([])
    assert calib.thresholds == []
    assert calib.calibrated == []


def test_apply_calibration_on_empty_map_passes_through_unchanged():
    calib = CalibrationMap()
    assert apply_calibration(calib, 0.42) == 0.42


def test_fit_isotonic_perfectly_separable_data():
    # Every low-confidence prediction is wrong, every high-confidence
    # prediction is right — a clean monotonic step should emerge.
    pairs = [(0.1, False), (0.2, False), (0.3, False), (0.8, True), (0.9, True), (0.95, True)]
    calib = fit_isotonic(pairs)
    assert apply_calibration(calib, 0.1) == 0.0
    assert apply_calibration(calib, 0.3) == 0.0
    assert apply_calibration(calib, 0.9) == 1.0


def test_fit_isotonic_output_is_monotonically_non_decreasing():
    pairs = [(0.1, True), (0.2, False), (0.3, True), (0.4, False), (0.5, True), (0.9, True)]
    calib = fit_isotonic(pairs)
    assert calib.calibrated == sorted(calib.calibrated)
    # thresholds must also be sorted (they're bin boundaries)
    assert calib.thresholds == sorted(calib.thresholds)


def test_fit_isotonic_pools_a_local_violation():
    # (0.1, True) then (0.2, False) is a genuine inversion (level would
    # have to DECREASE from 1.0 to 0.0) — PAV must pool the two points
    # into one bin at their mean (0.5), not leave a non-monotonic 1.0/0.0.
    pairs = [(0.1, True), (0.2, False)]
    calib = fit_isotonic(pairs)
    assert apply_calibration(calib, 0.1) == 0.5
    assert apply_calibration(calib, 0.2) == 0.5


def test_apply_calibration_above_every_threshold_uses_the_top_bin():
    calib = fit_isotonic([(0.1, False), (0.5, True)])
    assert apply_calibration(calib, 0.99) == apply_calibration(calib, 0.5)


def test_apply_calibration_below_every_threshold_uses_the_bottom_bin():
    calib = fit_isotonic([(0.1, False), (0.5, True)])
    assert apply_calibration(calib, 0.0) == apply_calibration(calib, 0.1)


def test_calibration_map_round_trips_through_dict():
    calib = fit_isotonic([(0.1, False), (0.2, False), (0.8, True)])
    restored = CalibrationMap.from_dict(calib.to_dict())
    assert restored.thresholds == calib.thresholds
    assert restored.calibrated == calib.calibrated


def test_calibration_map_from_dict_handles_missing_keys():
    restored = CalibrationMap.from_dict({})
    assert restored.thresholds == []
    assert restored.calibrated == []
