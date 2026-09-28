"""The agent's Ollama tools. Offered only while Ollama is running. Reading is free; anything that changes Ollama (pull,
load, create, copy, delete, push, moving models, or calling one of its routes that changes something) asks first.

    ollama_status       version, account and plan, loaded models and their VRAM, storage, server-setting warnings, jobs
    ollama_models       installed models; with model, one model's capabilities and Modelfile; with recommended, Ollama's picks
    ollama_pull         pull a model (wait=true to wait; otherwise a background job), or push one to ollama.com
    ollama_load         load a model at a context that fits, or unload it
    ollama_manage       create (from a model or a Modelfile's parts), import a GGUF file, copy, delete, move old models in
    ollama_operations   every Ollama route, by group, and the details of one; call_read runs a read-only one
    ollama_call         call any Ollama route by id or "METHOD /path"
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from bot.ollama import client, harness
from bot.ollama.client import OllamaError

MAX_OUT = 12_000


def _out(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def _enabled() -> bool:
    try:
        return client.find() is not None
    except Exception:  # noqa: BLE001
        return False


async def _run(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except OllamaError as exc:
        return f"Error: {exc}"


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, permission, read_only):
        toolspec.register(
            {"name": name, "description": description, "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only, origin="registered"),
            handler, enabled=_enabled)

    async def status(inp, **_):
        return await _run(harness.summary)

    async def models(inp, **_):
        if inp.get("model"):
            return await _run(harness.show, str(inp["model"]))
        if inp.get("recommended"):
            return await _run(harness.recommendations)
        return await _run(harness.models)

    async def pull(inp, **_):
        model = str(inp.get("model") or "")
        if not model:
            return "Error: give the model, e.g. qwen3.5:9b"
        if inp.get("push"):
            return await _run(harness.push, model)
        return await _run(harness.pull, model, wait=bool(inp.get("wait")))

    async def load(inp, **_):
        model = str(inp.get("model") or "")
        if not model:
            return "Error: give the model to load"
        if inp.get("unload"):
            return await _run(harness.unload, model)
        return await _run(harness.load, model, context=int(inp.get("context") or 0) or None, keep_alive=inp.get("keep_alive"),
                          options=inp.get("options") if isinstance(inp.get("options"), dict) else None)

    async def manage(inp, *, workspace=None, **_):
        action = str(inp.get("action") or "")
        model = str(inp.get("model") or "")
        if action == "create":
            spec = {k: inp.get(k) for k in ("from", "system", "template", "parameters", "messages", "license", "quantize")}
            return await _run(harness.create, model, spec)
        if action == "import_gguf":
            return await _run(harness.import_gguf, str(inp.get("path") or ""), model, system=inp.get("system"),
                              parameters=inp.get("parameters") if isinstance(inp.get("parameters"), dict) else None)
        if action == "copy":
            return await _run(harness.copy, model, str(inp.get("destination") or ""))
        if action == "delete":
            return await _run(harness.delete, model)
        if action == "move_models":
            return await _run(harness.move_models, inp.get("path") or None)
        return "Error: action is create, import_gguf, copy, delete or move_models"

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
        if inp.get("operation") and inp.get("call_read"):
            try:
                op = client.find_operation(str(inp["operation"]))
            except OllamaError as exc:
                return f"Error: {exc}"
            if op["mutating"]:
                return "That route changes something; use ollama_call for it."
            return await _run(client.call, str(inp["operation"]), dict(inp.get("args") or {}), timeout=float(inp.get("timeout_s") or 600))
        return await _run(run)

    async def call(inp, **_):
        ref = str(inp.get("operation") or "")
        if not ref:
            return "Error: give the operation id or \"METHOD /path\" (see ollama_operations)"
        return await _run(client.call, ref, dict(inp.get("args") or {}), timeout=float(inp.get("timeout_s") or 600))

    reg("ollama_status", "Ollama at a glance: version, ollama.com account and plan, loaded models with their VRAM and context, the models "
        "folder and its free space, warnings about the server's settings, and running pulls.", {}, [], status, permission="read", read_only=True)
    reg("ollama_models", "Installed Ollama models (family, size, quantization, loaded or not). With model: that model's capabilities (tools, "
        "vision, thinking, embedding), native context, parameters, template and Modelfile. With recommended=true: Ollama's recommendations.",
        {"model": {"type": "string"}, "recommended": {"type": "boolean"}}, [], models, permission="read", read_only=True)
    reg("ollama_pull", "Pull a model into Ollama (a name like qwen3.5:9b, or a :cloud model, which is only registered and runs on "
        "ollama.com). wait=true waits for it; otherwise it runs in the background (see ollama_status). push=true uploads it to ollama.com instead.",
        {"model": {"type": "string"}, "wait": {"type": "boolean"}, "push": {"type": "boolean"}}, ["model"], pull, permission="network", read_only=False)
    reg("ollama_load", "Load an Ollama model onto the GPU now, at a context that fits (context sets it; the default is 32,768), with "
        "keep_alive and other options; unload=true unloads it. Bots use Ollama models as <provider>/<model>.",
        {"model": {"type": "string"}, "context": {"type": "integer"}, "keep_alive": {"type": "string"}, "options": {"type": "object"},
         "unload": {"type": "boolean"}}, ["model"], load, permission="config", read_only=False)
    reg("ollama_manage", "Change Ollama's models. action: create (model, and from / system / template / parameters / messages / license / "
        "quantize, as in a Modelfile), import_gguf (path to a .gguf on this machine, model name), copy (model, destination), delete (model), "
        "move_models (bring models from the old ~/.ollama/models folder, or path, into the folder Ollama uses).",
        {"action": {"type": "string", "enum": ["create", "import_gguf", "copy", "delete", "move_models"]}, "model": {"type": "string"},
         "from": {"type": "string"}, "system": {"type": "string"}, "template": {"type": "string"}, "parameters": {"type": "object"},
         "messages": {"type": "array", "items": {"type": "object"}}, "license": {"type": "string"}, "quantize": {"type": "string"},
         "path": {"type": "string"}, "destination": {"type": "string"}}, ["action"], manage, permission="config", read_only=False)
    reg("ollama_operations", "Everything Ollama's API offers: with no arguments its groups (models, run, openai, anthropic, account, web, "
        "server); with group, its routes; with operation, one route's fields. call_read=true (with args) runs a read-only route such as "
        "show, chat, embed, web_search or tokenize.",
        {"group": {"type": "string"}, "operation": {"type": "string"}, "call_read": {"type": "boolean"}, "args": {"type": "object"},
         "timeout_s": {"type": "number"}}, [], operations, permission="read", read_only=True)
    reg("ollama_call", "Call any Ollama route by id or \"METHOD /path\" (see ollama_operations). args holds path parameters by name; the "
        "JSON body is args.body or the remaining args.",
        {"operation": {"type": "string"}, "args": {"type": "object"}, "timeout_s": {"type": "number"}}, ["operation"], call,
        permission="external", read_only=False)


register_tools()
