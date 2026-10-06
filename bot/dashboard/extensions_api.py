"""Extensions API: what this install added on top of the public ABP.

    GET  /api/extensions             every loaded extension, every panel, and what failed
    GET  /extensions/<name>/<file>   a panel's own script/stylesheet, served from the extension's folder

The second one is a plain file route rather than a StaticFiles mount because the folders are
discovered at runtime: there is one per extension and ABP has no idea how many until it looks.
Paths are resolved inside the extension's own folder and anything that escapes it is a 404, so
an extension can serve its own assets and nothing else.

Both UIs read /api/extensions on load and build the navigation from it (the sections, nav
entries and <script> tags for a panel that isn't in either HTML file), which is what lets a
panel exist without a line of it being committed here.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException

from bot import extensions

logger = logging.getLogger(__name__)

_MEDIA_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
                ".json": "application/json", ".svg": "image/svg+xml", ".map": "application/json",
                ".png": "image/png", ".woff2": "font/woff2"}


def register(app: FastAPI, require_token: Callable) -> None:
    from fastapi.responses import FileResponse, PlainTextResponse

    @app.get("/api/extensions", dependencies=[Depends(require_token)])
    async def extensions_list():
        loaded = extensions.loaded()
        return {**loaded.describe(), "panels": extensions.panels()}

    @app.get("/extensions/{name}/{path:path}", response_class=FileResponse)
    async def extension_file(name: str, path: str):
        ext = extensions.loaded().by_name(name)
        if ext is None:
            raise HTTPException(status_code=404, detail="no such extension")
        target = (ext.directory / path).resolve()
        # An extension serves its own folder and nothing else: "..", a symlink out of it or an
        # absolute path all land outside directory, and that is a 404 rather than a read.
        if not str(target).startswith(str(ext.directory.resolve())) or not target.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        media = _MEDIA_TYPES.get(target.suffix.lower())
        if media is None:
            return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))
        return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache"})