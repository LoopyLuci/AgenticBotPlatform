"""The Unsloth Studio workflows ABP runs itself: see what is there, get a model onto the GPU, keep it there for ABP's
agents, train, export.

Studio serves chat only for a model it has loaded ("No model loaded. Call POST /inference/load first"), so
`ensure_loaded()` loads a model on demand, once, even when several turns ask at the same moment; the native agent calls
it when Studio answers that way (bot/backends/native_backend.py). Everything else maps onto Studio's own operations;
anything this module does not wrap is still reachable through client.call().
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from bot.unsloth import client
from bot.unsloth.client import StudioError

logger = logging.getLogger("bot.unsloth")

EXPORT_KINDS = ("gguf", "lora", "merged", "base")
DEFAULT_CONTEXT = 32768      # tokens; `unsloth.context_length` in config/backends.yaml overrides it
_load_lock = threading.Lock()
_loading: dict[str, threading.Event] = {}


def _cfg() -> dict:
    try:
        from bot.config import config

        return (config.current.get("unsloth") or {})
    except Exception:  # noqa: BLE001
        return {}


# ---- seeing what is there --------------------------------------------------------------------------------------------------
def status() -> dict:
    """Studio at a glance: reachable, hardware, what is loaded, training, llama.cpp backend."""
    studio = client.find(refresh=True)
    if studio is None:
        return {"running": False, "url": client.DEFAULT_URL}
    out: dict[str, Any] = {"running": True, "url": studio.root, "provider": studio.provider, "has_key": bool(studio.key)}
    parts = (("health", "/api/health"), ("system", "/api/system"), ("hardware", "/api/train/hardware"),
             ("inference", "/api/inference/status"), ("loaded", "/api/inference/loaded-models"), ("training", "/api/train/status"),
             ("llama", "/api/llama/backend"), ("auth", "/api/auth/status"), ("hf_cache", "/api/settings/hugging-face-cache"))

    def one(path: str) -> Any:
        try:
            return client.request("GET", path, studio=studio, timeout=15)
        except StudioError as exc:
            return {"error": str(exc)}

    # Together: Studio takes seconds for hardware and system figures, and one after another the page waited for all.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(parts)) as pool:
        for (key, _path), value in zip(parts, pool.map(one, [path for _key, path in parts])):
            out[key] = value
    cache = out.pop("hf_cache") or {}
    out["storage"] = {"models_dir": cache.get("cache_home"), "free_bytes": cache.get("free_bytes"), "writable": cache.get("writable")} \
        if "error" not in cache else cache
    out["loaded"] = [m.get("id") for m in (out.get("loaded") or {}).get("data", []) if isinstance(m, dict)] \
        if isinstance(out.get("loaded"), dict) and "data" in out["loaded"] else []
    return out


def summary(full: Optional[dict] = None) -> dict:
    """The few facts that matter from status(): what an agent reads before deciding what to load."""
    s = full or status()
    if not s.get("running"):
        return {"running": False, "hint": f"Unsloth Studio is not running at {s.get('url')}"}
    gpus = [{"name": d.get("name"), "vram_used_gb": d.get("vram_used_gb"), "vram_total_gb": d.get("vram_total_gb")}
            for d in ((s.get("hardware") or {}).get("devices") or []) if isinstance(d, dict)]
    mem = (s.get("system") or {}).get("memory") or {}
    tr = s.get("training") or {}
    det = tr.get("details") or {}
    where = s.get("storage") or {}
    store = {"models_dir": where.get("models_dir"), "free_gb": round((where.get("free_bytes") or 0) / 1e9, 1)} if "error" not in where else None
    return {"running": True, "url": s.get("url"), "provider": s.get("provider"), "gpus": gpus,
            "ram_gb": {k: mem.get(k) for k in ("total_gb", "available_gb") if k in mem} or None,
            "backend": (s.get("llama") or {}).get("backend"), "loaded": s.get("loaded"), "storage": store,
            "training": {"phase": tr.get("phase"), "running": tr.get("is_training_running"), "step": det.get("step"),
                         "total_steps": det.get("total_steps"), "loss": det.get("loss"), "message": tr.get("message")},
            "auth": {k: (s.get("auth") or {}).get(k) for k in ("login_mode", "requires_password_change")}}


def loaded() -> list[str]:
    data = client.request("GET", "/api/inference/loaded-models", timeout=15)
    return [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


def models() -> dict:
    """What Studio can serve (`available`, each with whether it is loaded), what is cached from the Hugging Face hub,
    and the models on disk it found (including Ollama's and LM Studio's)."""
    served = client.request("GET", "/v1/models", timeout=30).get("data", [])
    out = {"available": [{"id": m.get("id"), "name": m.get("display_name") or m.get("id"), "loaded": bool(m.get("loaded")),
                          "context_length": m.get("context_length")} for m in served if isinstance(m, dict)]}
    for key, path in (("cached", "/api/models/cached-models"), ("cached_gguf", "/api/models/cached-gguf"), ("local", "/api/models/local")):
        try:
            data = client.request("GET", path, timeout=30)
            out[key] = data.get("cached") if "cached" in data else data.get("models", data)
        except StudioError as exc:
            out[key] = {"error": str(exc)}
    return out


def available_ids() -> list[str]:
    """Model ids Studio can serve right now (loaded or loadable): what ABP lists as this provider's models."""
    try:
        return [m.get("id") for m in client.request("GET", "/v1/models", timeout=5).get("data", []) if isinstance(m, dict) and m.get("id")]
    except StudioError:
        return []


