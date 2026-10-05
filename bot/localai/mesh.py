"""mesh-llm as a Local AI backend: one node's OpenAI-compatible API (http://127.0.0.1:9337 by default) that pools
GPUs and memory across machines, and the console's read-only management API on 3131 for the node's own view of the
mesh (nodes, peers, GPUs, which model runs where).

mesh-llm is a program ABP does not start: it is already installed and running, or it is not there. ABP only talks to
it, so a node that is down costs nothing - no mesh models in /api/tags, a status that says it is not reachable, and a
request for a mesh model answered with that one sentence. When it runs, its models are served by ABP's own server on
11436 like local ones, under a name that says where they run: "mesh/<the mesh's own model id>", an Ollama namespace
like hf.co/ is. The engine is chosen by the name, so /api/tags, /api/chat, /api/generate and /v1/chat/completions
need no routes of their own and "+memory" (ABP's shared memory in the system prompt) works exactly as for a local
model.

Settings (engine.set_settings, `abp ai settings mesh_url=...`): mesh_url, mesh_console_url, mesh_timeout_s.

    settings()    where the node is, and how long to wait for it
    status()      reachable?, the node, its peers, GPUs, the models it serves and what it is serving now
    list_models() the models it serves right now (GET /v1/models), each with the name ABP lists it under
    listing()     those as Ollama-shaped entries for /api/tags and /v1/models
    record(name)  the model record for a name that routes here (None for anything else)
    routed(name)  the proxy target for such a name, or None when ABP's own engine should run it
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from bot.localai import engine, models
from bot.localai.paths import LocalAIError

PREFIX = "mesh/"                   # mesh models are listed as "mesh/<the mesh's own id>" (an Ollama namespace)
ENGINE = "mesh-llm"
DEFAULT_URL = "http://127.0.0.1:9337"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:3131"
CACHE_S = 10.0                      # discovery is cached this long: /api/tags is a route clients poll


# ---- settings -------------------------------------------------------------------------------------------------------- #

def settings() -> dict:
    st = engine.settings()
    return {"url": _trim(st.get("mesh_url") or DEFAULT_URL), "console_url": _trim(st.get("mesh_console_url") or DEFAULT_CONSOLE_URL),
            "timeout_s": float(st.get("mesh_timeout_s") or 3.0)}


def _trim(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _why(e: Exception) -> str:
    return "nothing is listening there" if isinstance(e, httpx.ConnectError) else str(e) or type(e).__name__


def _get(url: str, path: str, timeout_s: float) -> dict:
    with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0))) as c:
        r = c.get(f"{url}{path}")
        r.raise_for_status()
        return r.json()


# ---- discovery ------------------------------------------------------------------------------------------------------ #

_cache: dict[str, tuple[float, dict]] = {}


def forget() -> None:
    _cache.clear()


def _cached(key: str, build) -> dict:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    out = build()
    _cache[key] = (time.time(), out)
    return out


def _discover() -> dict:
    """One look at the node: /health on the API port, the models it serves, and the console's own view of the mesh
    when it answers (it runs on its own port and may be off). A node that is down is a status, never an exception."""
    st = settings()
    out: dict = {"url": st["url"], "console_url": st["console_url"], "reachable": False, "console": False, "error": "",
                 "seen": time.time(), "node": {}, "peers": [], "gpus": [], "models": [], "serving": [], "loaded": [],
                 "mesh": {}, "health": {}}
    try:
        health = _get(st["url"], "/health", st["timeout_s"])
    except (httpx.HTTPError, ValueError) as e:
        out["error"] = f"{ENGINE} is not reachable at {st['url']} ({_why(e)})"
        return out
    out.update(reachable=True, health=health if isinstance(health, dict) else {})
    out["mesh"] = out["health"].get("mesh") if isinstance(out["health"].get("mesh"), dict) else {}
    out["serving"] = [str(m) for m in (out["health"].get("serving") or {}).get("models") or []]
    try:
        listed = _get(st["url"], "/v1/models", st["timeout_s"])
        out["models"] = [m for m in (_model(d) for d in (listed.get("data") or []) if isinstance(d, dict)) if m["id"]]
    except (httpx.HTTPError, ValueError):
        pass
    try:
        status = _get(st["console_url"], "/api/status", st["timeout_s"])
    except (httpx.HTTPError, ValueError):
        return out
    if not isinstance(status, dict):
        return out
    out["console"] = True
    out["node"] = _node(status)
    out["peers"] = [_peer(p) for p in status.get("peers") or [] if isinstance(p, dict)]
    out["gpus"] = [_gpu(g) for g in status.get("gpus") or [] if isinstance(g, dict)]
    out["loaded"] = [{"name": str(m.get("name") or ""), "profile": str(m.get("profile") or ""), "backend": str(m.get("backend") or ""),
                      "status": str(m.get("status") or ""), "context_length": m.get("context_length")}
                     for m in ((status.get("runtime") or {}).get("models") or []) if isinstance(m, dict)]
    for m in out["models"]:
        m["where"] = _where(m["id"], status)
    return out


def _model(raw: dict) -> dict:
    mid = str(raw.get("id") or "").strip()
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    return {"id": mid, "name": PREFIX + mid, "display_name": str(raw.get("display_name") or mid),
            "owned_by": str(raw.get("owned_by") or ENGINE), "capabilities": [str(c) for c in raw.get("capabilities") or []],
            "context_length": int(meta.get("context_length") or 0), "architecture": str(meta.get("architecture") or ""),
            "parameter_size": str(meta.get("parameter_size") or ""), "quantization": str(meta.get("quant") or ""),
            "workload": str(meta.get("workload_class") or ""), "where": ""}


def _node(status: dict) -> dict:
    return {"id": str(status.get("node_id") or ""), "hostname": str(status.get("my_hostname") or ""),
            "state": str(status.get("node_state") or ""), "mesh": str(status.get("mesh_name") or ""),
            "version": str(status.get("version") or ""), "vram_gb": float(status.get("my_vram_gb") or 0.0),
            "is_host": bool(status.get("is_host"))}


def _peer(p: dict) -> dict:
    return {"id": str(p.get("id") or ""), "hostname": str(p.get("hostname") or ""), "state": str(p.get("state") or ""),
            "role": str(p.get("role") or ""), "vram_gb": float(p.get("vram_gb") or 0.0),
            "models": [str(m) for m in p.get("available_models") or []],
            "serving": [str(m) for m in p.get("serving_models") or []], "rtt_ms": p.get("rtt_ms"),
            "gpus": [_gpu(g) for g in p.get("gpus") or [] if isinstance(g, dict)]}


def _gpu(g: dict) -> dict:
    return {"name": str(g.get("name") or ""), "vram_gb": round(int(g.get("vram_bytes") or 0) / 2**30, 1),
            "backend": str(g.get("backend_device") or "")}


def _where(mid: str, status: dict) -> str:
    """Which node or peer runs a model right now, as the mesh says; empty while nothing serves it."""
    if mid in [str(m) for m in status.get("serving_models") or []]:
        return str(status.get("my_hostname") or status.get("node_id") or "this node")
    for p in status.get("peers") or []:
        if not isinstance(p, dict):
            continue
        served = [str(m) for m in (p.get("serving_models") or []) + (p.get("hosted_models") or [])]
        if mid in served:
            return str(p.get("hostname") or p.get("id") or "a peer")
    return ""


def status() -> dict:
    """The node's view of itself and the mesh, for the Local AI page, `abp ai mesh status` and /api/tags."""
    return _cached("status", _discover)


