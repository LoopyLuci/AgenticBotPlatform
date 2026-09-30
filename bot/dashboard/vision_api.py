"""/api/vision: computer vision on this machine (bot/vision), for the Vision page, the CLI and MCP clients.

    GET  /api/vision                                   device, models, tasks, edits
    POST /api/vision/analyze  {image, tasks?, min_score?, classes?}
    POST /api/vision/find     {image?, text? | template?, threshold?}      (image defaults to the screen)
    POST /api/vision/compare  {before, after}
    POST /api/vision/edit     {image, steps}
    POST /api/vision/faces    {a, b}
    POST /api/vision/models/{model}/fetch               download and verify one zoo model
    GET  /api/vision/out/{name}                         a result picture (data/vision/out only)

`image` is a file path, an http(s) URL, a data: URL (the page uploads that way), "screen" / "screen:<n>", or
"camera" / "camera:<n>" (here the owner asked for it on the page; agents reach the camera only through vision_capture).
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from bot.vision import images, service, zoo
from bot.vision.images import VisionError


async def _v(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except VisionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _with_url(res: dict, inline: bool = False) -> dict:
    """Result pictures are also offered by URL, and with inline=true as a data: URL (at most 1600 px on a side), which
    the page shows directly (an <img> cannot send the dashboard token)."""
    for key in ("annotated", "path"):
        p = res.get(key)
        if isinstance(p, str) and p.startswith(str(images.out_dir())):
            res[key + "_url"] = "/api/vision/out/" + p.replace("\\", "/").rsplit("/", 1)[-1]
            if inline:
                img = images.load(p)[0]
                res[key + "_data"] = images.data_url(img, 1600)
    return res


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]

    @app.get("/api/vision", dependencies=dep)
    async def vision_status():
        return await _v(service.status)

    @app.post("/api/vision/analyze", dependencies=dep)
    async def vision_analyze(body: dict = Body(...)):
        return await asyncio.to_thread(_with_url, await _v(service.analyze, body.get("image"), body.get("tasks"),
                                  min_score=float(body.get("min_score") or 0.4), classes=body.get("classes")), bool(body.get("inline")))

    @app.post("/api/vision/find", dependencies=dep)
    async def vision_find(body: dict = Body(...)):
        return await asyncio.to_thread(_with_url, await _v(service.find, body.get("image") or "screen", text=str(body.get("text") or ""),
                                  template=body.get("template"), threshold=float(body.get("threshold") or 0.8)), bool(body.get("inline")))

    @app.post("/api/vision/compare", dependencies=dep)
    async def vision_compare(body: dict = Body(...)):
        return await asyncio.to_thread(_with_url, await _v(service.compare, body.get("before"), body.get("after")), bool(body.get("inline")))

    @app.post("/api/vision/edit", dependencies=dep)
    async def vision_edit(body: dict = Body(...)):
        return await asyncio.to_thread(_with_url, await _v(service.edit, body.get("image"), body.get("steps") or []), bool(body.get("inline")))

    @app.post("/api/vision/faces", dependencies=dep)
    async def vision_faces(body: dict = Body(...)):
        return await _v(service.faces_match, body.get("a"), body.get("b"))

    @app.post("/api/vision/models/{model}/fetch", dependencies=dep)
    async def vision_fetch(model: str):
        return await _v(zoo.fetch, model)

    @app.get("/api/vision/out/{name}", dependencies=dep)
    async def vision_out(name: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name) or ".." in name:
            raise HTTPException(status_code=400, detail="bad name")
        p = images.out_dir() / name
        if not p.is_file():
            raise HTTPException(status_code=404, detail="no such result")
        return FileResponse(p)