def recommended(limit: int = 40) -> list[dict]:
    data = client.request("GET", "/api/models/list", timeout=30)
    keep = ("id", "name", "is_gguf", "is_vision", "is_embedding", "is_audio", "is_lora", "model_size_bytes")
    return [{k: m.get(k) for k in keep} for m in (data.get("models") or [])[:limit]]


def variants(repo_id: str) -> list[dict]:
    """The GGUF files of a hub repo: quant, size, whether it is already downloaded."""
    data = client.request("GET", "/api/models/gguf-variants", params={"repo_id": repo_id, "prefer_local_cache": True}, timeout=60)
    keep = ("filename", "quant", "size_bytes", "download_size_bytes", "downloaded", "partial", "update_available")
    return [{k: v.get(k) for k in keep} for v in data.get("variants", [])]


def estimate(model: str, *, variant: Optional[str] = None, context: Optional[int] = None) -> dict:
    """Studio's estimate of the memory a model needs, and whether it fits this machine."""
    return client.request("POST", "/api/inference/estimate-memory",
                          body={"model_path": model, "gguf_variant": variant, "n_ctx": context or None}, timeout=120)


# ---- where models live ---------------------------------------------------------------------------------------------------------
def storage() -> dict:
    """Where Studio keeps what it downloads (its Hugging Face cache), free space there, and the folders it scans for models."""
    cache = client.request("GET", "/api/settings/hugging-face-cache", timeout=30)
    try:
        scan = client.request("GET", "/api/models/scan-folders", timeout=30).get("folders", [])
    except StudioError:
        scan = []
    return {"models_dir": cache.get("cache_home"), "hub_cache": cache.get("hub_cache"), "free_bytes": cache.get("free_bytes"),
            "writable": cache.get("writable"), "custom": cache.get("is_custom"), "scan_folders": [f.get("path") for f in scan if f.get("path")]}


def set_models_dir(path: str) -> dict:
    """Make Studio download into `path` (its Hugging Face cache home). Models already downloaded elsewhere stay there."""
    path = str(path or "").strip()
    if not path:
        raise StudioError("give the folder models should be downloaded into")
    client.request("PUT", "/api/settings/hugging-face-cache", body={"cache_home": path}, timeout=60)
    _note("models folder set", path)
    return storage()


def ensure_models_dir() -> Optional[dict]:
    """Apply `unsloth.models_dir` from config/backends.yaml if Studio uses a different folder. None when not configured."""
    want = str(_cfg().get("models_dir") or "").strip()
    if not want:
        return None
    have = storage()
    if (have.get("models_dir") or "").rstrip("\\/").lower() != want.rstrip("\\/").lower():
        return set_models_dir(want)
    return have


