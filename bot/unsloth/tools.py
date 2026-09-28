"""The agent's Unsloth Studio tools. Offered only while Studio is running. Reading is free; anything that changes Studio
(loading a model, downloading, training, exporting, or calling one of its operations that is not a read) asks first.

    unsloth_status       hardware, what is loaded, training, backend
    unsloth_models       what it can serve and what is cached; with repo_id, that repo's GGUF files; with recommended, its picks
    unsloth_load         load a model onto the GPU (then any bot can use it as unsloth/<id>); unload with unload=true
    unsloth_download     download a model (a GGUF repo needs a variant, e.g. Q4_K_M) from the Hugging Face hub
    unsloth_train        start / status / metrics / stop / runs of fine-tuning
    unsloth_export       export the trained model (gguf, lora, merged, base), or its status
    unsloth_operations   every operation Studio offers, by group, and the details of one (read-only)
    unsloth_call         call any Studio operation by id or "METHOD /path" (asks first; unsloth_operations can run the
                         read-only ones without asking)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from bot.unsloth import client, harness
from bot.unsloth.client import StudioError

MAX_OUT = 12_000


def _out(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def _workspace_files(spec: dict, workspace) -> dict:
    """{field: path or [paths]} -> {field: [(filename, bytes)]}, reading only files inside the agent's workspace."""
    from pathlib import Path

    if not workspace:
        raise ValueError("no workspace to read files from")
    root = Path(workspace).resolve()
    out: dict[str, list] = {}
    for field, paths in spec.items():
        for p in paths if isinstance(paths, list) else [paths]:
            path = (root / str(p)).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"{p} is outside the workspace")
            if not path.is_file():
                raise ValueError(f"{p} is not a file")
            out.setdefault(str(field), []).append((path.name, path.read_bytes()))
    return out


def _enabled() -> bool:
    try:
        return client.find_cached() is not None
    except Exception:  # noqa: BLE001
        return False


