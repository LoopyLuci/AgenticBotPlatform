"""Phase 9 of the Hermes-swarm plan: the pre-dispatch cost-ceiling guard.

Pure unit tests against bot/swarm_budget.py's functions — no DB, no HTTP,
no live pricing calls. Route-level wiring (the actual refusal reaching
/api/hermes/{id}/dispatch) is covered in test_hermes_swarm_routes.py.
"""

from __future__ import annotations

from bot.swarm_budget import check_budget, estimate_dispatch_cost

FREE_ROW = {"id": "free-model", "free": True, "input": None, "output": None}
PAID_ROW = {"id": "paid-model", "free": False, "input": 0.000001, "output": 0.000002}
DEFAULT_CFG = {
    "enabled": True,
    "max_children": 6,
    "max_estimated_usd": 0.25,
    "require_confirm_above_usd": 1.0,
    "deny_unpriced_paid_models": False,
    "assumed_tokens_per_child": {"input": 2000, "output": 1000},
}


def test_estimate_free_model_is_zero():
    assert estimate_dispatch_cost(FREE_ROW, max_children=6, assumed_tokens={"input": 2000, "output": 1000}) == 0.0


def test_estimate_unpriced_returns_none():
    assert estimate_dispatch_cost(None, max_children=6, assumed_tokens={"input": 2000, "output": 1000}) is None
    row_missing_price = {"id": "x", "free": False, "input": None, "output": None}
    assert estimate_dispatch_cost(row_missing_price, max_children=6, assumed_tokens={"input": 2000, "output": 1000}) is None


def test_estimate_paid_model_scales_with_children():
    tokens = {"input": 2000, "output": 1000}
    one_child = estimate_dispatch_cost(PAID_ROW, max_children=1, assumed_tokens=tokens)
    six_children = estimate_dispatch_cost(PAID_ROW, max_children=6, assumed_tokens=tokens)
    assert one_child == 2000 * 0.000001 + 1000 * 0.000002
    assert six_children == one_child * 6


def test_check_budget_allows_free_model():
    decision = check_budget(pricing_row=FREE_ROW, max_children=6, confirm=False, cfg=DEFAULT_CFG)
    assert decision.allowed
    assert decision.estimated_usd == 0.0


def test_check_budget_disabled_always_allows():
    decision = check_budget(pricing_row=None, max_children=999, confirm=False, cfg={"enabled": False})
    assert decision.allowed


def test_check_budget_refuses_too_many_children():
    decision = check_budget(pricing_row=FREE_ROW, max_children=99, confirm=False, cfg=DEFAULT_CFG)
    assert not decision.allowed
    assert "max_children" in decision.reason


def test_check_budget_allows_unpriced_by_default():
    decision = check_budget(pricing_row=None, max_children=6, confirm=False, cfg=DEFAULT_CFG)
    assert decision.allowed
    assert decision.estimated_usd is None


def test_check_budget_denies_unpriced_when_configured():
    cfg = dict(DEFAULT_CFG, deny_unpriced_paid_models=True)
    decision = check_budget(pricing_row=None, max_children=6, confirm=False, cfg=cfg)
    assert not decision.allowed
    assert "no known pricing" in decision.reason


def test_check_budget_denies_unpriced_free_flag_still_allowed():
    # A row that IS marked free but happens to have no input/output fields
    # (Hermes's own inventory sometimes omits pricing for a free entry)
    # must still be treated as free, not as "unpriced paid".
    cfg = dict(DEFAULT_CFG, deny_unpriced_paid_models=True)
    decision = check_budget(pricing_row=FREE_ROW, max_children=6, confirm=False, cfg=cfg)
    assert decision.allowed


def test_check_budget_soft_refusal_needs_confirm():
    # A paid model priced high enough to land between require_confirm_above_usd
    # and max_estimated_usd.
    pricey_row = {"id": "mid", "free": False, "input": 0.00003, "output": 0.00003}
    cfg = dict(DEFAULT_CFG, max_estimated_usd=10.0, require_confirm_above_usd=0.01)
    decision = check_budget(pricing_row=pricey_row, max_children=6, confirm=False, cfg=cfg)
    assert not decision.allowed
    assert "confirm=true" in decision.reason

    confirmed = check_budget(pricing_row=pricey_row, max_children=6, confirm=True, cfg=cfg)
    assert confirmed.allowed


def test_check_budget_hard_cap_not_overridable_by_confirm():
    very_pricey_row = {"id": "big", "free": False, "input": 1.0, "output": 1.0}
    decision = check_budget(pricing_row=very_pricey_row, max_children=6, confirm=True, cfg=DEFAULT_CFG)
    assert not decision.allowed
    assert "max_estimated_usd" in decision.reason


def test_check_budget_defaults_max_children_to_config_cap_when_unspecified():
    decision = check_budget(pricing_row=FREE_ROW, max_children=None, confirm=False, cfg=DEFAULT_CFG)
    assert decision.allowed
