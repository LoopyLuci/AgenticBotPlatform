"""Editor integrations API (bot/editor_integrations.py): the ABP Agents page's Editors tab.

    GET  /api/editors/status           VS Code found?, the bundled and installed extension versions, the ACP command
    POST /api/editors/vscode/install   install or update the VS Code extension

Reading uses the dashboard's normal auth. Installing changes this computer's VS Code, so it needs the
desktop dashboard token."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import editor_integrations as editors

    @app.get("/api/editors/status", dependencies=[Depends(read_auth)])
    async def editors_status():
        return await asyncio.to_thread(editors.status)

    @app.post("/api/editors/vscode/install", dependencies=[Depends(write_auth)])
    async def editors_install_vscode():
        try:
            return await asyncio.to_thread(editors.install_vscode)
        except editors.EditorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
