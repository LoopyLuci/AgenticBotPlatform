"""Video Studio (V1 engine, bot/video): the job queue, the library, workflows, models and settings.

All routes need the dashboard token; settings changes and every job are audited.
    GET    /api/video                              settings, queue, backends, ffmpeg, workflows, models, recent jobs/assets
    PUT    /api/video/settings                     any of jobs.DEFAULTS (audited)
    GET    /api/video/workflows                    the versioned templates with their typed parameters
    GET    /api/video/models                       the registry: each model's files and whether they are verified
    POST   /api/video/models/{id}/verify           hash the model's files (slow the first time)
    POST   /api/video/models/{id}/download         fetch missing files in the background (SHA-256 checked)
    GET    /api/video/jobs                         ?status=&limit=
    POST   /api/video/jobs                         {workflow?, params: {prompt, ...}, parent_id?, timeout_s?}
    GET    /api/video/jobs/{id}
    POST   /api/video/jobs/{id}/cancel
    GET    /api/video/assets                       ?limit=&offset=
    GET    /api/video/assets/{id}                  the asset, its lineage and children
    GET    /api/video/assets/{id}/file             the clip
    GET    /api/video/assets/{id}/thumbnail
    GET    /api/video/assets/{id}/provenance       sidecar + embedded manifests and lineage
    POST   /api/video/assets/{id}/derive           {op: trim|transcode|concat, args} -> a child asset (needs ffmpeg)"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

_downloads: dict[str, dict] = {}


def register(app: FastAPI, require_token: Callable) -> None:
    from bot.video import backends, jobs, library, media, registry, workflows
    from bot.video import views as tools

    dep = [Depends(require_token)]

    async def _call(fn, *a):
        try:
            return await asyncio.to_thread(fn, *a)
        except (ValueError, media.MediaError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def _asset(aid: str) -> dict:
        a = library.get(aid)
        if not a:
            raise HTTPException(status_code=404, detail="no such asset")
        return a

    @app.get("/api/video", dependencies=dep)
    async def video_status():
        def go():
            return {"settings": jobs.settings(), "queue": jobs.summary(), "backends": backends.listing(),
                    "ffmpeg": media.status(), "workflows": workflows.listing(), "models": registry.status(),
                    "downloads": dict(_downloads), "jobs": [tools.job_view(j) for j in jobs.jobs(30)],
                    "assets": [tools.asset_view(a) for a in library.listing(24)]}
        return await asyncio.to_thread(go)

    @app.put("/api/video/settings", dependencies=dep)
    def video_settings(body: dict = Body(...)):
        """Change Video Studio settings from the Workspace (audited)."""
        from bot import db
        from bot.config import config

        try:
            changes = jobs.check_settings(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        config.set_values({("video", k): v for k, v in changes.items()}, actor="dashboard")
        db.log_audit(actor="dashboard", action="video_settings_set",
                     detail=", ".join(f"{k}={v}" for k, v in changes.items()))
        if changes.get("enabled"):
            jobs.ensure_worker()
        return jobs.settings() | changes

    @app.get("/api/video/workflows", dependencies=dep)
    async def video_workflows():
        return workflows.listing()

    @app.get("/api/video/models", dependencies=dep)
    async def video_models():
        return await asyncio.to_thread(registry.status)

    @app.post("/api/video/models/{model_id}/verify", dependencies=dep)
    async def video_model_verify(model_id: str):
        return await _call(registry.verify, model_id)

    @app.post("/api/video/models/{model_id}/download", dependencies=dep)
    async def video_model_download(model_id: str):
        from bot import db

        await _call(registry.get, model_id)
        d = await _call(registry.disk, model_id)
        if not d["fits"]:
            raise HTTPException(status_code=400, detail=f"{d['dir']} lacks the space for {model_id}")
        if (_downloads.get(model_id) or {}).get("state") == "running":
            return _downloads[model_id]
        st = _downloads[model_id] = {"state": "running", "file": "", "bytes": 0, "of": 0, "error": ""}

        def prog(name: str, n: int, total: int) -> None:
            st.update(file=name, bytes=n, of=total)

        def go() -> None:
            try:
                registry.download(model_id, progress=prog)
                st["state"] = "done"
            except Exception as exc:  # noqa: BLE001 - shown in the Workspace
                st.update(state="failed", error=str(exc))
        threading.Thread(target=go, name=f"video-dl-{model_id}", daemon=True).start()
        db.log_audit(actor="dashboard", action="video_model_download", detail=model_id)
        return st

    @app.get("/api/video/jobs", dependencies=dep)
    async def video_jobs(status: str = Query(""), limit: int = Query(50, ge=1, le=500)):
        return [tools.job_view(j) for j in await asyncio.to_thread(jobs.jobs, limit, status)]

    @app.post("/api/video/jobs", dependencies=dep)
    async def video_job_submit(body: dict = Body(...)):
        return tools.job_view(await _call(jobs.submit, body, "dashboard"))

    @app.get("/api/video/jobs/{job_id}", dependencies=dep)
    async def video_job(job_id: str):
        j = await asyncio.to_thread(jobs.job, job_id)
        if not j:
            raise HTTPException(status_code=404, detail="no such job")
        return tools.job_view(j)

    @app.post("/api/video/jobs/{job_id}/cancel", dependencies=dep)
    async def video_job_cancel(job_id: str):
        j = await asyncio.to_thread(jobs.cancel, job_id, "dashboard")
        if not j:
            raise HTTPException(status_code=404, detail="no such job")
        return tools.job_view(j)

    @app.get("/api/video/assets", dependencies=dep)
    async def video_assets(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        return [tools.asset_view(a) for a in await asyncio.to_thread(library.listing, limit, offset)]

    @app.get("/api/video/assets/{asset_id}", dependencies=dep)
    async def video_asset(asset_id: str):
        a = _asset(asset_id)
        return {"asset": tools.asset_view(a), "meta": a.get("meta"),
                "lineage": [tools.asset_view(x) for x in library.lineage(asset_id)[1:]],
                "children": [tools.asset_view(x) for x in library.children(asset_id)]}

    @app.get("/api/video/assets/{asset_id}/file", dependencies=dep)
    async def video_asset_file(asset_id: str):
        p = library.path_of(_asset(asset_id))
        if not p.is_file():
            raise HTTPException(status_code=404, detail="the asset's file is gone")
        return FileResponse(p)

    @app.get("/api/video/assets/{asset_id}/thumbnail", dependencies=dep)
    async def video_asset_thumbnail(asset_id: str):
        p = library.path_of(_asset(asset_id)).with_suffix(".jpg")
        if not p.is_file():
            raise HTTPException(status_code=404, detail="no thumbnail")
        return FileResponse(p)

    @app.get("/api/video/assets/{asset_id}/provenance", dependencies=dep)
    async def video_asset_provenance(asset_id: str):
        _asset(asset_id)
        return await _call(library.provenance_of, asset_id)

    @app.post("/api/video/assets/{asset_id}/derive", dependencies=dep)
    async def video_asset_derive(asset_id: str, body: dict = Body(...)):
        _asset(asset_id)
        a = await _call(library.derive, asset_id, str((body or {}).get("op") or ""), (body or {}).get("args") or {})
        return tools.asset_view(a)
