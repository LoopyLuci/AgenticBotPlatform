"""Unsloth API: ABP's page for Unsloth Studio.

    GET  /api/unsloth/status                  everything at a glance (running, GPUs, loaded models, training, storage)
    GET  /api/unsloth/models                  what it serves, what is cached, what is on disk
    GET  /api/unsloth/recommended             Studio's recommended models
    GET  /api/unsloth/variants?repo_id=       the GGUF files of a hub repo
    POST /api/unsloth/estimate                {"model", "variant", "context"} -> memory needed, and whether it fits
    POST /api/unsloth/download                {"repo_id", "variant"} (runs in Studio; poll download-status)
    GET  /api/unsloth/download-status?repo_id=&variant=
    GET  /api/unsloth/downloads               active downloads
    POST /api/unsloth/load                    {"model", "variant", "context", "options"} (waits until loaded)
    POST /api/unsloth/unload                  {"model"}
    GET  /api/unsloth/storage                 where models are downloaded, free space, scanned folders
    PUT  /api/unsloth/storage                 {"models_dir"}
    GET  /api/unsloth/train/schema            every training setting with its default
    POST /api/unsloth/train/start             {"fields": {...}}
    GET  /api/unsloth/train/status | /train/metrics | /train/runs
    POST /api/unsloth/train/stop
    POST /api/unsloth/export                  {"kind": "gguf|lora|merged|base", "fields": {...}}
    GET  /api/unsloth/export/status
    GET  /api/unsloth/operations              every operation Studio offers (its own OpenAPI description)
    POST /api/unsloth/call                    {"operation": id or "METHOD /path", "args": {...}}
    POST /api/unsloth/upload                  {"operation", "args", "files": {field: [{"name", "data" (base64)}]}}

Reading uses the dashboard's normal auth; anything that changes Studio needs the desktop dashboard token.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.unsloth import client, harness
    from bot.unsloth.client import StudioError

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def run(fn, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except StudioError as exc:
            code = 503 if "not running" in str(exc) or "could not reach" in str(exc) else (exc.status if exc.status and exc.status < 500 else 502)
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    def audit(action: str, detail: str) -> None:
        db.log_audit(actor="dashboard", action=f"unsloth_{action}", detail=detail[:500])

    @app.get("/api/unsloth/status", dependencies=read)
    async def unsloth_status():
        def both():
            full = harness.status()
            return {"summary": harness.summary(full), "full": full, "storage": full.get("storage") or {}}
        return await run(both)

    @app.get("/api/unsloth/models", dependencies=read)
    async def unsloth_models():
        return await run(harness.models)

    @app.get("/api/unsloth/recommended", dependencies=read)
    async def unsloth_recommended(limit: int = Query(60, ge=1, le=500)):
        return {"models": await run(harness.recommended, limit)}

    @app.get("/api/unsloth/variants", dependencies=read)
    async def unsloth_variants(repo_id: str):
        return {"repo_id": repo_id, "variants": await run(harness.variants, repo_id)}

    @app.post("/api/unsloth/estimate", dependencies=read)
    async def unsloth_estimate(payload: dict = Body(...)):
        return await run(harness.estimate, str(payload.get("model") or ""), variant=payload.get("variant") or None,
                         context=int(payload.get("context") or 0) or None)

    @app.post("/api/unsloth/download", dependencies=write)
    async def unsloth_download(payload: dict = Body(...)):
        repo = str(payload.get("repo_id") or "").strip()
        if not repo:
            raise HTTPException(status_code=400, detail="repo_id is required")
        out = await run(harness.download, repo, variant=payload.get("variant") or None, wait=False)
        audit("download", f"{repo} {payload.get('variant') or ''}")
        return out

    @app.get("/api/unsloth/download-status", dependencies=read)
    async def unsloth_download_status(repo_id: str, variant: Optional[str] = None):
        return await run(harness.download_status, repo_id, variant=variant)

    @app.get("/api/unsloth/downloads", dependencies=read)
    async def unsloth_downloads():
        return await run(client.request, "GET", "/api/hub/active-downloads")

    @app.post("/api/unsloth/load", dependencies=write)
    async def unsloth_load(payload: dict = Body(...)):
        model = str(payload.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        options = payload.get("options") if isinstance(payload.get("options"), dict) else None
        out = await run(harness.load, model, variant=payload.get("variant") or None, context=int(payload.get("context") or 0), options=options)
        audit("load", model)
        return out

    @app.post("/api/unsloth/unload", dependencies=write)
    async def unsloth_unload(payload: dict = Body(...)):
        model = str(payload.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        out = await run(harness.unload, model)
        audit("unload", model)
        return out

    @app.get("/api/unsloth/storage", dependencies=read)
    async def unsloth_storage():
        return await run(harness.storage)

    @app.put("/api/unsloth/storage", dependencies=write)
    async def unsloth_set_storage(payload: dict = Body(...)):
        out = await run(harness.set_models_dir, str(payload.get("models_dir") or ""))
        audit("storage", str(payload.get("models_dir")))
        return out

    @app.get("/api/unsloth/train/schema", dependencies=read)
    async def unsloth_train_schema():
        op = await run(client.find_operation, "POST /api/train/start")
        return {"schema": op["body"]}

    @app.post("/api/unsloth/train/start", dependencies=write)
    async def unsloth_train_start(payload: dict = Body(...)):
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        out = await run(harness.train_start, fields)
        audit("train_start", str(fields.get("model_name")))
        return out

    @app.get("/api/unsloth/train/status", dependencies=read)
    async def unsloth_train_status():
        return await run(harness.train_status)

    @app.get("/api/unsloth/train/metrics", dependencies=read)
    async def unsloth_train_metrics():
        return await run(harness.train_metrics)

    @app.get("/api/unsloth/train/runs", dependencies=read)
    async def unsloth_train_runs():
        return await run(harness.train_runs)

    @app.post("/api/unsloth/train/stop", dependencies=write)
    async def unsloth_train_stop():
        out = await run(harness.train_stop)
        audit("train_stop", "")
        return out

    @app.post("/api/unsloth/export", dependencies=write)
    async def unsloth_export(payload: dict = Body(...)):
        kind = str(payload.get("kind") or "gguf")
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        out = await run(harness.export, kind, fields)
        audit("export", f"{kind} -> {fields.get('save_directory') or fields.get('repo_id')}")
        return out

    @app.get("/api/unsloth/export/status", dependencies=read)
    async def unsloth_export_status():
        return await run(harness.export_status)

    @app.get("/api/unsloth/operations", dependencies=read)
    async def unsloth_operations():
        ops = await run(client.operations)
        return {"operations": ops, "count": len(ops)}

    @app.post("/api/unsloth/upload", dependencies=write)
    async def unsloth_upload(payload: dict = Body(...)):
        """An upload operation: {"operation", "args", "files": {field: [{"name", "data"}]}} with data base64."""
        import base64
        import binascii

        ref = str(payload.get("operation") or "").strip()
        files_in = payload.get("files") if isinstance(payload.get("files"), dict) else {}
        try:
            files = {field: [(str(f.get("name") or "upload"), base64.b64decode(str(f.get("data") or ""), validate=True))
                             for f in (items if isinstance(items, list) else [items]) if isinstance(f, dict)]
                     for field, items in files_in.items()}
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"a file is not valid base64: {exc}") from exc
        if not ref or not any(files.values()):
            raise HTTPException(status_code=400, detail="operation and at least one file are required")
        args = dict(payload.get("args") or {}) if isinstance(payload.get("args"), dict) else {}
        args["files"] = files
        result = await run(client.call, ref, args, timeout=float(payload.get("timeout_s") or 900))
        audit("upload", f"{ref}: {sum(len(v) for v in files.values())} file(s)")
        return {"operation": ref, "result": result}

    @app.post("/api/unsloth/call", dependencies=write)
    async def unsloth_call(payload: dict = Body(...)):
        ref = str(payload.get("operation") or "").strip()
        if not ref:
            raise HTTPException(status_code=400, detail="operation is required")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        result = await run(client.call, ref, args, timeout=float(payload.get("timeout_s") or 600))
        op = await run(client.find_operation, ref)
        if op["mutating"]:
            audit("call", f"{op['method']} {op['path']}")
        return {"operation": op["id"], "result": result}
