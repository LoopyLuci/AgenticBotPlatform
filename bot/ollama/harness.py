"""The Ollama workflows ABP runs itself.

Pulls, pushes and creates run as background jobs with live progress (`jobs()`), so a 17 GB download never ties up a
request. Ollama loads a model by itself on the first request, but at its configured default context, which on this
kind of setup can be enormous (OLLAMA_CONTEXT_LENGTH=262144 does not fit a 9B model's KV cache in 24 GB). So `load()`
asks for DEFAULT_CONTEXT unless told otherwise, and `serving_model()` gives ABP a model whose own default context is
sane, so requests through the OpenAI-compatible endpoint (which carry no context setting) do not reload it huge.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from bot.ollama import client
from bot.ollama.client import OllamaError

logger = logging.getLogger("bot.ollama")

DEFAULT_CONTEXT = 32768
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _cfg() -> dict:
    try:
        from bot.config import config

        return config.current.get("ollama") or {}
    except Exception:  # noqa: BLE001
        return {}


def default_context() -> int:
    return int(_cfg().get("context_length") or DEFAULT_CONTEXT)


# ---- seeing what is there ------------------------------------------------------------------------------------------------------
def server_settings() -> dict:
    """The OLLAMA_* settings the running server was started with (read from its process; only for a local server)."""
    out: dict[str, str] = {}
    try:
        import psutil

        for p in psutil.process_iter(["name", "cmdline"]):
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if name.startswith("ollama") and "serve" in cmd:
                try:
                    out = {k: v for k, v in p.environ().items() if k.startswith("OLLAMA_")}
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    out = {}
                break
    except ImportError:
        pass
    if not out:
        out = {k: v for k, v in os.environ.items() if k.startswith("OLLAMA_")}
    return out


def storage(settings: Optional[dict] = None) -> dict:
    s = settings if settings is not None else server_settings()
    path = s.get("OLLAMA_MODELS") or str(Path.home() / ".ollama" / "models")
    free = None
    try:
        free = shutil.disk_usage(Path(path).anchor or path).free
    except OSError:
        pass
    old = Path.home() / ".ollama" / "models"
    old_there = old.resolve() != Path(path).resolve() and (old / "manifests").is_dir() and any((old / "manifests").rglob("*"))
    return {"models_dir": path, "free_bytes": free, "old_models_dir": str(old) if old_there else None}


def warnings(settings: dict) -> list[str]:
    out = []
    ctx = int(settings.get("OLLAMA_CONTEXT_LENGTH") or 0)
    if ctx > 65536:
        out.append(f"OLLAMA_CONTEXT_LENGTH is {ctx:,}: a model loaded at that context rarely fits a GPU. ABP loads at "
                   f"{default_context():,} unless told otherwise.")
    host = settings.get("OLLAMA_HOST", "")
    if host.startswith("0.0.0.0") or host.startswith("[::]"):
        out.append("OLLAMA_HOST listens on every network address: anyone on your network can use (and delete) your models. "
                   "127.0.0.1 keeps it on this computer.")
    return out


def status() -> dict:
    o = client.find(refresh=True)
    if o is None:
        return {"running": False, "url": client.DEFAULT_URL}
    out: dict[str, Any] = {"running": True, "url": o.root, "provider": o.provider, "version": o.version}
    for key, (method, path) in {"ps": ("GET", "/api/ps"), "tags": ("GET", "/api/tags"), "cloud": ("GET", "/api/status"),
                                "account": ("POST", "/api/me")}.items():
        try:
            out[key] = client.request(method, path, ollama=o, timeout=15)
        except OllamaError as exc:
            out[key] = {"error": str(exc)}
    settings = server_settings()
    out["settings"] = settings
    out["storage"] = storage(settings)
    out["warnings"] = warnings(settings)
    out["loaded"] = [{"model": m.get("name"), "vram_gb": round((m.get("size_vram") or 0) / 1e9, 2), "size_gb": round((m.get("size") or 0) / 1e9, 2),
                      "context_length": m.get("context_length"), "expires_at": m.get("expires_at")}
                     for m in (out.get("ps") or {}).get("models") or []]
    out["installed"] = len((out.get("tags") or {}).get("models") or [])
    out["jobs"] = jobs()
    return out


def summary(full: Optional[dict] = None) -> dict:
    s = full or status()
    if not s.get("running"):
        return {"running": False, "hint": f"Ollama is not running at {s.get('url')}"}
    acct = s.get("account") or {}
    return {"running": True, "url": s["url"], "version": s.get("version"), "provider": s.get("provider"), "loaded": s.get("loaded"),
            "installed": s.get("installed"), "account": {"name": acct.get("name"), "plan": acct.get("plan")} if acct.get("name") else None,
            "cloud_enabled": not ((s.get("cloud") or {}).get("cloud") or {}).get("disabled", False),
            "storage": {"models_dir": s["storage"]["models_dir"], "free_gb": round((s["storage"]["free_bytes"] or 0) / 1e9, 1)},
            "warnings": s.get("warnings"), "jobs": [j for j in s.get("jobs") or [] if j["state"] == "running"]}


def models() -> list[dict]:
    """Installed models with what each is: family, size, quantization, and whether it is loaded now."""
    tags = client.request("GET", "/api/tags", timeout=30).get("models") or []
    loaded = {m.get("name") for m in (client.request("GET", "/api/ps", timeout=15).get("models") or [])}
    out = []
    for m in tags:
        d = m.get("details") or {}
        out.append({"model": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 2), "family": d.get("family"),
                    "parameters": d.get("parameter_size"), "quantization": d.get("quantization_level"), "format": d.get("format"),
                    "cloud": bool(m.get("remote_host")) or str(m.get("name", "")).endswith("cloud"), "modified": m.get("modified_at"),
                    "loaded": m.get("name") in loaded})
    return out


def show(model: str) -> dict:
    """A model's capabilities (tools, vision, thinking, embedding), native context, parameters, template and Modelfile."""
    data = client.request("POST", "/api/show", body={"model": model}, timeout=60)
    info = data.get("model_info") or {}
    ctx = next((v for k, v in info.items() if k.endswith(".context_length")), None)
    return {"model": model, "capabilities": data.get("capabilities") or [], "context_length": ctx, "details": data.get("details"),
            "parameters": data.get("parameters"), "template": data.get("template"), "system": data.get("system"),
            "modelfile": data.get("modelfile"), "license": (data.get("license") or "")[:2000], "remote_host": data.get("remote_host")}


