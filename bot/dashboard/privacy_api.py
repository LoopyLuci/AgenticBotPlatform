"""Privacy mode over HTTP (bot/privacy.py): GET /api/privacy (the switch, and which backends would still answer)
and PUT /api/privacy {enabled?, allow_lan?}."""
from __future__ import annotations

from typing import Callable

from fastapi import Body, Depends, FastAPI

from bot import privacy


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]

    @app.get("/api/privacy", dependencies=dep)
    async def privacy_get():
        return {**privacy.settings(), "cloud_backends": sorted(privacy.CLOUD_BACKENDS),
                "network_tools": sorted(privacy.NETWORK_TOOLS)}

    @app.put("/api/privacy", dependencies=dep)
    async def privacy_put(body: dict = Body(...)):
        return privacy.set_settings(enabled=body.get("enabled"), allow_lan=body.get("allow_lan"))
