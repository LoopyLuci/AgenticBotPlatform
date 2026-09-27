"""Dashboard routes: delegation.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from fastapi import Body, Depends, FastAPI

from bot import db
from bot.config import config


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key

    # Cross-instance calls (ask_instance, dispatch_swarm_goal,
    # delegate_to_instance) all write a "source -> target: prompt" row to
    # audit_log alongside their own jobs-table entry — this surfaces just
    # those three action kinds, newest first, so the dashboard can show
    # "who asked whom to do what" without wading through every other
    # audit event (config changes, snapshots, mcp registration, etc.).
    _DELEGATION_ACTIONS = ["agent_ask", "swarm_dispatch", "agent_delegate", "swarm_dispatch_blocked"]

    @app.get("/api/delegation-activity", dependencies=[Depends(_require_token_or_api_key)])
    def api_delegation_activity(limit: int = 20):
        rows = db.list_audit_log(actions=_DELEGATION_ACTIONS, limit=max(1, min(limit, 200)))
        return {
            "events": [
                {
                    "id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                    "detail": r["detail"], "job_id": r["job_id"],
                }
                for r in rows
            ]
        }

    @app.get("/api/jobs/{job_id}/tool-events", dependencies=[Depends(_require_token_or_api_key)])
    def api_job_tool_events(job_id: int):
        """Live top-level delegate_task tool_started/tool_completed events
        recorded for this job by bot/swarm/observability.py — see that
        module's docstring for why this is top-level only, never
        per-child."""
        events = db.list_job_tool_events(job_id)
        return {"events": [dict(e) for e in events]}

    @app.get("/api/jobs/{job_id}/children", dependencies=[Depends(_require_token_or_api_key)])
    def api_job_children(job_id: int):
        """Post-hoc per-child breakdown parsed from this job's own final
        reply — see bot/swarm/child_parser.py. Empty until the dispatch
        completes and its reply actually included the structured block."""
        children = db.list_job_children(job_id)
        return {"children": [dict(c) for c in children]}

    @app.get("/api/swarm-budget", dependencies=[Depends(_require_token_or_api_key)])
    async def api_get_swarm_budget():
        from bot import swarm_budget

        cfg = (config.current.get("swarm_budget") or {})
        return {
            "enabled": cfg.get("enabled", True),
            "max_children": cfg.get("max_children", swarm_budget.DEFAULT_MAX_CHILDREN),
            "max_estimated_usd": cfg.get("max_estimated_usd", swarm_budget.DEFAULT_MAX_ESTIMATED_USD),
            "require_confirm_above_usd": cfg.get(
                "require_confirm_above_usd", swarm_budget.DEFAULT_REQUIRE_CONFIRM_ABOVE_USD
            ),
            "deny_unpriced_paid_models": cfg.get("deny_unpriced_paid_models", False),
        }

    @app.post("/api/swarm-budget", dependencies=[Depends(_require_token_or_api_key)])
    async def api_set_swarm_budget(payload: dict = Body(...)):
        fields = (
            "enabled", "max_children", "max_estimated_usd",
            "require_confirm_above_usd", "deny_unpriced_paid_models",
        )
        for field in fields:
            if payload.get(field) is not None:
                config.set_value(["swarm_budget", field], payload[field], actor="dashboard")
        return await api_get_swarm_budget()
