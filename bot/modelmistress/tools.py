"""The agent's ModelMistress tools. Offered only while a hub answers. Reading is free; pulling and loading ask first.

    modelmistress_status   version, uptime, loaded models, backends, downloads running (peer: a linked machine's hub)
    modelmistress_models   the models that can be served, and which are loaded
    modelmistress_load     load a model (context, GPU layers...), or unload it
    modelmistress_pull     download a model (hf.co/org/repo[:quant] or an Ollama library name); a job unless wait
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from bot.modelmistress import client, harness
from bot.modelmistress.client import MMError

MAX_OUT = 12_000
PEER = {"type": "string", "description": "a linked machine's hub (modelmistress.peers in the config); reading its models only"}


def _out(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def _enabled() -> bool:
    try:
        return client.find_cached() is not None
    except Exception:  # noqa: BLE001
        return False


async def _run(fn, *args, **kwargs) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except MMError as exc:
        return f"Error: {exc}"


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, permission, read_only):
        toolspec.register(
            {"name": name, "description": description, "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only, origin="registered"),
            handler, enabled=_enabled)

    async def status(inp, **_):
        return await _run(harness.status, str(inp.get("peer") or ""))

    async def models(inp, **_):
        return await _run(harness.models, str(inp.get("peer") or ""), refresh=bool(inp.get("refresh")))

    async def load(inp, **_):
        model = str(inp.get("model") or "")
        if not model:
            return "Error: give the model to load"
        if inp.get("unload"):
            return await _run(harness.unload, model)
        return await _run(harness.load, model, ctx_size=int(inp.get("context") or 0) or None,
                          gpu_layers=inp.get("gpu_layers"), parallel=inp.get("parallel"))

    async def pull(inp, **_):
        model = str(inp.get("model") or "")
        if not model:
            return "Error: give the model, e.g. hf.co/org/repo:Q4_K_M or llama3.2:1b"
        return await _run(harness.pull, model, wait=bool(inp.get("wait")), source=inp.get("source") or None)

    reg("modelmistress_status", "ModelMistress at a glance: version, uptime, loaded models with their processes, backends and "
        "running downloads. peer: a linked machine's hub instead (its model list only).",
        {"peer": PEER}, [], status, permission="read", read_only=True)
    reg("modelmistress_models", "Models ModelMistress can serve (Ollama store, Hugging Face cache, its own downloads), with size and "
        "whether each is loaded. refresh=true rescans the disk. Use them as modelmistress/<model>.",
        {"peer": PEER, "refresh": {"type": "boolean"}}, [], models, permission="read", read_only=True)
    reg("modelmistress_load", "Load a ModelMistress model into memory now (context sets the context size; gpu_layers 999 = all, "
        "0 = CPU only), or unload it with unload=true (model \"*\" unloads every model).",
        {"model": {"type": "string"}, "context": {"type": "integer"}, "gpu_layers": {"type": "integer"}, "parallel": {"type": "integer"},
         "unload": {"type": "boolean"}}, ["model"], load, permission="config", read_only=False)
    reg("modelmistress_pull", "Download a model into ModelMistress: hf.co/<org>/<repo>[:<quant>] from Hugging Face, or <name>[:<tag>] "
        "from the Ollama library. wait=true waits; otherwise it runs as a background job (see modelmistress_status).",
        {"model": {"type": "string"}, "wait": {"type": "boolean"}, "source": {"type": "string", "enum": ["huggingface", "ollama"]}},
        ["model"], pull, permission="network", read_only=False)


register_tools()

# Look for the hub in the background now, so the first agent turn already knows whether to offer these tools.
client.find_cached()
