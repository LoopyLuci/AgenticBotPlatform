"""Likeness (Video Studio V3): face swap, recast and characters of real people, under consent (bot/likeness).

All routes need the dashboard token; every change and every job is audited.
    GET    /api/likeness                            settings, models, consents, characters, jobs, recent log
    PUT    /api/likeness/settings                   {"enabled"?: bool, "max_seconds"?: 5..600} (audited)
    POST   /api/likeness/uploads                    multipart file -> {"id": "upload:..."} (an image or a video)
    POST   /api/likeness/consents                   {name, kind: self|consented, attested_by, adult: true, statement,
                                                     scope?, images: [1..8 image refs]}
    POST   /api/likeness/consents/{id}/revoke       {reason?}
    POST   /api/likeness/jobs                       {mode: faceswap|recast, consent_id, source, target, purpose,
                                                     target_face?: best|largest|all, target_reference?}
    GET    /api/likeness/jobs/{id}
    POST   /api/likeness/characters                 {name, consent_id, trigger?, images: [1..40]}
    POST   /api/likeness/characters/{id}/dataset    build its LoRA dataset
    GET    /api/likeness/out/{name}                 an output or its .provenance.json
A refusal (policy) answers 403 with the reason."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse


def register(app: FastAPI, require_token: Callable) -> None:
    from bot.likeness import jobs, models, policy, store

    dep = [Depends(require_token)]

    async def _call(fn, *a):
        try:
            return await asyncio.to_thread(fn, *a)
        except policy.Refused as e:
            raise HTTPException(status_code=403, detail=f"refused: {e}") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/likeness", dependencies=dep)
    async def likeness_status(limit: int = Query(30, ge=1, le=500)):
        return {"settings": jobs.settings(), "models": models.status(), "consents": store.consents(),
                "characters": store.characters(), "jobs": jobs.jobs()[:50], "log": store.job_log(limit)}

    @app.put("/api/likeness/settings", dependencies=dep)
    def likeness_settings(body: dict = Body(...)):
        """Switch likeness on or off and set the video length cap (from the Workspace; audited)."""
        from bot import db
        from bot.config import config

        changes: dict = {}
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise HTTPException(status_code=400, detail="enabled must be true or false")
            changes[("likeness", "enabled")] = body["enabled"]
        if "max_seconds" in body:
            v = body["max_seconds"]
            if isinstance(v, bool) or not isinstance(v, int) or not 5 <= v <= 600:
                raise HTTPException(status_code=400, detail="max_seconds must be whole seconds from 5 to 600")
            changes[("likeness", "max_seconds")] = v
        if not changes:
            raise HTTPException(status_code=400, detail="send enabled and/or max_seconds")
        config.set_values(changes, actor="dashboard")
        db.log_audit(actor="dashboard", action="likeness_settings_set",
                     detail=", ".join(f"{k[-1]}={v}" for k, v in changes.items()))
        return jobs.settings() | {k[-1]: v for k, v in changes.items()}

    @app.post("/api/likeness/uploads", dependencies=dep)
    async def likeness_upload(file: UploadFile = File(...)):
        data = await file.read(jobs.UPLOAD_MAX + 1)
        return await _call(jobs.save_upload, file.filename or "", data)

    @app.post("/api/likeness/consents", dependencies=dep)
    async def likeness_consent_add(body: dict = Body(...)):
        return await _call(jobs.create_consent, body)

    @app.post("/api/likeness/consents/{cid}/revoke", dependencies=dep)
    async def likeness_consent_revoke(cid: str, body: dict = Body(default={})):
        c = await _call(jobs.revoke_consent, cid, str((body or {}).get("reason") or ""))
        if not c:
            raise HTTPException(status_code=404, detail="no such consent record")
        return c

    @app.post("/api/likeness/jobs", dependencies=dep)
    async def likeness_job_submit(body: dict = Body(...)):
        return await _call(jobs.submit, body)

    @app.get("/api/likeness/jobs/{job_id}", dependencies=dep)
    async def likeness_job(job_id: str):
        j = jobs.job(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="no such job")
        return j

    @app.post("/api/likeness/characters", dependencies=dep)
    async def likeness_character_add(body: dict = Body(...)):
        return await _call(jobs.create_character, body)

    @app.post("/api/likeness/characters/{chid}/dataset", dependencies=dep)
    async def likeness_character_dataset(chid: str):
        return await _call(jobs.build_dataset, chid)

    @app.get("/api/likeness/out/{name}", dependencies=dep)
    async def likeness_out(name: str):
        p = jobs.output_path(name)
        if p is None:
            raise HTTPException(status_code=404, detail="no such output")
        return FileResponse(p)
