"""Pre-dispatch cost ceiling for dispatch_swarm_goal — a runaway-swarm
guard, not a real spend tracker.

bot/backends/hermes_gateway_backend.py never populates
BackendResult.tokens (confirmed by reading its ask() implementation), so
there is no real post-hoc token/cost figure to reconcile against for a
Hermes-gateway dispatch. What this module CAN do, honestly: estimate a
worst-case cost from already-computed per-model pricing
(bot.models.hermes_models_with_pricing) and the dispatch's own
max_children cap, and refuse to configure/send a dispatch whose estimate
exceeds a configured ceiling. This is a pre-flight gate, not a live
spend meter.

Config-driven, same style as bot/agent_control.py — a plain dict read via
config.current.get("swarm_budget", {}).get(key, default), no schema
enforcement, hot-reloads automatically via the existing ConfigManager.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_MAX_CHILDREN = 6
DEFAULT_MAX_ESTIMATED_USD = 0.25
DEFAULT_REQUIRE_CONFIRM_ABOVE_USD = 1.0
DEFAULT_ASSUMED_TOKENS = {"input": 2000, "output": 1000}


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str
    estimated_usd: Optional[float]


def estimate_dispatch_cost(
    pricing_row: Optional[dict],
    max_children: int,
    assumed_tokens: dict,
) -> Optional[float]:
    """Worst-case dollar estimate for a dispatch: max_children children,
    each assumed to use assumed_tokens input+output, at pricing_row's
    per-token cost. Returns None when pricing is unavailable for the
    resolved model (can't estimate what we don't know the price of), and
    0.0 when the model is marked free."""
    if not pricing_row:
        return None
    if pricing_row.get("free"):
        return 0.0
    input_cost = pricing_row.get("input")
    output_cost = pricing_row.get("output")
    if input_cost is None or output_cost is None:
        return None
    per_child = assumed_tokens["input"] * input_cost + assumed_tokens["output"] * output_cost
    return max(0, max_children) * per_child


def check_budget(
    *,
    pricing_row: Optional[dict],
    max_children: Optional[int],
    confirm: bool,
    cfg: dict[str, Any],
) -> BudgetDecision:
    """Evaluates a proposed dispatch against config["swarm_budget"] (the
    caller passes that sub-dict directly, already resolved from
    config.current — see bot/dashboard/server.py's api_hermes_dispatch
    and bot/mcp_server.py's dispatch_swarm_goal, the two call sites).

    Hard refusals (never overridable by confirm=True, only by editing
    config): too many children, or an unpriced paid model when
    deny_unpriced_paid_models is set, or the estimate exceeding the hard
    max_estimated_usd ceiling. Soft refusal (overridable by confirm=True,
    mirroring configure_delegation's existing subagent_auto_approve
    confirm-flag pattern): the estimate exceeding
    require_confirm_above_usd but still under the hard ceiling."""
    if not cfg.get("enabled", True):
        return BudgetDecision(True, "swarm budget guard disabled", None)

    cap_children = cfg.get("max_children", DEFAULT_MAX_CHILDREN)
    effective_children = max_children if max_children is not None else cap_children
    if effective_children > cap_children:
        return BudgetDecision(
            False,
            f"requested max_children={effective_children} exceeds swarm_budget.max_children={cap_children}",
            None,
        )

    deny_unpriced = cfg.get("deny_unpriced_paid_models", False)
    assumed_tokens = cfg.get("assumed_tokens_per_child", DEFAULT_ASSUMED_TOKENS)
    estimated = estimate_dispatch_cost(pricing_row, effective_children, assumed_tokens)

    if estimated is None:
        if deny_unpriced and not (pricing_row and pricing_row.get("free")):
            return BudgetDecision(
                False,
                "resolved model has no known pricing and deny_unpriced_paid_models is set — "
                "refusing to dispatch against an unknown cost",
                None,
            )
        return BudgetDecision(True, "model pricing unavailable but deny_unpriced_paid_models is off", None)

    hard_cap = cfg.get("max_estimated_usd", DEFAULT_MAX_ESTIMATED_USD)
    if estimated > hard_cap:
        return BudgetDecision(
            False,
            f"estimated worst-case cost ${estimated:.4f} exceeds swarm_budget.max_estimated_usd=${hard_cap}",
            estimated,
        )

    confirm_above = cfg.get("require_confirm_above_usd", DEFAULT_REQUIRE_CONFIRM_ABOVE_USD)
    if estimated > confirm_above and not confirm:
        return BudgetDecision(
            False,
            f"estimated worst-case cost ${estimated:.4f} exceeds swarm_budget.require_confirm_above_usd="
            f"${confirm_above} — pass confirm=true to proceed anyway",
            estimated,
        )

    return BudgetDecision(True, "within budget", estimated)
