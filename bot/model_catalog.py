"""Everything ABP knows about a model, and where each fact came from (roadmap PM).

    info = model_catalog.lookup("openrouter", "qwen/qwen3.8-27b:free")
    info.context, info.max_output, info.tool_call, info.free, info.limits.rpm ...

Facts are layered; a higher layer wins, and every group of facts records its source so a
person (or the agent) can tell a published figure from a guess:

  1. **override**  - what you set under `native_agent.models.overrides` / `.limits` in backends.yaml
  2. **observed**  - limits the provider itself reported in response headers (usage_limits.py)
  3. **curated**   - config/model_limits.yaml: published free-tier limits, each with its source
                     URL and the date it was checked
  4. **catalog**   - models.dev: context window, output limit, modalities, tool/reasoning support,
                     knowledge cutoff, release date, open weights, and price (model_pricing.py
                     downloads and caches it)
  5. **builtin**   - the context-window table in context_window.py, for models nobody catalogued

Limits are *never invented*: a missing figure is `None`, meaning "not published", not
"unlimited". Prices are dollars per million tokens here (model_pricing.py converts to per-token).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("bot.model_catalog")

CURATED_PATH = Path(__file__).resolve().parent.parent / "config" / "model_limits.yaml"
_RESETS = ("rolling", "utc_midnight", "pacific_midnight")


@dataclass
class Limits:
    rpm: Optional[int] = None           # requests per minute
    rpd: Optional[int] = None           # requests per day
    tpm: Optional[int] = None           # tokens per minute
    tpd: Optional[int] = None           # tokens per day
    concurrent: Optional[int] = None    # simultaneous requests
    match: str = "*"                    # for a shared (provider-scope) counter: which model ids share it
    scope: str = "model"                # "model", or "provider" when every matching model shares one counter
    reset: str = "rolling"              # how the daily figures reset
    source: str = "unknown"             # override | observed | curated | unknown
    url: str = ""
    checked: str = ""
    note: str = ""

    def known(self) -> bool:
        return any(v is not None for v in (self.rpm, self.rpd, self.tpm, self.tpd, self.concurrent))


@dataclass
class ModelInfo:
    provider: str
    model: str
    name: str = ""
    family: str = ""
    description: str = ""
    context: Optional[int] = None
    max_output: Optional[int] = None
    input_modalities: list = field(default_factory=list)
    output_modalities: list = field(default_factory=list)
    tool_call: Optional[bool] = None
    reasoning: Optional[bool] = None
    reasoning_options: list = field(default_factory=list)
    structured_output: Optional[bool] = None
    attachment: Optional[bool] = None
    open_weights: Optional[bool] = None
    knowledge: str = ""                 # training-data cutoff
    release_date: str = ""
    last_updated: str = ""
    status: str = ""                    # e.g. "deprecated" when the catalog says so
    free: Optional[bool] = None
    price_input: Optional[float] = None         # $ per million tokens
    price_output: Optional[float] = None
    price_cache_read: Optional[float] = None
    price_cache_write: Optional[float] = None
    limits: Limits = field(default_factory=Limits)
    sources: dict = field(default_factory=dict)  # group -> where it came from
    catalog_provider: str = ""          # the models.dev provider id that matched, if any
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def vision(self) -> bool:
        return "image" in (self.input_modalities or [])


# ---- the raw catalog (models.dev), read without the network --------------------------
_disk: dict = {"mtime": None, "data": None}
_curated: dict = {"mtime": None, "entries": []}


def _raw() -> dict:
    """The models.dev catalog: the in-memory copy, else the on-disk cache, else {}."""
    from bot import model_pricing

    data = model_pricing._memory_cache.get("data")
    if isinstance(data, dict):
        return data
    path = model_pricing.CACHE_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _disk["data"] is not None and _disk["mtime"] == mtime:
        return _disk["data"]
    loaded = model_pricing._read_disk_cache() or {}
    _disk.update(mtime=mtime, data=loaded)
    return loaded


async def refresh(force: bool = False) -> str:
    """Download the catalog (cached for a day). Returns "live", "cache_fallback" or "unavailable"."""
    from bot import model_pricing

    _data, source = await model_pricing._catalog(refresh=force)
    return source


def catalog_age_s() -> Optional[float]:
    from bot import model_pricing

    try:
        return max(0.0, time.time() - model_pricing.CACHE_PATH.stat().st_mtime)
    except OSError:
        return None


# ---- names ------------------------------------------------------------------------------
def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def catalog_provider_for(provider: str, base_url: str = "") -> str:
    """The models.dev provider id for what ABP calls `provider` (a providers.yaml name, a
    catalog_id, or a base URL's host)."""
    raw = _raw()
    provider = (provider or "").strip()
    try:
        from bot import providers as registry

        cfg = registry.get_provider(provider) or {}
        if cfg.get("catalog_id"):
            return str(cfg["catalog_id"])
        base_url = base_url or cfg.get("base_url", "")
    except Exception:  # noqa: BLE001
        pass
    if provider in raw:
        return provider
    host = _host(base_url) or (provider if "." in provider else "")
    if host:
        for pid, entry in raw.items():
            if isinstance(entry, dict) and _host(str(entry.get("api") or "")) == host:
                return pid
    return provider


def _find_model(raw: dict, catalog_provider: str, model: str) -> tuple[Optional[dict], str]:
    """(entry, models.dev provider id). Falls back to a unique exact-id match anywhere."""
    entry = (raw.get(catalog_provider) or {}).get("models") or {}
    for candidate in (model, model.split("/", 1)[-1], model.split(":", 1)[0]):
        if candidate in entry:
            return entry[candidate], catalog_provider
    if not raw:
        return None, ""
    hits = [(pid, e["models"][model]) for pid, e in raw.items()
            if isinstance(e, dict) and isinstance(e.get("models"), dict) and model in e["models"]]
    if len({json.dumps(m.get("limit"), sort_keys=True) for _, m in hits}) == 1 and hits:
        return hits[0][1], hits[0][0]
    return None, ""


# ---- configuration --------------------------------------------------------------------------
def _cfg() -> dict:
    try:
        from bot.config import config

        return (((config.current.get("native_agent") or {}).get("models")) or {})
    except Exception:  # noqa: BLE001
        return {}


def _curated_entries() -> list[dict]:
    try:
        mtime = CURATED_PATH.stat().st_mtime
    except OSError:
        return []
    if _curated["mtime"] == mtime:
        return _curated["entries"]
    try:
        import yaml

        data = yaml.safe_load(CURATED_PATH.read_text(encoding="utf-8")) or {}
        entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
    except Exception:  # noqa: BLE001
        logger.warning("could not read %s", CURATED_PATH, exc_info=True)
        entries = []
    _curated.update(mtime=mtime, entries=entries)
    return entries


def _as_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _limits_from(d: dict, source: str) -> Limits:
    reset = str(d.get("reset") or "rolling")
    return Limits(
        rpm=_as_int(d.get("rpm")), rpd=_as_int(d.get("rpd")), tpm=_as_int(d.get("tpm")), tpd=_as_int(d.get("tpd")),
        concurrent=_as_int(d.get("concurrent")), scope="provider" if d.get("scope") == "provider" else "model",
        reset=reset if reset in _RESETS else "rolling", match=str(d.get("match") or "*"), source=source, url=str(d.get("source") or d.get("url") or ""),
        checked=str(d.get("checked") or ""), note=str(d.get("note") or ""))


def _glob(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())


def declared_limits(provider: str, model: str, catalog_provider: str = "") -> Limits:
    """The user's override if there is one, else the curated published limits, else nothing."""
    overrides = {str(k): v for k, v in (_cfg().get("limits") or {}).items() if isinstance(v, dict)}
    names = [f"{provider}/{model}", f"{catalog_provider}/{model}", model]
    for key in names:                                                    # an exact entry first
        if key in overrides:
            return _limits_from(overrides[key], "override")
    for key in sorted(overrides, key=len, reverse=True):                 # then patterns, most specific first
        if any(_glob(key, n) for n in names):
            return _limits_from(overrides[key], "override")
    for e in _curated_entries():
        if str(e.get("provider")) in (provider, catalog_provider) and _glob(str(e.get("match") or "*"), model):
            return _limits_from(e, "curated")
    return Limits()


# ---- lookup ---------------------------------------------------------------------------------
def lookup(provider: str, model: str, *, base_url: str = "") -> ModelInfo:
    """Everything known about `model` at `provider`. Never raises and never needs the network."""
    provider, model = str(provider or ""), str(model or "")
    cp = catalog_provider_for(provider, base_url)
    info = ModelInfo(provider=provider, model=model, catalog_provider=cp)
    entry, matched = _find_model(_raw(), cp, model)
    if entry:
        info.catalog_provider = matched
        limit = entry.get("limit") or {}
        cost = entry.get("cost") or {}
        mods = entry.get("modalities") or {}
        info.name, info.family = str(entry.get("name") or ""), str(entry.get("family") or "")
        info.description = str(entry.get("description") or "")
        info.context, info.max_output = _as_int(limit.get("context")), _as_int(limit.get("output"))
        info.input_modalities, info.output_modalities = list(mods.get("input") or []), list(mods.get("output") or [])
        for attr in ("tool_call", "reasoning", "structured_output", "attachment", "open_weights"):
            if isinstance(entry.get(attr), bool):
                setattr(info, attr, entry[attr])
        info.reasoning_options = list(entry.get("reasoning_options") or [])
        info.knowledge, info.release_date = str(entry.get("knowledge") or ""), str(entry.get("release_date") or "")
        info.last_updated, info.status = str(entry.get("last_updated") or ""), str(entry.get("status") or "")
        if cost.get("input") is not None and cost.get("output") is not None:
            info.price_input, info.price_output = float(cost["input"]), float(cost["output"])
            info.price_cache_read = float(cost["cache_read"]) if cost.get("cache_read") is not None else None
            info.price_cache_write = float(cost["cache_write"]) if cost.get("cache_write") is not None else None
            info.free = info.price_input == 0 and info.price_output == 0
        info.sources = {g: "catalog" for g in ("identity", "context", "capabilities", "price") if entry}
    else:
        info.notes.append("not in the models.dev catalog" + ("" if _raw() else " (the catalog has not been downloaded yet)"))
    if info.context is None:
        from bot.agent_runtime import context_window

        info.context, info.sources["context"] = context_window.builtin_window(model), "builtin"
        if info.context is None:
            info.notes.append("context window unknown; set it under native_agent.models.overrides")
    if info.free is None:
        from bot.commands import is_free_model_id

        if is_free_model_id(model):
            info.free, info.sources["price"] = True, "name (…:free)"
    ov = (_cfg().get("overrides") or {})
    for key in (f"{provider}/{model}", model):
        if isinstance(ov.get(key), dict):
            _apply_overrides(info, ov[key])
            break
    info.limits = declared_limits(provider, model, info.catalog_provider)
    info.sources["limits"] = info.limits.source
    if info.limits.source == "unknown":
        info.notes.append("no published rate limit is known for this model; usage is still counted")
    return info


def _apply_overrides(info: ModelInfo, o: dict) -> None:
    mapping = {"context": "context", "max_output": "max_output", "tool_call": "tool_call", "vision": None, "free": "free",
               "price_input": "price_input", "price_output": "price_output", "knowledge": "knowledge", "reasoning": "reasoning"}
    for key, attr in mapping.items():
        if key in o and attr:
            setattr(info, attr, o[key])
            info.sources["override:" + key] = "override"
    if o.get("vision") is True and "image" not in info.input_modalities:
        info.input_modalities.append("image")


def context_window(provider: str, model: str, *, base_url: str = "") -> Optional[int]:
    """The context window, or None when nobody knows it."""
    return lookup(provider, model, base_url=base_url).context


def describe(info: ModelInfo) -> str:
    """A compact, honest paragraph for a person or the model."""
    def n(v):
        return f"{v:,}" if isinstance(v, int) else "unknown"

    lines = [f"{info.provider}/{info.model}" + (f" ({info.name})" if info.name and info.name != info.model else "")]
    lines.append(f"  context {n(info.context)} tokens, max output {n(info.max_output)}")
    caps = [c for c, on in (("tools", info.tool_call), ("reasoning", info.reasoning), ("structured output", info.structured_output),
                            ("vision", info.vision or None), ("open weights", info.open_weights)) if on]
    lines.append("  " + (", ".join(caps) if caps else "capabilities: none listed"))
    if info.knowledge or info.release_date:
        lines.append(f"  knowledge cutoff {info.knowledge or '?'}, released {info.release_date or '?'}")
    if info.free:
        lines.append("  free to use")
    elif info.price_input is not None:
        lines.append(f"  ${info.price_input:g} / ${info.price_output:g} per million tokens (in / out)")
    lim = info.limits
    if lim.known():
        parts = [f"{v} {label}" for v, label in ((lim.rpm, "requests/min"), (lim.rpd, "requests/day"), (lim.tpm, "tokens/min"),
                                                (lim.tpd, "tokens/day"), (lim.concurrent, "at once")) if v is not None]
        lines.append(f"  limits ({lim.source}{', checked ' + lim.checked if lim.checked else ''}): " + ", ".join(parts)
                     + (" - shared by every model of this provider that matches" if lim.scope == "provider" else ""))
    for note in ([lim.note] if lim.note else []) + info.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def prompt_line(provider: str, model: str) -> str:
    """One stable line for the system prompt: what the agent is running on. It holds only facts that do
    not change during a session; what is left of an allowance is asked for with the model_info tool."""
    info = lookup(provider, model)
    bits = [f"context {info.context:,} tokens" if info.context else "context window unknown"]
    if info.max_output:
        bits.append(f"up to {info.max_output:,} output tokens")
    caps = [c for c, on in (("tool use", info.tool_call), ("reasoning", info.reasoning), ("images", info.vision or None)) if on]
    if caps:
        bits.append(", ".join(caps))
    if info.tool_call is False:
        bits.append("no native tool calling")
    lim = info.limits
    if lim.known():
        parts = [f"{v}/{unit}" for v, unit in ((lim.rpm, "min"), (lim.rpd, "day")) if v is not None]
        parts += [f"{v:,} tokens/{unit}" for v, unit in ((lim.tpm, "min"), (lim.tpd, "day")) if v is not None]
        bits.append("rate limits " + ", ".join(parts) + " - avoid needless calls and big fan-outs")
    return f"Model: {provider}/{model} - " + "; ".join(bits) + ". The model_info tool shows what is left of its allowance."


def search(*, provider: str = "", free_only: bool = False, min_context: int = 0, needs: tuple = (), query: str = "",
           limit: int = 20) -> list[dict]:
    """Catalogued models matching what you need, largest context first. `needs` may contain
    "tools", "reasoning", "vision", "structured"."""
    raw = _raw()
    cp = catalog_provider_for(provider) if provider else ""
    out = []
    for pid, entry in raw.items():
        if not isinstance(entry, dict) or (cp and pid != cp):
            continue
        for mid, m in (entry.get("models") or {}).items():
            if not isinstance(m, dict):
                continue
            if query and query.lower() not in (mid + " " + str(m.get("name") or "")).lower():
                continue
            ctx = _as_int((m.get("limit") or {}).get("context")) or 0
            cost = m.get("cost") or {}
            free = (cost.get("input") == 0 and cost.get("output") == 0) if cost.get("input") is not None else None
            if free_only and not free:
                continue
            if ctx < min_context:
                continue
            checks = {"tools": m.get("tool_call"), "reasoning": m.get("reasoning"), "structured": m.get("structured_output"),
                      "vision": "image" in ((m.get("modalities") or {}).get("input") or [])}
            if any(not checks.get(n) for n in needs):
                continue
            out.append({"provider": pid, "model": mid, "name": m.get("name") or "", "context": ctx or None,
                        "max_output": _as_int((m.get("limit") or {}).get("output")), "free": free,
                        "tools": bool(m.get("tool_call")), "reasoning": bool(m.get("reasoning")), "vision": checks["vision"],
                        "released": m.get("release_date") or ""})
    out.sort(key=lambda r: (r["context"] or 0, r["released"]), reverse=True)
    return out[:max(1, min(int(limit), 100))]
