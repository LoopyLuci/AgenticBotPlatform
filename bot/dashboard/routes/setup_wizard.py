"""Dashboard routes: setup wizard.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Body, Depends, FastAPI

from bot import desktop, setup_wizard


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token, _require_token_or_bootstrap

    # Same bootstrap rule as the env editor above: this is the thing that
    # sets up the first working .env, so it can't itself require one.

    @app.get("/api/setup/status", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_setup_status():
        return setup_wizard.check_status()

    @app.get("/api/setup/detect-desktop", dependencies=[Depends(_require_token_or_bootstrap)])
    def api_setup_detect_desktop():
        path = desktop.find_exe_path()
        return {"path": path, "exists": bool(path and Path(path).exists())}

    @app.post("/api/setup/apply", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_setup_apply(payload: dict = Body(...)):
        backup, status = setup_wizard.apply_setup(payload, actor="setup-wizard")
        return {"ok": True, "backup": backup.name if backup else None, "status": status}

    @app.post("/api/setup/install-cli", dependencies=[Depends(_require_token)])
    async def api_setup_install_cli():
        # npm install can take a while — run off the event loop so it
        # doesn't stall every other dashboard request (jobs polling, etc.)
        # for the duration.
        result = await asyncio.to_thread(desktop.install_cli, actor="dashboard")
        return result