def recommendations() -> list[dict]:
    return client.request("GET", "/api/experimental/model-recommendations", timeout=30).get("recommendations") or []


def installed_ids() -> list[str]:
    try:
        return [m.get("name") for m in client.request("GET", "/api/tags", timeout=5).get("models") or [] if m.get("name")]
    except OllamaError:
        return []


# ---- background jobs: pull, push, create ------------------------------------------------------------------------------------------
def jobs() -> list[dict]:
    with _jobs_lock:
        return [dict(j) for j in sorted(_jobs.values(), key=lambda j: -j["started"])][:50]


def job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _start(kind: str, model: str, fn: Callable[[Callable[[dict], None]], Any]) -> dict:
    with _jobs_lock:
        for j in _jobs.values():
            if j["kind"] == kind and j["model"] == model and j["state"] == "running":
                return dict(j)          # the same pull twice shares one job
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {"id": job_id, "kind": kind, "model": model, "state": "running", "status": "starting", "completed": 0,
                         "total": 0, "error": None, "started": time.time(), "finished": None}

    def progress(event: dict) -> None:
        with _jobs_lock:
            j = _jobs[job_id]
            j["status"] = event.get("status") or j["status"]
            if event.get("total"):
                j["total"], j["completed"] = int(event["total"]), int(event.get("completed") or 0)

    def run() -> None:
        try:
            fn(progress)
            state, error = "done", None
        except Exception as exc:  # noqa: BLE001 — reported on the job
            state, error = "failed", str(exc)[:500]
        with _jobs_lock:
            _jobs[job_id].update(state=state, error=error, finished=time.time())
        _note(f"{kind} {state}", model)

    threading.Thread(target=run, name=f"ollama-{kind}-{job_id}", daemon=True).start()
    return job(job_id) or {}


