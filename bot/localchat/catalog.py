"""The model picker's catalog: every model the ModelMistress hub can run, one row each, every figure labelled with its source.

    metadata   what the model file says (GGUF via the hub's model.list): architecture, size, quantization, context, MoE, vision
    planned    the hub's model.plan against this machine's GPU: fits / fits after unloading another / does not fit, VRAM
    measured   speeds from real replies and benchmarks here (speed.py); quality from ABP's eval reports (docs/agents/eval-reports)
    estimated  a speed estimate where nothing was measured (speed.py says how)

Grouped for the picker: loaded, gpu (fits on the GPU), cpu (only on the CPU: slow), too_big (does not fit here), other
(Ollama pass-through models the hub does not plan). Built from the hub on demand and cached for local_chat.catalog_ttl_s.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from bot.localchat import speed

logger = logging.getLogger("bot.localchat.catalog")

GROUP_ORDER = ("loaded", "gpu", "cpu", "too_big", "other")
_QUANT = re.compile(r"(?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|F16|BF16|F32|Q8_0)", re.I)
_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "doc": None}

Call = Callable[[str, Optional[dict]], Any]


def _hub_call(op: str, args: Optional[dict] = None) -> Any:
    from bot.modelmistress import client

    return client.call(op, args or {}, timeout=60)


# ---- what the model is for ------------------------------------------------------------------------------------------------------
def kind_of(row: dict) -> str:
    text = f"{row.get('id', '')} {row.get('name', '')} {row.get('architecture', '')}".lower()
    if "embed" in text or "rerank" in text:
        return "embedding"
    if "tts" in text or "speech" in text or "whisper" in text:
        return "speech"
    if "diffusion" in text or "sdxl" in text or "flux" in text:
        return "image"
    return "chat"


def strengths(row: dict, evals: list[dict]) -> list[dict]:
    """Short "good for" notes, each with where it came from. Nothing here is a guess: names and metadata say what a model was
    built for; eval reports say how it did here."""
    out: list[dict] = []
    text = f"{row.get('id', '')} {row.get('name', '')}".lower()
    kind = kind_of(row)
    if kind != "chat":
        out.append({"text": {"embedding": "embeddings and search (not for chatting)", "speech": "speech (not for chatting)",
                             "image": "images (not for chatting)"}[kind], "source": "metadata"})
        return out
    if "coder" in text or "code" in text:
        out.append({"text": "coding (a code-tuned model)", "source": "metadata"})
    if row.get("vision"):
        out.append({"text": "reads images", "source": "metadata"})
    if row.get("experts"):
        out.append({"text": f"mixture of experts ({row.get('experts_used') or '?'} of {row['experts']} active): fast for its size",
                    "source": "metadata"})
    ctx = int(row.get("context_length") or 0)
    if ctx >= 65536:
        out.append({"text": f"long context (up to {ctx // 1024}k tokens)", "source": "metadata"})
    if "instruct" in text or "chat" in text:
        out.append({"text": "follows instructions", "source": "metadata"})
    if "thinking" in text or "reason" in text or re.search(r"\br1\b", text):
        out.append({"text": "step-by-step reasoning", "source": "metadata"})
    best = max((e for e in evals if e.get("kind") == "factory"), key=lambda e: e.get("score") or 0, default=None)
    if best:
        out.append({"text": f"agentic coding: {best['passed']}/{best['total']} on ABP's factory bench ({best['when'][:10]})",
                    "source": "measured"})
    return out


def quant_of(model_id: str) -> str:
    tail = model_id.split(":", 1)[-1]
    found = _QUANT.findall(tail)
    return found[-1].upper() if found else ""


# ---- ABP's own eval reports ------------------------------------------------------------------------------------------------------
_REPORT = re.compile(r"^\d{4}-\d{2}-\d{2}-(factory|suite)-(.+?)(?:-(run\d+|fulltools|smalloff))?\.json$")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def eval_reports(folder: Optional[Path] = None) -> list[dict]:
    """[{kind, slug, variant, score, passed, total, when, file}] from docs/agents/eval-reports (factory and suite runs)."""
    from bot.modules import registry

    folder = folder or registry.abp_root() / "docs" / "agents" / "eval-reports"
    out = []
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        m = _REPORT.match(p.name)
        if not m:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("limited"):
            continue
        total, passed = int(d.get("total") or 0), int(d.get("passed") or 0)
        if not total:
            continue
        out.append({"kind": m.group(1), "slug": m.group(2), "variant": m.group(3) or "", "score": round(passed / total, 3),
                    "passed": passed, "total": total, "when": str(d.get("when") or p.name[:10]), "file": p.name})
    return out


def evals_for(model_id: str, reports: list[dict]) -> list[dict]:
    key = _norm(model_id)
    return [r for r in reports if _norm(r["slug"]) and _norm(r["slug"]) in key]


# ---- one row -----------------------------------------------------------------------------------------------------------------------
def _runs_on(plan: Optional[dict], row: dict, ram_free_gb: float) -> str:
    verdict = (plan or {}).get("verdict")
    if verdict in ("fits", "fits_after_unloading"):
        return "gpu"
    size = float(row.get("size_gb") or 0)
    if verdict in ("does_not_fit", "unknown") or plan is None:
        if size and ram_free_gb and size * 1.1 < ram_free_gb:
            return "cpu"                       # MM never spills a model half onto the CPU by itself; all-CPU still works
        return "none" if size else "unknown"
    return "unknown"


def build_row(row: dict, plan: Optional[dict], device: str, ram_free_gb: float, reports: list[dict], min_context: int,
              loaded: set[str]) -> dict:
    mid = str(row.get("id") or "")
    ollama = mid.startswith("ollama/")
    runs_on = "unknown" if ollama else _runs_on(plan, row, ram_free_gb)
    serving = (plan or {}).get("serving") or {}
    est = (plan or {}).get("estimate") or {}
    evals = evals_for(mid, reports)
    warnings = []
    per_slot = serving.get("n_ctx_per_slot")
    if per_slot and min_context and int(per_slot) < min_context and kind_of(row) == "chat":
        warnings.append(f"served with {per_slot} tokens per request: below the {min_context} agent work needs")
    if (plan or {}).get("unload"):
        warnings.append("loading it unloads " + ", ".join(str(u) for u in plan["unload"]))
    is_loaded = mid in loaded or bool(row.get("loaded"))
    group = "loaded" if is_loaded else "other" if ollama else {"gpu": "gpu", "cpu": "cpu", "none": "too_big"}.get(runs_on, "other")
    return {
        "id": mid, "name": row.get("name") or mid, "kind": kind_of(row), "group": group, "loaded": is_loaded,
        "metadata": {"architecture": row.get("architecture"), "size_label": row.get("size_label"), "size_gb": row.get("size_gb"),
                     "quant": quant_of(mid), "context_max": row.get("context_length"), "experts": row.get("experts"),
                     "experts_used": row.get("experts_used"), "vision": bool(row.get("vision")), "source": row.get("source")},
        "runs_on": runs_on,
        "plan": None if plan is None else {"verdict": plan.get("verdict"), "device": device, "vram_mib": est.get("total_mib"),
                                           "free_mib": plan.get("free_mib"), "n_ctx_per_slot": per_slot,
                                           "slots": serving.get("slots"), "unload": plan.get("unload") or []},
        "speed": speed.figure(row, device, runs_on),
        "strengths": strengths(row, evals),
        "evals": evals,
        "warnings": warnings,
    }


def build(call: Call = _hub_call, *, reports: Optional[list[dict]] = None, min_context: Optional[int] = None) -> dict:
    """The whole catalog from the hub (no cache). {"hub": {...}, "device": ..., "groups": [...], "models": [...]}."""
    if min_context is None:
        try:
            from bot import conductor

            min_context = int(conductor.settings().get("min_context") or 0)
        except Exception:  # noqa: BLE001
            min_context = 0
    status = call("service.status", None) or {}
    listed = call("model.list", None) or []
    listed = listed if isinstance(listed, list) else (listed.get("models") or [])
    try:
        loaded_rows = call("model.loaded", None) or []
    except Exception:  # noqa: BLE001
        loaded_rows = []
    loaded = {str(r.get("model") or r.get("id") or "") for r in loaded_rows if isinstance(r, dict)}
    ram_free_gb = float(((status.get("system") or {}).get("available_mb") or 0)) / 1024
    reports = eval_reports() if reports is None else reports

    def plan_of(row: dict) -> Optional[dict]:
        mid = str(row.get("id") or "")
        if not mid or mid.startswith("ollama/"):
            return None
        try:
            p = call("model.plan", {"model": mid})
            return p if isinstance(p, dict) else None
        except Exception as e:  # noqa: BLE001 - one model the hub cannot plan still gets a row
            logger.debug("model.plan %s: %s", mid, e)
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        plans = list(ex.map(plan_of, listed))
    device = next((str(d.get("name")) for p in plans if p for d in (p.get("devices") or []) if d.get("name")), "cpu")
    rows = [build_row(r, p, device, ram_free_gb, reports, min_context, loaded) for r, p in zip(listed, plans)]
    rows.sort(key=lambda r: (GROUP_ORDER.index(r["group"]), r["kind"] != "chat", -(r["speed"].get("tokens_per_s") or 0)))
    return {"hub": {"version": status.get("version"), "engine": bool(status.get("llama_server")),
                    "engine_version": status.get("llama_built_in") or status.get("llama_version")},
            "device": device, "ram_free_gb": round(ram_free_gb, 1), "min_context": min_context,
            "groups": [g for g in GROUP_ORDER if any(r["group"] == g for r in rows)], "models": rows, "built": time.time()}


def get(*, refresh: bool = False, ttl_s: Optional[float] = None) -> dict:
    """The cached catalog (rebuilt when older than local_chat.catalog_ttl_s, or on refresh)."""
    from bot import localchat

    ttl = float(localchat.settings()["catalog_ttl_s"] if ttl_s is None else ttl_s)
    with _lock:
        doc = _cache["doc"]
        if doc is not None and not refresh and time.time() - _cache["ts"] < ttl:
            return doc
    doc = build()
    with _lock:
        _cache.update(ts=time.time(), doc=doc)
    return doc


def forget() -> None:
    with _lock:
        _cache.update(ts=0.0, doc=None)


def default_model(doc: dict) -> str:
    """The model a new session starts on: local_chat.default_model, else a loaded chat model, else the best measured local
    coder that fits the GPU, else any chat model that fits."""
    from bot import localchat

    want = str(localchat.settings().get("default_model") or "")
    ids = {r["id"] for r in doc.get("models", [])}
    if want and want in ids:
        return want
    chat = [r for r in doc.get("models", []) if r["kind"] == "chat"]
    for r in chat:
        if r["loaded"]:
            return r["id"]
    gpu = [r for r in chat if r["group"] == "gpu"]
    scored = sorted(gpu, key=lambda r: -max((e["score"] for e in r["evals"] if e["kind"] == "factory"), default=-1))
    if scored:
        return scored[0]["id"]
    return chat[0]["id"] if chat else ""