def list_models() -> list[dict]:
    return status()["models"]


# ---- the models, as ABP's API lists them ------------------------------------------------------------------------------ #

def capabilities(m: dict) -> list[str]:
    """Ollama's capability names for a mesh model, from what the mesh advertises. Its inference port normalizes tool
    calls, so every model that can talk can call tools."""
    if str(m.get("workload") or "").lower() == "embedding":
        return ["embedding"]
    caps = ["completion"]
    if set(m.get("capabilities") or []) & {"vision", "multimodal", "audio"}:
        caps.append("vision")
    if "reasoning" in (m.get("capabilities") or []):
        caps.append("thinking")
    caps.append("tools")
    return caps


def listing() -> list[dict]:
    """The mesh's models as Ollama-shaped entries for /api/tags and /v1/models; empty when it is not running."""
    st = status()
    out = []
    for m in st["models"]:
        fam = m["architecture"]
        out.append({"name": m["name"], "model": m["name"], "size": 0, "modified_at": models._iso(st["seen"]), "digest": "",
                    "source": ENGINE, "engine": ENGINE, "owned_by": m["owned_by"],
                    "details": {"format": "mesh", "engine": ENGINE, "family": fam, "families": [fam] if fam else [],
                                "parameter_size": m["parameter_size"], "quantization_level": m["quantization"],
                                "context_length": m["context_length"], "where": m["where"]},
                    "mesh": {"id": m["id"], "display_name": m["display_name"], "capabilities": capabilities(m),
                             "where": m["where"], "url": st["url"]}})
    return out


