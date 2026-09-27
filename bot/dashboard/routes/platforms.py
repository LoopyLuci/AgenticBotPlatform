"""Dashboard routes: platforms.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from fastapi import Body, Depends, FastAPI

from bot import db, setup_wizard


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key, _require_token_or_bootstrap

    # Reachable any time from Settings, not gated to first-run like the
    # core wizard above — same bootstrap rule though, since a fresh install
    # may want to set up a platform before DASHBOARD_TOKEN even exists.

    @app.get("/api/platforms/status", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_platforms_status():
        return setup_wizard.platform_status()

    @app.post("/api/platforms/apply", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_platforms_apply(payload: dict = Body(...)):
        backup, status = setup_wizard.apply_platform_fields(payload, actor="platforms-settings")
        return {"ok": True, "backup": backup.name if backup else None, "status": status}

    @app.get("/api/security/allowed-users", dependencies=[Depends(_require_token_or_api_key)])
    def api_allowed_users():
        return [dict(r) for r in db.list_allowed_users()]
