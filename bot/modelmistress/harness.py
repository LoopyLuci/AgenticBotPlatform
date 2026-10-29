"""The everyday ModelMistress workflows, on top of client.py: status, models, load / unload, pull and its jobs.

Each function returns plain data (dicts and lists) for the agent tools, the dashboard routes and the other callers."""
from __future__ import annotations

from typing import Any, Optional

from bot.modelmistress import client
from bot.modelmistress.client import MMError


def status(peer: str = "") -> dict:
    hub = client.require(peer)
    out: dict[str, Any] = {"url": hub.url, "version": hub.version, "peer": hub.peer or None}
    if hub.remote:           # a peer key reaches the OpenAI routes only
        out["models"] = [m["id"] for m in client.models(hub)]
        return out
    out["service"] = client.call("service.status", hub=hub, timeout=15)
    out["loaded"] = client.call("model.loaded", hub=hub, timeout=15)
    out["backends"] = client.call("backend.list", hub=hub, timeout=15)
    out["jobs"] = [j for j in (client.call("job.list", hub=hub, timeout=15) or []) if j.get("status") == "running"]
    return out


def models(peer: str = "", *, refresh: bool = False) -> list[dict]:
    """The models that can be served (a local hub: with source, size and whether each is loaded)."""
    hub = client.require(peer)
    if hub.remote:
        return [{"id": m["id"], "source": m.get("owned_by")} for m in client.models(hub)]
    return list(client.call("model.list", {"refresh": refresh, "include_ollama": True}, hub=hub, timeout=60) or [])


def load(model: str, **options: Any) -> Any:
    """Load a model (options: ctx_size, gpu_layers, parallel, threads, embeddings, reranking, reload...)."""
    args = {"model": model, **{k: v for k, v in options.items() if v is not None}}
    from bot.hwguard import slot

    # A load fills most of the card and runs the CPU hard for a while: it waits for the heavy-work slot (bot/hwguard), so it
    # never stacks with a render, a full test run or a build, and never starts while the hardware guard says pause.
    with slot.heavy("model_load", f"ModelMistress load {model}", wait_s=120):
        return client.call("model.load", args, timeout=900)


def unload(model: str) -> Any:
    return client.call("model.unload", {"model": model}, timeout=60)


def pull(model: str, *, wait: bool = False, source: Optional[str] = None) -> Any:
    """Download a model (hf.co/org/repo[:quant], or an Ollama library name). A job unless wait."""
    args: dict[str, Any] = {"model": model, "wait": bool(wait)}
    if source:
        args["source"] = source
    return client.call("model.pull", args, timeout=None if wait else 60)  # type: ignore[arg-type]


def jobs() -> list[dict]:
    return list(client.call("job.list", timeout=15) or [])


def job(job_id: str) -> Any:
    return client.call("job.get", {"job": job_id}, timeout=15)


def available() -> bool:
    return client.find() is not None


__all__ = ["MMError", "available", "job", "jobs", "load", "models", "pull", "status", "unload"]