# ---- downloading ---------------------------------------------------------------------------------------------------------------
def download(repo_id: str, *, variant: Optional[str] = None, wait: bool = False, timeout: float = 6 * 3600,
             on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Start a hub download (a GGUF repo needs its variant, e.g. "Q4_K_M"). With wait, poll until it finishes."""
    started = client.request("POST", "/api/hub/download", body={"repo_id": repo_id, "gguf_variant": variant}, timeout=120)
    if not wait:
        return {"started": True, "repo_id": repo_id, "variant": variant, "response": started}
    deadline = time.monotonic() + timeout
    seen_busy = False
    while time.monotonic() < deadline:
        st = download_status(repo_id, variant=variant)
        state = str(st.get("state") or "").lower()
        if on_progress:
            on_progress(st)
        if st.get("error") or state in ("error", "failed", "cancelled", "canceled"):
            raise StudioError(f"downloading {repo_id} failed: {st.get('error') or state}")
        if state in ("done", "completed", "complete", "finished", "downloaded", "ready") or (seen_busy and state == "idle"):
            return {"done": True, "repo_id": repo_id, "variant": variant, "status": st}
        seen_busy = seen_busy or state not in ("", "idle")
        time.sleep(3)
    raise StudioError(f"downloading {repo_id} did not finish within {timeout / 60:.0f} minutes (it may still be running)")


def download_status(repo_id: str, *, variant: Optional[str] = None) -> dict:
    st = client.request("GET", "/api/hub/download-status", params={"repo_id": repo_id, "gguf_variant": variant}, timeout=30)
    try:
        # A GGUF download reports on its own endpoint (and names the quant `variant` there).
        prog = (client.request("GET", "/api/hub/gguf-download-progress", params={"repo_id": repo_id, "variant": variant}, timeout=30)
                if variant else client.request("GET", "/api/hub/download-progress", params={"repo_id": repo_id}, timeout=30))
        if isinstance(prog, dict):
            st = {**st, "progress": prog}
    except StudioError:
        pass
    return st


# ---- loading ---------------------------------------------------------------------------------------------------------------
def load(model: str, *, variant: Optional[str] = None, context: int = 0, options: Optional[dict] = None, timeout: float = 1800) -> dict:
    """Load a model onto the GPU (Studio's own defaults unless `options` says otherwise: any field of its LoadRequest,
    e.g. gpu_layers, cache_type_kv, n_parallel). Blocks until it is loaded; the first load of a big GGUF can take minutes."""
    # Studio's own default is 2048 tokens for some models: too small for an agent's prompt and tools, which then
    # overflow it and come back as nonsense. ABP asks for DEFAULT_CONTEXT unless told otherwise.
    body = {"model_path": model, "gguf_variant": variant, "max_seq_length": int(context or _cfg().get("context_length") or DEFAULT_CONTEXT)}
    body.update({k: v for k, v in (options or {}).items() if v is not None})
    result = client.request("POST", "/api/inference/load", body=body, timeout=timeout)
    _note("loaded", model)
    return {"model": model, "context_length": result.get("context_length") if isinstance(result, dict) else None,
            "supports_reasoning": result.get("supports_reasoning") if isinstance(result, dict) else None}


def unload(model: str) -> dict:
    client.request("POST", "/api/inference/unload", body={"model_path": model}, timeout=300)
    _note("unloaded", model)
    return {"model": model, "unloaded": True}


def ensure_loaded(model: str, *, timeout: float = 1800) -> bool:
    """Make sure Studio has `model` loaded; load it if not. Returns True if it had to load it. Several callers asking for
    the same model at once share one load."""
    try:
        if model in loaded():
            return False
    except StudioError:
        pass
    with _load_lock:
        event = _loading.get(model)
        owner = event is None
        if owner:
            event = _loading[model] = threading.Event()
    if not owner:
        event.wait(timeout)
        return False
    try:
        logger.info("unsloth: loading %s on demand", model)
        load(model, variant=downloaded_variant(model), timeout=timeout)
        return True
    finally:
        with _load_lock:
            _loading.pop(model, None)
        event.set()


def downloaded_variant(model: str) -> Optional[str]:
    """For a GGUF repo, the quant already on disk (so an on-demand load never starts a download). None otherwise."""
    if "gguf" not in model.lower():
        return None
    try:
        have = [v for v in variants(model) if v.get("downloaded")]
    except StudioError:
        return None
    return have[0]["quant"] if have else None


def not_loaded_error(text: str) -> bool:
    """Whether a chat failure means "load this model first"."""
    low = str(text or "").lower()
    return "no model loaded" in low or ("not loaded" in low and "model" in low)


# ---- training and export ------------------------------------------------------------------------------------------------------
def train_start(fields: dict) -> dict:
    """Start fine-tuning: any field of Studio's TrainingStartRequest. model_name, training_type (e.g. "lora") and
    format_type (e.g. "chatml", "alpaca", "sharegpt") are required, plus a dataset (hf_dataset or local_datasets)."""
    for need in ("model_name", "training_type", "format_type"):
        if not fields.get(need):
            raise StudioError(f"training needs {need!r}")
    result = client.request("POST", "/api/train/start", body=fields, timeout=300)
    _note("training started", fields.get("model_name"))
    return result


def train_status() -> dict:
    return client.request("GET", "/api/train/status", timeout=30)


def train_metrics() -> dict:
    return client.request("GET", "/api/train/metrics", timeout=30)


def train_stop() -> dict:
    result = client.request("POST", "/api/train/stop", body={}, timeout=120)
    _note("training stopped", None)
    return result


def train_runs() -> Any:
    return client.request("GET", "/api/train/runs", timeout=30)


def export(kind: str, fields: dict) -> dict:
    """Export the loaded checkpoint: gguf (quantization_method, save_directory), lora, merged or base."""
    if kind not in EXPORT_KINDS:
        raise StudioError(f"export kind must be one of {', '.join(EXPORT_KINDS)}")
    if not fields.get("save_directory") and not fields.get("push_to_hub"):
        raise StudioError("an export needs save_directory (or push_to_hub with repo_id)")
    result = client.request("POST", f"/api/export/export/{kind}", body=fields, timeout=3600)
    _note(f"exported {kind}", fields.get("save_directory") or fields.get("repo_id"))
    return result


def export_status() -> dict:
    return client.request("GET", "/api/export/status", timeout=30)


def _note(what: str, model: Optional[str]) -> None:
    try:
        from bot import db

        db.log_audit(actor="unsloth", action=what.replace(" ", "_"), detail=str(model or "")[:300])
    except Exception:  # noqa: BLE001 — the audit trail is best-effort here
        pass
