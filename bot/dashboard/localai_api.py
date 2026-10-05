"""/api/localai and /api/lab: ABP's local AI runtime (bot/localai) and the Neural Lab (bot/neurallab) from the
Local AI and Neural Lab pages, the CLI (`abp ai`, `abp lab`), the TUI and MCP. (Model inference itself is the local AI
server's own API: Ollama's and OpenAI's, on port 11436.)

    GET     /api/localai                          overview: server, engines, GPUs, models, running, mesh, training, runs
    GET|PUT /api/localai/settings                 POST /server/start | /server/stop
    POST    /api/localai/engine/install           {backend?} -> a run
    GET     /api/localai/models                   POST /models/pull {name} -> a run;  DELETE /models?name=
    POST    /api/localai/models/import            {name, path, reference?}   POST /models/copy {source, destination}
    POST    /api/localai/models/create            {name, modelfile}          GET /models/show?name=
    GET     /api/localai/mesh                     mesh-llm: reachable?, the node, its peers, GPUs, the models it serves
    GET     /api/localai/discover                 models other programs keep;  POST /discover/adopt {item, action?, name?}
                                                  POST /discover/adopt-all {gguf: reference|import}
    GET|PUT /api/localai/stores                   extra Ollama-format stores read in place
    GET     /api/localai/train                    environment, runs, datasets;  POST /train/setup -> a run
    POST    /api/localai/train                    {base, data, name?, export?, ...settings} -> the run
    GET     /api/localai/train/{id}               status + log tail;  POST /train/{id}/stop | /export {quant?, name?} -> a run
    POST    /api/localai/train/{id}/amethyst      {name, version?, type?, description?, install?} -> an .apkg (and installed)

    GET     /api/lab                              overview: runs, designs, projects, ops, system models
    POST    /api/lab/validate                     {spec} -> shapes, parameters, FLOPs
    GET     /api/lab/designs                      PUT /designs/{name} {spec};  GET /designs/{name}
    POST    /api/lab/runs                         {spec | design, data, train?, ...} -> the run;  GET /runs/{id}; POST /runs/{id}/stop
    POST    /api/lab/autotune                     {spec, data, space, trials?, metric?} -> a run
    GET     /api/lab/projects                     the toolkit's projects; GET /brainbuilder, /kotmoe; POST /import {kind, ...}
    POST    /api/lab/export/brainbuilder          {spec, path}
    GET     /api/lab/systune                      system models, CPU policy, telemetry;  POST /systune/bench {kind} -> a run
    POST    /api/lab/systune/train                {kind} -> the run;  GET /systune/advice?kind=&...
    GET     /api/lab/telemetry                    ?seconds= recent samples;  GET /telemetry/hw  machine checks, power losses
    GET     /api/localai/runs/{id}                the background runs above (the same registry as hosting's)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from bot.dashboard.hosting_api import _runs, start_run
from bot.localai import discover, engine, mesh, models, modelfile, pull, service, train
from bot.localai.paths import LocalAIError


async def _t(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (LocalAIError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _progress(log):
    last = {}

    def p(s: dict):
        st = s.get("status", "")
        if s.get("total"):
            pct = int(100 * (s.get("completed") or 0) / s["total"])
            if pct // 10 != last.get(st):
                last[st] = pct // 10
                log(f"{st}: {pct}%")
        elif st and st != last.get("_"):
            last["_"] = st
            log(st)
    return p


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]
    A = "/api/localai"

    @app.get(A, dependencies=dep)
    async def ai_overview():
        return await _t(service.overview)

    @app.get(f"{A}/settings", dependencies=dep)
    async def ai_settings():
        return engine.settings()

    @app.put(f"{A}/settings", dependencies=dep)
    async def ai_settings_put(body: dict = Body(...)):
        return await _t(engine.set_settings, body)

    @app.post(f"{A}/server/{{action}}", dependencies=dep)
    async def ai_server(action: str):
        if action == "start":
            return await _t(service.server_start)
        if action == "stop":
            return {"stopped": await _t(service.server_stop)}
        raise HTTPException(404, f"no server action {action}")

    @app.post(f"{A}/engine/install", dependencies=dep)
    async def ai_engine_install(body: dict = Body(default={})):
        return start_run("Install the llama.cpp engine", lambda log: engine.install(body.get("backend", ""), log))

    @app.get(f"{A}/models", dependencies=dep)
    async def ai_models():
        return await _t(models.listing)

    @app.post(f"{A}/models/pull", dependencies=dep)
    async def ai_pull(body: dict = Body(...)):
        name = body.get("name") or ""
        if not name:
            raise HTTPException(400, "name is required")
        if name.startswith(("http://", "https://")) and body.get("as"):
            return start_run(f"Download {body['as']}", lambda log: pull.pull_url(name, body["as"], _progress(log), body.get("sha256", "")))
        return start_run(f"Pull {name}", lambda log: pull.pull(name, _progress(log)))

    @app.delete(f"{A}/models", dependencies=dep)
    async def ai_delete(name: str = Query(...)):
        return {"deleted": await _t(models.delete, name)}

    @app.post(f"{A}/models/import", dependencies=dep)
    async def ai_import(body: dict = Body(...)):
        if body.get("reference"):
            return await _t(models.add_reference, body["name"], body["path"], body.get("origin", ""), body.get("projector", ""))
        return await _t(models.import_file, body["name"], body["path"], body.get("projector", ""))

    @app.post(f"{A}/models/copy", dependencies=dep)
    async def ai_copy(body: dict = Body(...)):
        await _t(models.copy, body["source"], body["destination"])
        return {"copied": True}

    @app.post(f"{A}/models/create", dependencies=dep)
    async def ai_create(body: dict = Body(...)):
        return await _t(modelfile.create, body["name"], body["modelfile"], base_dir=Path(body["dir"]) if body.get("dir") else None)

    @app.get(f"{A}/models/show", dependencies=dep)
    async def ai_show(name: str = Query(...)):
        rec = await _t(models.resolve, name)
        return {**rec, "modelfile": modelfile.render(rec)}

    @app.get(f"{A}/mesh", dependencies=dep)
    async def ai_mesh():
        return await _t(mesh.status)

    @app.get(f"{A}/discover", dependencies=dep)
    async def ai_discover():
        return await _t(discover.scan)

    @app.post(f"{A}/discover/adopt", dependencies=dep)
    async def ai_adopt(body: dict = Body(...)):
        return await _t(discover.adopt, body["item"], body.get("action", ""), body.get("name", ""))

    @app.post(f"{A}/discover/adopt-all", dependencies=dep)
    async def ai_adopt_all(body: dict = Body(default={})):
        return await _t(discover.adopt_all, body.get("gguf", "reference"))

    @app.get(f"{A}/stores", dependencies=dep)
    async def ai_stores():
        return models.extra_stores()

    @app.put(f"{A}/stores", dependencies=dep)
    async def ai_stores_put(body: list = Body(...)):
        return await _t(models.set_extra_stores, body)

    @app.get(f"{A}/train", dependencies=dep)
    async def ai_train():
        return {"env": await _t(train.env_status), "runs": await _t(train.runs), "datasets": await _t(train.datasets),
                "quants": train.QUANTS, "defaults": train.DEFAULTS}

    @app.post(f"{A}/train/setup", dependencies=dep)
    async def ai_train_setup():
        return start_run("Set up the training environment", lambda log: train.setup_env(log))

    @app.post(f"{A}/train", dependencies=dep)
    async def ai_train_start(body: dict = Body(...)):
        b = dict(body)
        base, data = b.pop("base", ""), b.pop("data", [])
        if not base or not data:
            raise HTTPException(400, "base and data are required")
        return await _t(train.start, base, data if isinstance(data, list) else [data], b.pop("name", ""), b.pop("export", "q4_k_m"), **b)

    @app.get(f"{A}/train/{{rid}}", dependencies=dep)
    async def ai_train_status(rid: str):
        return {**(await _t(train.status, rid)), "log": train.log_tail(rid, 60)}

    @app.post(f"{A}/train/{{rid}}/stop", dependencies=dep)
    async def ai_train_stop(rid: str):
        return await _t(train.stop, rid)

    @app.post(f"{A}/train/{{rid}}/export", dependencies=dep)
    async def ai_train_export(rid: str, body: dict = Body(default={})):
        return start_run(f"Export run {rid}", lambda log: train.export(rid, body.get("quant", ""), body.get("name", ""), log))

    @app.post(f"{A}/train/{{rid}}/amethyst", dependencies=dep)
    async def ai_train_amethyst(rid: str, body: dict = Body(...)):
        from bot.neurallab import interop

        def go(log):
            r = interop.apkg_from_run(rid, body["name"], body.get("version", "1.0.0"), body.get("type", "skill"),
                                      body.get("description", ""))
            log(f"packaged {r['path']} ({r['size'] >> 20} MiB)")
            if body.get("install", True):
                log(interop.amethyst_install(r["path"])[-600:])
            return r
        return start_run(f"Amethyst module from run {rid}", go)

    @app.get(f"{A}/runs/{{rid}}", dependencies=dep)
    async def ai_run(rid: str, since: int = Query(0, ge=0)):
        run = _runs.get(rid)
        if not run:
            raise HTTPException(404, "no such run (runs are kept for an hour)")
        return {**{k: v for k, v in run.items() if k != "log"}, "log": run["log"][since:], "log_total": len(run["log"])}

    _register_lab(app, dep)


def _register_lab(app: FastAPI, dep: list) -> None:
    from bot.neurallab import interop, lab, spec, systune, telemetry
    from bot.neurallab import service as lab_service
    L = "/api/lab"

    @app.get(L, dependencies=dep)
    async def lab_overview():
        return await _t(lab_service.overview)

    @app.post(f"{L}/validate", dependencies=dep)
    async def lab_validate(body: dict = Body(...)):
        s = await _t(spec.validate, body.get("spec") or body)
        return {"spec": s, "text": spec.describe(s)}

    @app.get(f"{L}/designs", dependencies=dep)
    async def lab_designs():
        return await _t(lab.designs)

    @app.get(f"{L}/designs/{{name}}", dependencies=dep)
    async def lab_design(name: str):
        return await _t(lab.design, name)

    @app.put(f"{L}/designs/{{name}}", dependencies=dep)
    async def lab_design_put(name: str, body: dict = Body(...)):
        return await _t(lab.save_design, body.get("spec") or body, name)

    @app.post(f"{L}/runs", dependencies=dep)
    async def lab_start(body: dict = Body(...)):
        b = dict(body)
        s = b.pop("spec", None) or (await _t(lab.design, b.pop("design")) if b.get("design") else None)
        if not s:
            raise HTTPException(400, "spec or design is required")
        data = b.pop("data", None)
        if not data:
            raise HTTPException(400, "data is required ({path, target?, tokenizer?})")
        extra = {k: b[k] for k in ("init_kmoe", "init_weights", "kmoe", "sample_prompt", "sample_tokens") if k in b}
        return await _t(lab.start, s, data, b.get("train"), b.get("label", ""), **extra)

    @app.get(f"{L}/runs", dependencies=dep)
    async def lab_runs():
        return await _t(lab.runs)

    @app.get(f"{L}/runs/{{rid}}", dependencies=dep)
    async def lab_run(rid: str):
        st = await _t(lab.status, rid)
        try:
            st["export"] = lab.export_of(rid)
        except LocalAIError:
            pass
        return st

    @app.post(f"{L}/runs/{{rid}}/stop", dependencies=dep)
    async def lab_stop(rid: str):
        return await _t(lab.stop, rid)

    @app.post(f"{L}/autotune", dependencies=dep)
    async def lab_autotune(body: dict = Body(...)):
        return start_run(f"Design search: {body['spec'].get('name', 'model')}",
                         lambda log: lab.autotune(body["spec"], body["data"], body["space"], int(body.get("trials", 8)),
                                                  body.get("train"), body.get("metric", "val_loss"), log))

    @app.get(f"{L}/projects", dependencies=dep)
    async def lab_projects():
        return interop.projects()

    @app.get(f"{L}/brainbuilder", dependencies=dep)
    async def lab_bb():
        return await _t(interop.brainbuilder_graphs)

    @app.get(f"{L}/kotmoe", dependencies=dep)
    async def lab_kotmoe():
        return {"registry": await _t(interop.kotmoe_registry), "checkpoints": await _t(interop.kotmoe_checkpoints)}

    @app.post(f"{L}/import", dependencies=dep)
    async def lab_import(body: dict = Body(...)):
        kind = body.get("kind")
        if kind == "brainbuilder":
            s = await _t(interop.from_bbir, body["path"])
        elif kind == "kotmoe-registry":
            row = next((r for r in interop.kotmoe_registry() if r["id"] == body["id"]), None)
            if not row:
                raise HTTPException(404, f"no KotMoE registry entry {body.get('id')}")
            s = interop.from_kotmoe_registry(row)
        elif kind == "kotmoe-checkpoint":
            s = await _t(interop.from_kmoe, body["path"])
        else:
            raise HTTPException(400, "kind is brainbuilder, kotmoe-registry or kotmoe-checkpoint")
        if body.get("save", True):
            await _t(lab.save_design, s, body.get("name", ""))
        return {"spec": s, "text": spec.describe(s)}

    @app.post(f"{L}/export/brainbuilder", dependencies=dep)
    async def lab_export_bb(body: dict = Body(...)):
        return {"path": str(await _t(interop.to_bbir, body["spec"], body["path"]))}

    @app.get(f"{L}/systune", dependencies=dep)
    async def lab_systune():
        return await _t(systune.status)

    @app.post(f"{L}/systune/bench", dependencies=dep)
    async def lab_bench(body: dict = Body(...)):
        kind = body.get("kind")
        if kind == "transfer":
            return start_run("Measure the drives", lambda log: systune.bench_all(log, int(body.get("size_mb", 128))))
        if kind == "llm":
            names = body.get("models") or [m["name"] for m in models.listing() if "embed" not in m["name"]]
            return start_run("Measure llama.cpp on the GPU", lambda log: [r for n in names for r in systune.bench_llm(n, log)])
        raise HTTPException(400, "kind is transfer or llm (memory and stability learn from telemetry)")

    @app.post(f"{L}/systune/train", dependencies=dep)
    async def lab_systune_train(body: dict = Body(...)):
        return await _t(systune.train_model, body["kind"])

    @app.get(f"{L}/systune/advice", dependencies=dep)
    async def lab_advice(kind: str = Query(...), src: str = "", dst: str = "", size_gb: float = 4, files: int = 100, model: str = ""):
        if kind == "transfer":
            return await _t(systune.advise_copy, src, dst, int(size_gb * 2**30), files)
        if kind == "llm":
            return await _t(systune.advise_llm, model)
        if kind == "memory":
            return await _t(systune.forecast_memory) or {}
        if kind == "stability":
            return await _t(systune.cpu_policy)
        raise HTTPException(400, "kind is transfer, llm, memory or stability")

    @app.get(f"{L}/telemetry", dependencies=dep)
    async def lab_telemetry(seconds: float = 900):
        return {"stats": await _t(telemetry.stats), "samples": await _t(telemetry.history, seconds, 2000)}

    @app.get(f"{L}/telemetry/hw", dependencies=dep)
    async def lab_hw():
        return {"topology": await _t(telemetry.topology), **(await _t(telemetry.hw_errors))}