def pull(model: str, *, wait: bool = False, on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Download a model into Ollama's models folder (a :cloud model is only registered: it runs on ollama.com)."""
    if wait:
        last = client.stream("/api/pull", {"model": model}, on_event=on_progress)
        _note("pulled", model)
        return {"model": model, "status": last.get("status")}
    return _start("pull", model, lambda progress: client.stream("/api/pull", {"model": model}, on_event=progress))


def push(model: str) -> dict:
    return _start("push", model, lambda progress: client.stream("/api/push", {"model": model}, on_event=progress))


def create(model: str, spec: dict, *, wait: bool = True) -> dict:
    """Create a model: {"from", "system", "template", "parameters", "messages", "license", "quantize", "files", "adapters"}."""
    body = {"model": model, **{k: v for k, v in spec.items() if v not in (None, "", {}, [])}}
    if wait:
        last = client.stream("/api/create", body)
        _note("created", model)
        return {"model": model, "status": last.get("status")}
    return _start("create", model, lambda progress: client.stream("/api/create", body, on_event=progress))


def copy(source: str, destination: str) -> dict:
    client.request("POST", "/api/copy", body={"source": source, "destination": destination}, timeout=120)
    _note("copied", f"{source} -> {destination}")
    return {"source": source, "destination": destination}


def delete(model: str) -> dict:
    client.request("DELETE", "/api/delete", body={"model": model}, timeout=120)
    _note("deleted", model)
    return {"model": model, "deleted": True}


def import_gguf(path: str, model: str, *, system: Optional[str] = None, parameters: Optional[dict] = None,
                on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Make a GGUF file on this machine (e.g. one Unsloth Studio downloaded) an Ollama model: upload it as a blob (skipped
    when Ollama already has it), then create the model from it."""
    file = Path(path)
    if not file.is_file() or file.suffix.lower() != ".gguf":
        raise OllamaError(f"{path} is not a .gguf file")
    digest = "sha256:" + _sha256(file, on_progress)
    head = client.request("HEAD", f"/api/blobs/{digest}", timeout=30, raw=True)
    if head.status_code != 200:
        if on_progress:
            on_progress({"status": "uploading", "total": file.stat().st_size, "completed": 0})
        o = client.require()
        import httpx

        with open(file, "rb") as fh:
            r = httpx.post(o.root + f"/api/blobs/{digest}", content=fh, timeout=httpx.Timeout(3600, connect=10))
        if r.status_code >= 400:
            raise OllamaError(f"uploading {file.name} failed ({r.status_code}): {r.text[:300]}")
    params = {"num_ctx": default_context(), **(parameters or {})}
    return create(model, {"files": {file.name: digest}, "system": system, "parameters": params})


def _sha256(file: Path, on_progress: Optional[Callable[[dict], None]] = None) -> str:
    h = hashlib.sha256()
    total = file.stat().st_size
    done = 0
    with open(file, "rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
            done += len(chunk)
            if on_progress and done % (512 << 20) < (8 << 20):
                on_progress({"status": "hashing", "total": total, "completed": done})
    return h.hexdigest()


# ---- loading ---------------------------------------------------------------------------------------------------------------------
def load(model: str, *, context: Optional[int] = None, keep_alive: Optional[str] = None, options: Optional[dict] = None) -> dict:
    """Load a model onto the GPU now, at a context that fits (DEFAULT_CONTEXT unless told otherwise)."""
    opts = {"num_ctx": int(context or default_context()), **(options or {})}
    body: dict[str, Any] = {"model": model, "prompt": "", "stream": False, "options": opts}
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    client.request("POST", "/api/generate", body=body, timeout=1800)
    _note("loaded", model)
    info = next((m for m in client.request("GET", "/api/ps", timeout=15).get("models") or [] if m.get("name") == model), {})
    return {"model": model, "context_length": info.get("context_length") or opts["num_ctx"],
            "vram_gb": round((info.get("size_vram") or 0) / 1e9, 2), "expires_at": info.get("expires_at")}


def unload(model: str) -> dict:
    client.request("POST", "/api/generate", body={"model": model, "keep_alive": 0, "stream": False}, timeout=120)
    _note("unloaded", model)
    return {"model": model, "unloaded": True}


def serving_model(model: str) -> str:
    """The name ABP should send requests to so `model` runs at a context that fits.

    Requests through the OpenAI-compatible endpoint carry no context setting, so Ollama uses the model's own default,
    or OLLAMA_CONTEXT_LENGTH, which may be far too big to load. For a local model whose default is missing or too big,
    ABP creates `<model>-abp` (FROM the model, sharing its files, with num_ctx set) once and uses that."""
    if model.endswith("-abp") or "cloud" in model:
        return model
    settings = server_settings()
    env_ctx = int(settings.get("OLLAMA_CONTEXT_LENGTH") or 0)
    try:
        params = show(model).get("parameters") or ""
    except OllamaError:
        return model
    own = next((int(line.split()[-1]) for line in params.splitlines() if line.strip().startswith("num_ctx")), None)
    if own and own <= 131072:
        return model
    if not own and (not env_ctx or env_ctx <= 131072):
        return model
    name = model.split(":")[0] + "-abp:" + (model.split(":")[1] if ":" in model else "latest")
    if name not in installed_ids():
        create(name, {"from": model, "parameters": {"num_ctx": default_context()}})
    return name


# ---- storage: bring old models across -------------------------------------------------------------------------------------------
def move_models(source: Optional[str] = None, *, on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Move models from an old models folder (default ~/.ollama/models) into the one Ollama uses now. Each blob is copied,
    checked against its sha256 name, and only then removed from the old folder; manifests are merged, never overwritten."""
    target = Path(storage()["models_dir"])
    src = Path(source) if source else Path.home() / ".ollama" / "models"
    if src.resolve() == target.resolve():
        raise OllamaError("the old folder is the one Ollama already uses")
    if not (src / "blobs").is_dir():
        raise OllamaError(f"{src} has no Ollama models")
    (target / "blobs").mkdir(parents=True, exist_ok=True)
    blobs = [b for b in (src / "blobs").iterdir() if b.is_file()]
    total = sum(b.stat().st_size for b in blobs)
    moved = done = 0
    for b in blobs:
        dest = target / "blobs" / b.name
        if not dest.exists():
            tmp = dest.with_name(dest.name + ".partial")
            shutil.copyfile(b, tmp)
            if b.name.startswith("sha256-") and _sha256(tmp) != b.name.split("-", 1)[1]:
                tmp.unlink(missing_ok=True)
                raise OllamaError(f"copy of {b.name} did not match its checksum; nothing was removed")
            tmp.replace(dest)
            moved += 1
        done += b.stat().st_size
        if on_progress:
            on_progress({"status": f"moving {b.name[:19]}", "total": total, "completed": done})
    manifests = 0
    for m in (src / "manifests").rglob("*"):
        if m.is_file():
            dest = target / "manifests" / m.relative_to(src / "manifests")
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(m, dest)
                manifests += 1
    # Every file is now in the target, verified: remove the old copies.
    for b in blobs:
        b.unlink(missing_ok=True)
    shutil.rmtree(src / "manifests", ignore_errors=True)
    _note("moved models", f"{src} -> {target}")
    return {"from": str(src), "to": str(target), "blobs_copied": moved, "manifests": manifests, "bytes": total}


def _note(what: str, detail: Optional[str]) -> None:
    try:
        from bot import db

        db.log_audit(actor="ollama", action=what.replace(" ", "_"), detail=str(detail or "")[:300])
    except Exception:  # noqa: BLE001
        pass
