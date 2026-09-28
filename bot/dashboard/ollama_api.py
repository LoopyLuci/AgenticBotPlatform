"""Ollama API: ABP's page for Ollama.

    GET  /api/ollama/status              version, account, loaded models, storage, server settings and warnings, jobs
    GET  /api/ollama/models              installed models
    GET  /api/ollama/show?model=         capabilities, context, parameters, template, Modelfile
    GET  /api/ollama/recommendations     Ollama's recommended models
    POST /api/ollama/pull                {"model"} -> a background job
    POST /api/ollama/push                {"model"} -> a background job
    GET  /api/ollama/jobs                pulls, pushes and creates with their progress
    POST /api/ollama/load                {"model", "context", "keep_alive", "options"}
    POST /api/ollama/unload              {"model"}
    POST /api/ollama/create              {"model", "spec": {"from", "system", "template", "parameters", "messages", "license", "quantize"}}
    GET  /api/ollama/gguf-files          .gguf files on this machine that can be imported (Unsloth's downloads, the models folders)
    POST /api/ollama/import-gguf         {"path", "model", "system", "parameters"} -> a background job
    POST /api/ollama/copy                {"source", "destination"}
    POST /api/ollama/delete              {"model"}
    POST /api/ollama/move-models         {"path"} bring models from an old folder into the one Ollama uses -> a background job
    GET  /api/ollama/operations          every route Ollama serves
    POST /api/ollama/call                {"operation": id or "METHOD /path", "args": {...}}

Reading uses the dashboard's normal auth; anything that changes Ollama needs the desktop dashboard token.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.ollama import client, harness
    from bot.ollama.client import OllamaError

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def run(fn, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except OllamaError as exc:
            code = 503 if "not running" in str(exc) or "could not reach" in str(exc) else (exc.status if exc.status and exc.status < 500 else 502)
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    def audit(action: str, detail: str) -> None:
        db.log_audit(actor="dashboard", action=f"ollama_{action}", detail=detail[:500])

    def need(payload: dict, key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"{key} is required")
        return value

    @app.get("/api/ollama/status", dependencies=read)
    async def ollama_status():
        full = await run(harness.status)
        return {"summary": harness.summary(full), "full": full}

    @app.get("/api/ollama/models", dependencies=read)
    async def ollama_models():
        return {"models": await run(harness.models)}

    @app.get("/api/ollama/show", dependencies=read)
    async def ollama_show(model: str):
        return await run(harness.show, model)

    @app.get("/api/ollama/recommendations", dependencies=read)
    async def ollama_recommendations():
        return {"recommendations": await run(harness.recommendations)}

    @app.post("/api/ollama/pull", dependencies=write)
    async def ollama_pull(payload: dict = Body(...)):
        model = need(payload, "model")
        audit("pull", model)
        return await run(harness.pull, model)

    @app.post("/api/ollama/push", dependencies=write)
    async def ollama_push(payload: dict = Body(...)):
        model = need(payload, "model")
        audit("push", model)
        return await run(harness.push, model)

    @app.get("/api/ollama/jobs", dependencies=read)
    def ollama_jobs():
        return {"jobs": harness.jobs()}

    @app.post("/api/ollama/load", dependencies=write)
    async def ollama_load(payload: dict = Body(...)):
        model = need(payload, "model")
        out = await run(harness.load, model, context=int(payload.get("context") or 0) or None, keep_alive=payload.get("keep_alive") or None,
                        options=payload.get("options") if isinstance(payload.get("options"), dict) else None)
        audit("load", model)
        return out

    @app.post("/api/ollama/unload", dependencies=write)
    async def ollama_unload(payload: dict = Body(...)):
        model = need(payload, "model")
        audit("unload", model)
        return await run(harness.unload, model)

    @app.post("/api/ollama/create", dependencies=write)
    async def ollama_create(payload: dict = Body(...)):
        model = need(payload, "model")
        spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
        audit("create", model)
        return await run(harness.create, model, spec, wait=False)

    @app.get("/api/ollama/gguf-files", dependencies=read)
    async def ollama_gguf_files(limit: int = Query(200, ge=1, le=2000)):
        def scan():
            roots: list[Path] = []
            try:
                from bot.unsloth import harness as studio

                d = studio.storage().get("models_dir")
                if d:
                    roots.append(Path(d))
            except Exception:  # noqa: BLE001 — Studio not running: its folder is simply not offered
                pass
            roots += [Path.home() / ".cache" / "huggingface" / "hub", Path.home() / ".unsloth"]
            seen, out = set(), []
            for root in roots:
                if not root.is_dir():
                    continue
                for f in root.rglob("*.gguf"):
                    try:
                        real = f.resolve()
                    except OSError:
                        continue
                    if real in seen or not real.is_file() or "mmproj" in f.name.lower():
                        continue
                    seen.add(real)
                    out.append({"path": str(f), "name": f.name, "size_bytes": real.stat().st_size})
                    if len(out) >= limit:
                        return out
            return out
        return {"files": await asyncio.to_thread(scan)}

    @app.post("/api/ollama/import-gguf", dependencies=write)
    async def ollama_import(payload: dict = Body(...)):
        path, model = need(payload, "path"), need(payload, "model")
        params = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else None
        audit("import_gguf", f"{path} -> {model}")
        return harness._start("import", model, lambda progress: harness.import_gguf(path, model, system=payload.get("system") or None,
                                                                                      parameters=params, on_progress=progress))

    @app.post("/api/ollama/copy", dependencies=write)
    async def ollama_copy(payload: dict = Body(...)):
        source, dest = need(payload, "source"), need(payload, "destination")
        audit("copy", f"{source} -> {dest}")
        return await run(harness.copy, source, dest)

    @app.post("/api/ollama/delete", dependencies=write)
    async def ollama_delete(payload: dict = Body(...)):
        model = need(payload, "model")
        audit("delete", model)
        return await run(harness.delete, model)

    @app.post("/api/ollama/move-models", dependencies=write)
    async def ollama_move(payload: dict = Body(default={})):
        path = str(payload.get("path") or "").strip() or None
        audit("move_models", path or "~/.ollama/models")
        return harness._start("move", path or "old models", lambda progress: harness.move_models(path, on_progress=progress))

    @app.get("/api/ollama/operations", dependencies=read)
    def ollama_operations():
        ops = client.operations()
        return {"operations": ops, "count": len(ops)}

    @app.post("/api/ollama/call", dependencies=write)
    async def ollama_call(payload: dict = Body(...)):
        ref = need(payload, "operation")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        try:
            op = client.find_operation(ref)
        except OllamaError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result = await run(client.call, ref, args, timeout=float(payload.get("timeout_s") or 900))
        if op["mutating"]:
            audit("call", f"{op['method']} {op['path']}")
        return {"operation": op["id"], "result": result}
