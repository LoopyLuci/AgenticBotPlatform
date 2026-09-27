"""Dashboard routes: agent control.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import agent_control, db
from bot.router import router


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token

    # Lets one bot instance's own AI session ask another instance a one-off
    # question via router.ask(), subject to agent_control's trust_all/
    # allowlist toggle. source_instance is self-declared by the caller (see
    # bot/agent_control.py's module docstring) — not cryptographically
    # verified, an accepted tradeoff for this single-operator app.

    @app.post("/api/agent/ask", dependencies=[Depends(_require_token)])
    async def api_agent_ask(payload: dict = Body(...)):
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="payload must be {source_instance, target_instance, prompt}")
        source = agent_control.resolve_instance(payload.get("source_instance"))
        if source is None:
            raise HTTPException(status_code=404, detail=f"source instance {payload.get('source_instance')!r} not found")
        target = agent_control.resolve_instance(payload.get("target_instance"))
        if target is None:
            raise HTTPException(status_code=404, detail=f"target instance {payload.get('target_instance')!r} not found")
        if not agent_control.can_target(source["id"], target["id"]):
            raise HTTPException(
                status_code=403,
                detail=f"{source['name']} is not permitted to target {target['name']} under the current allowlist",
            )
        db.log_audit(
            actor=f"agent:{source['name']}", action="agent_ask",
            detail=f"-> {target['name']}: {prompt[:120]}",
        )
        try:
            result = await router.ask(prompt, action_type="agent_relay", instance_id=target["id"])
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"ask failed: {exc}") from exc
        return {"ok": True, "result": result.text}