def details(mid: str) -> dict:
    """What /api/show answers for a mesh model: the mesh's own metadata, there being no local GGUF to read. It
    answers even when no node is there, so `ollama show` says where the model would come from."""
    st = status()
    m = next((x for x in st["models"] if x["id"] == mid), {})
    fam = m.get("architecture", "")
    return {"modelfile": "", "parameters": "", "template": "", "system": "", "license": "",
            "details": {"parent_model": "", "format": "mesh", "engine": ENGINE, "family": fam,
                        "families": [fam] if fam else [], "parameter_size": m.get("parameter_size", ""),
                        "quantization_level": m.get("quantization", "")},
            "model_info": {k: m[k] for k in ("context_length", "architecture", "parameter_size", "quantization") if m.get(k)},
            "capabilities": capabilities(m), "modified_at": models._iso(st["seen"]),
            "abp": {"source": ENGINE, "url": st["url"], "mesh_model": mid, "where": m.get("where", ""),
                    "error": st["error"]}}


# ---- routing ---------------------------------------------------------------------------------------------------------- #

def mesh_id(name: str) -> str:
    """The mesh's own model id behind an ABP model name. "mesh/<id>" is the id verbatim; Ollama's implicit ":latest"
    is taken off when the node does not serve a model with that tag (ABP's own name handling adds one)."""
    mid = name[len(PREFIX):].strip() if name.startswith(PREFIX) else name.strip()
    if mid.endswith(":latest"):
        ids = {m["id"] for m in list_models()}
        if mid[: -len(":latest")] in ids or mid not in ids:
            return mid[: -len(":latest")]
    return mid


def record(name: str) -> Optional[dict]:
    """The model record for a name that belongs to the mesh, None for anything else. A name with the mesh's prefix is
    the mesh's; a bare name is too when the node serves it - the caller (bot/localai/server.py) asks ABP's own store
    first, so a model that is really in it keeps running on the local engine."""
    mid = mesh_id(name)
    if not mid:
        return None
    if not name.startswith(PREFIX) and mid not in {m["id"] for m in list_models()}:
        return None
    return {"name": PREFIX + mid, "mesh_id": mid, "engine": "mesh", "source": ENGINE, "weights": "", "digest": "",
            "projector": None, "adapters": [], "template": None, "system": None, "params": {}, "messages": [],
            "license": None, "modified": 0}


class Target:
    """What bot/localai/server.py needs from a loaded local engine, for a model the mesh runs: the node's own
    OpenAI API in place of a llama-server, so a mesh request takes the same path as a local one - the same "+memory"
    block, tool calls, reasoning and streaming. The mesh owns loading, keep-alive and GPU memory, so there is
    nothing to unload here."""

    def __init__(self, rec: dict, url: str, embedding: bool = False):
        self.rec, self.url, self.port = rec, url, 0
        self.embedding = embedding
        self.busy, self.expires = 0, float("inf")
        self.started, self.last_used, self.load_seconds = time.time(), time.time(), 0.0

    def alive(self) -> bool:
        return True

    def stop(self) -> None:
        pass


def routed(name: str, embedding: bool = False) -> Optional[Target]:
    """The mesh target for a name the mesh serves, or None when ABP's own engine should run this model. A node that
    is not there raises, with the one sentence that says where ABP looked."""
    rec = record(name)
    if not rec:
        return None
    st = settings()
    try:
        _get(st["url"], "/health", st["timeout_s"])
    except (httpx.HTTPError, ValueError) as e:
        raise LocalAIError(f"{ENGINE} is not reachable at {st['url']} ({_why(e)})") from e
    return Target(rec, st["url"], embedding)