async def _run(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except StudioError as exc:
        return f"Error: {exc}"


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, permission, read_only):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only, origin="registered"),
            handler, enabled=_enabled)

    async def status(inp, **_):
        return await _run(harness.summary)

    async def models(inp, **_):
        if inp.get("repo_id"):
            return await _run(harness.variants, str(inp["repo_id"]))
        if inp.get("recommended"):
            return await _run(harness.recommended)
        return await _run(harness.models)

    async def load(inp, **_):
        model = str(inp.get("model") or "")
        if not model:
            return "Error: give the model to load (an id from unsloth_models, or a hub repo)"
        if inp.get("unload"):
            return await _run(harness.unload, model)
        return await _run(harness.load, model, variant=inp.get("variant"), context=int(inp.get("context") or 0),
                          options=inp.get("options") if isinstance(inp.get("options"), dict) else None)

    async def download(inp, **_):
        repo = str(inp.get("repo_id") or "")
        if not repo:
            return "Error: give the hub repo, e.g. unsloth/Qwen3.6-27B-MTP-GGUF"
        if inp.get("status_only"):
            return await _run(harness.download_status, repo, variant=inp.get("variant"))
        return await _run(harness.download, repo, variant=inp.get("variant"), wait=bool(inp.get("wait")), timeout=3 * 3600)

    async def train(inp, **_):
        action = str(inp.get("action") or "status")
        fns = {"status": harness.train_status, "metrics": harness.train_metrics, "stop": harness.train_stop, "runs": harness.train_runs}
        if action == "start":
            return await _run(harness.train_start, dict(inp.get("fields") or {}))
        if action not in fns:
            return "Error: action is start, status, metrics, stop or runs"
        return await _run(fns[action])

    async def export(inp, **_):
        if inp.get("status_only"):
            return await _run(harness.export_status)
        return await _run(harness.export, str(inp.get("kind") or "gguf"), dict(inp.get("fields") or {}))

    async def operations(inp, **_):
        def run():
            if inp.get("operation"):
                return client.find_operation(str(inp["operation"]))
            ops = client.operations()
            group = str(inp.get("group") or "")
            if not group:
                counts: dict[str, int] = {}
                for o in ops:
                    counts[o["group"]] = counts.get(o["group"], 0) + 1
                return {"groups": counts, "hint": "pass group to list its operations, or operation for one's details"}
            return [{"id": o["id"], "call": f"{o['method']} {o['path']}", "summary": o["summary"], "changes": o["mutating"]}
                    for o in ops if o["group"] == group]
        out = await _run(run)
        if inp.get("operation") and inp.get("call_read"):
            op = client.find_operation(str(inp["operation"]))
            if op["mutating"]:
                return out + "\n\nThat operation changes something; use unsloth_call for it."
            return await _run(client.call, str(inp["operation"]), dict(inp.get("args") or {}))
        return out

    async def call(inp, *, workspace=None, **_):
        ref = str(inp.get("operation") or "")
        if not ref:
            return "Error: give the operation id or \"METHOD /path\" (see unsloth_operations)"
        args = dict(inp.get("args") or {})
        if isinstance(inp.get("files"), dict):
            try:
                args["files"] = _workspace_files(inp["files"], workspace)
            except ValueError as exc:
                return f"Error: {exc}"
        return await _run(client.call, ref, args, timeout=float(inp.get("timeout_s") or 300))

    reg("unsloth_status", "Unsloth Studio at a glance: GPU and memory, the loaded model, training state, llama.cpp backend.",
        {}, [], status, permission="read", read_only=True)
    reg("unsloth_models", "Models in Unsloth Studio: what it can serve (and which is loaded), what is cached and on disk. With repo_id, the "
        "GGUF files (quants and sizes) of that Hugging Face repo; with recommended=true, Studio's recommended models.",
        {"repo_id": {"type": "string"}, "recommended": {"type": "boolean"}}, [], models, permission="read", read_only=True)
    reg("unsloth_load", "Load a model in Unsloth Studio (it then serves it; bots use it as unsloth/<model>). variant picks a GGUF quant; context "
        "sets the context length (0 = Studio's choice); options are any other load settings (gpu_layers, cache_type_kv, n_parallel...). "
        "unload=true unloads it instead.",
        {"model": {"type": "string"}, "variant": {"type": "string"}, "context": {"type": "integer"}, "options": {"type": "object"},
         "unload": {"type": "boolean"}}, ["model"], load, permission="config", read_only=False)
    reg("unsloth_download", "Download a model from the Hugging Face hub into Unsloth Studio. A GGUF repo needs variant (e.g. Q4_K_M; see "
        "unsloth_models with repo_id). wait=true waits until it finishes; status_only=true reports progress.",
        {"repo_id": {"type": "string"}, "variant": {"type": "string"}, "wait": {"type": "boolean"}, "status_only": {"type": "boolean"}},
        ["repo_id"], download, permission="network", read_only=False)
    reg("unsloth_train", "Fine-tune with Unsloth Studio. action: start (fields: any training setting; model_name, training_type, format_type and "
        "a dataset are required), status, metrics, stop, runs.",
        {"action": {"type": "string", "enum": ["start", "status", "metrics", "stop", "runs"]}, "fields": {"type": "object"}},
        ["action"], train, permission="execute", read_only=False)
    reg("unsloth_export", "Export the trained model from Unsloth Studio: kind gguf (fields: save_directory, quantization_method), lora, merged or base. "
        "status_only=true reports the export's progress.",
        {"kind": {"type": "string", "enum": list(harness.EXPORT_KINDS)}, "fields": {"type": "object"}, "status_only": {"type": "boolean"}},
        [], export, permission="write", read_only=False)
    reg("unsloth_operations", "Everything Unsloth Studio can do, from its own API description: with no arguments, its groups (chat, models, hub, "
        "train, export, datasets, data-recipe, rag, mcp, settings...); with group, that group's operations; with operation, one operation's "
        "parameters and body. call_read=true (with args) runs a read-only operation.",
        {"group": {"type": "string"}, "operation": {"type": "string"}, "call_read": {"type": "boolean"}, "args": {"type": "object"}},
        [], operations, permission="read", read_only=True)
    reg("unsloth_call", "Call any Unsloth Studio operation by id or \"METHOD /path\" (find them with unsloth_operations). args holds its path "
        "and query parameters by name; the JSON body is args.body or the remaining args. An upload (a dataset, a document) takes "
        "files: {field: path} from your working folder.",
        {"operation": {"type": "string"}, "args": {"type": "object"}, "timeout_s": {"type": "number"},
         "files": {"type": "object", "description": "for an upload: {field: path or [paths]} inside your working folder"}},
        ["operation"], call, permission="external", read_only=False)


register_tools()

# Look for it in the background now, so the first agent turn already knows whether to offer these tools.
client.find_cached()
