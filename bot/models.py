"""Live model choices per backend, for validation and dashboard dropdowns.

No hardcoded model list exists here on purpose: every model shown anywhere
in the app (Telegram picker, dashboard, desktop app, Support Bot) comes
from a live fetch against a provider actually configured on this machine —
Anthropic's own /v1/models (via ANTHROPIC_API_KEY) for the api backend, or
Hermes Agent's own live provider model cache for the two Hermes backends.
No key/install configured means no models offered, not a stale built-in
list masquerading as real choices.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bot.models")

# Which of the two model "families" each backend belongs to — used by the
# dashboard to group model/backend pickers into a Claude section and a
# Hermes Agent section, since the two ecosystems have entirely disjoint
# model catalogs.
BACKEND_FAMILY: dict[str, str] = {
    "api": "claude",
    "cli": "claude",
    "ui": "claude",
    "hermes_cli": "hermes",
    "hermes_gateway": "hermes",
    "custom_model": "custom",
    "native_agent": "custom",
}

# The one hardcoded model id in this module — not a "list of choices" (the
# module docstring's promise stays intact: every choice offered anywhere
# still comes from a live fetch), just the same fallback bot.backends.
# api_backend.ApiBackend and bot.router already fell back to before this
# constant existed, given one name so both agree and so /status can report
# it accurately when nothing overrides it.
DEFAULT_API_MODEL = "claude-sonnet-5"

_API_CACHE_TTL_S = 300.0
_api_cache: dict = {"at": 0.0, "models": None}


async def live_api_models() -> Optional[list[str]]:
    """Live model IDs from Anthropic's own /v1/models, via the same
    ANTHROPIC_API_KEY the api backend already uses. Returns None (never
    raises) if there's no key configured or the call fails — callers show
    "no models available" in that case, never a hardcoded fallback list."""
    now = time.monotonic()
    if _api_cache["models"] is not None and (now - _api_cache["at"]) < _API_CACHE_TTL_S:
        return _api_cache["models"]

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key)
        ids = set()
        async for m in client.models.list(limit=1000):
            ids.add(m.id)
        models = sorted(ids, reverse=True)
    except Exception as exc:
        logger.warning("live_api_models: fetch failed, falling back to static list: %s", exc)
        return None

    _api_cache["models"] = models
    _api_cache["at"] = now
    return models


async def known_models_for(backend: str) -> Optional[list[str]]:
    """Every model id currently reachable for `backend`, or None if nothing
    live is available (no API key/Hermes install/cache) — the caller's
    signal to skip validation rather than fall back to a hardcoded list.
    Used wherever a model name needs checking against "does this actually
    exist right now" (e.g. /model set), as opposed to instance_model_groups'
    provider-grouped shape used by the picker UI."""
    family = BACKEND_FAMILY.get(backend, "claude")
    if family == "hermes":
        grouped = live_hermes_models()
        if not grouped:
            return None
        ids: set[str] = set()
        for models in grouped.values():
            ids.update(models)
        return sorted(ids) or None
    if family == "custom":
        grouped = await live_custom_models()
        if not grouped:
            return None
        ids = set()
        for models in grouped.values():
            ids.update(models)
        return sorted(ids) or None
    return await live_api_models()


def cached_api_models() -> Optional[list[str]]:
    """Whatever live_api_models() last fetched, without triggering a new
    network call — for synchronous callers (Support Bot slot-matching) that
    can't await. Returns None until the async fetch has run at least once
    since startup (or if it found no key), same as live_api_models() itself
    would in that case — no hardcoded fallback here either."""
    return _api_cache["models"]


def _hermes_cache_path() -> Optional[Path]:
    """Hermes Agent (a separate, third-party CLI this bot can route prompts
    through) keeps its own disk cache of every provider's live /v1/models
    response, refreshed whenever `hermes model` runs — there's no
    scriptable/non-interactive way to ask Hermes for this list directly, so
    reading its cache file is the only way to get a live-ish answer without
    launching Hermes's own interactive model picker. Checked in order across
    the locations Hermes is known to use across platforms; the first that
    exists wins."""
    candidates = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "hermes" / "provider_models_cache.json")
    home = Path.home()
    candidates.append(home / ".cache" / "hermes" / "provider_models_cache.json")
    candidates.append(home / ".hermes" / "provider_models_cache.json")
    for path in candidates:
        if path.is_file():
            return path
    return None


_hermes_gateway_cache: dict[int, dict] = {}  # instance_id -> {"at": float, "payload": dict}
_HERMES_GATEWAY_CACHE_TTL_S = 300.0


async def live_hermes_gateway_options(instance_id: int, refresh: bool = False) -> Optional[dict]:
    """The raw `/api/model/options` payload from the specific Hermes
    gateway process backing `instance_id`, or None if that instance isn't
    hermes_gateway-backed or the call fails (gateway not installed/
    reachable). Cached 300s per instance unless `refresh` is passed
    through to the gateway itself (which does its own pricing-fetch
    caching — this cache is just to avoid spawning/round-tripping on
    every dashboard render)."""
    from bot.backends.hermes_gateway_backend import HermesGatewayBackend
    from bot.router import router

    now = time.monotonic()
    if not refresh:
        cached = _hermes_gateway_cache.get(instance_id)
        if cached is not None and (now - cached["at"]) < _HERMES_GATEWAY_CACHE_TTL_S:
            return cached["payload"]

    backend = router.get_backend_for_instance(instance_id)
    if not isinstance(backend, HermesGatewayBackend):
        return None
    try:
        payload = await backend.fetch_model_options(refresh=refresh)
    except Exception as exc:
        logger.warning("live_hermes_gateway_options: fetch failed for instance %s: %s", instance_id, exc)
        return None
    _hermes_gateway_cache[instance_id] = {"at": now, "payload": payload}
    return payload


def _rows_from_gateway_payload(payload: dict) -> dict[str, list[dict]]:
    """Normalizes `/api/model/options`'s per-provider rows (see
    hermes_cli.inventory.build_model_options_payload — each row carries
    `models`, and a `pricing` map of model_id -> {"free": bool, "input":
    ..., "output": ...} once `_apply_pricing` has run) into
    {provider_slug: [{"id", "free", "input", "output"}, ...]}. Tolerant of
    whatever subset of fields a given payload actually has — this reads
    a third-party process's live JSON, not a contract BotServer controls.

    Keyed by each row's `slug` (e.g. "nous", "openrouter"), NOT its `name`
    (e.g. "Nous Portal", "OpenRouter") — `slug` is the literal value
    Hermes's own `delegation.provider` config expects; `name` is only a
    display label. Confirmed against the real hermes_cli/inventory.py
    source: rows carry both (`"name": _PROVIDER_LABELS.get(entry.slug,
    entry.label)`), and this was found the hard way — an earlier version
    of this function keyed on `name`, which silently produced an invalid
    delegation.provider value ("Nous Portal" is not a provider Hermes's
    own config resolves) that Hermes itself had to notice and
    self-correct mid-run during live testing. The synthetic "moa" row
    (Mixture-of-Agents — a feature, not a routable provider) is excluded
    since it isn't a valid delegation.provider target either."""
    grouped: dict[str, list[dict]] = {}
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        return grouped
    for row in providers:
        if not isinstance(row, dict):
            continue
        slug = row.get("slug")
        models = row.get("models")
        if not slug or slug.lower() == "moa" or not models:
            continue
        pricing = row.get("pricing") or {}
        entries = []
        for m in models:
            model_id = m if isinstance(m, str) else (m or {}).get("id")
            if not model_id:
                continue
            price = pricing.get(model_id) or {}
            entries.append({
                "id": model_id,
                "free": bool(price.get("free", False)),
                "input": price.get("input"),
                "output": price.get("output"),
            })
        if entries:
            grouped[slug] = entries
    return grouped


async def hermes_models_with_pricing(instance_id: int, refresh: bool = False) -> tuple[dict[str, list[dict]], str]:
    """{provider: [{"id","free","input","output"}, ...]} for `instance_id`,
    plus a source label so callers (and Claude, via the MCP model-listing
    tool) always know whether "free" is real pricing data or a fallback
    guess: "live" (fetched from this instance's own running Hermes
    gateway, real pricing), "cache_fallback" (Hermes's stale, possibly-
    never-populated disk cache — model ids only, "free" is the
    naming-convention regex from bot.commands.is_free_model_id), or
    "unavailable" (neither worked — empty dict)."""
    from bot.commands import is_free_model_id

    payload = await live_hermes_gateway_options(instance_id, refresh=refresh)
    if payload:
        grouped = _rows_from_gateway_payload(payload)
        if grouped:
            return grouped, "live"

    disk = live_hermes_models()
    if disk:
        return (
            {
                provider: [{"id": mid, "free": is_free_model_id(mid), "input": None, "output": None} for mid in ids]
                for provider, ids in disk.items()
            },
            "cache_fallback",
        )
    return {}, "unavailable"


_hermes_cache: dict = {"mtime": None, "models": None}


def live_hermes_models() -> Optional[dict[str, list[str]]]:
    """{provider_name: [model_ids]} from Hermes's own cache file, or None if
    that file doesn't exist (Hermes never installed/used) or fails to
    parse. Re-read only when the file's mtime changes, so this stays cheap
    to call on every dashboard refresh."""
    path = _hermes_cache_path()
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _hermes_cache["models"] is not None and _hermes_cache["mtime"] == mtime:
        return _hermes_cache["models"]
    try:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        grouped = {}
        for provider, entry in raw.items():
            models = (entry or {}).get("models")
            if models:
                grouped[provider] = sorted(models)
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        logger.warning("live_hermes_models: failed to read %s: %s", path, exc)
        return None
    _hermes_cache["mtime"] = mtime
    _hermes_cache["models"] = grouped
    return grouped


_CUSTOM_CACHE_TTL_S = 300.0
_custom_cache: dict[str, dict] = {}  # provider_name -> {"at": float, "models": Optional[list[str]]}


async def _resolve_effective_enabled(provider_name: str, entry: dict, model_ids: list[str]) -> dict[str, bool]:
    """Per-model_id effective enabled/disabled state for `provider_name`,
    combining any explicit bot.db.model_toggles override with a free/
    paid-based default when none exists: a local endpoint's models are
    free by convention (see _is_local_base_url) and default enabled; any
    other model defaults enabled only when models.dev actually reports
    it as free (cost 0/0) — an unpriced or genuinely paid model defaults
    DISABLED, so adding a real API key for a provider with hundreds of
    models doesn't silently expose all of them, paid ones included,
    with no explicit opt-in. Prefers the provider's stored catalog_id
    (set when it was added via the catalog-assisted picker) over its
    bare config name when looking up pricing, since the two only
    coincide by chance for a manually-named provider."""
    from bot import db
    from bot import model_pricing

    overrides = {row["model_id"]: bool(row["enabled"]) for row in db.list_model_toggles(provider_name)}
    is_local = _is_local_base_url(entry.get("base_url", ""))
    pricing_key = entry.get("catalog_id") or provider_name

    result: dict[str, bool] = {}
    for model_id in model_ids:
        if model_id in overrides:
            result[model_id] = overrides[model_id]
        elif is_local:
            result[model_id] = True
        else:
            pricing, _source = await model_pricing.get_pricing(pricing_key, model_id)
            result[model_id] = bool(pricing and pricing.get("free"))
    return result


async def live_custom_models() -> Optional[dict[str, list[str]]]:
    """{provider_name: [model_ids]} for every provider configured in
    config/providers.yaml, fetched live from that provider's own
    OpenAI-compatible /models endpoint (Ollama, LM Studio, vLLM, and
    llama.cpp's server all implement this) — same "no hardcoded catalog,
    ever" rule live_api_models() follows, just for custom endpoints.
    A provider whose fetch fails is simply omitted, not treated as a
    reason to fail the whole call — one dead local server shouldn't hide
    every other configured provider's models.

    Filters to only EFFECTIVELY ENABLED models — an explicit Models-page
    override when one exists, else the free/paid-based default computed
    by _resolve_effective_enabled() (paid/unpriced models are off unless
    explicitly turned on). This is the ONE choke point every model
    picker in the app (Telegram's /model, the dashboard's
    GET /api/models, dispatch_native_swarm_goal's auto-pick-free logic)
    already flows through, so toggling — or a provider's own free/paid
    default — is respected everywhere with no changes needed at those
    call sites. Use browse_provider_models() instead when disabled
    models must still be visible (i.e. the Models page's own toggle UI)."""
    from bot import providers as provider_registry

    configured = provider_registry.list_providers()
    if not configured:
        return None

    now = time.monotonic()
    grouped: dict[str, list[str]] = {}
    for name, entry in configured.items():
        cached = _custom_cache.get(name)
        if cached is not None and (now - cached["at"]) < _CUSTOM_CACHE_TTL_S:
            models = cached["models"]
        else:
            models = await _fetch_custom_models(name, entry, provider_registry)
            _custom_cache[name] = {"at": now, "models": models}
        if models is not None:
            effective = await _resolve_effective_enabled(name, entry, models)
            visible = [m for m in models if effective.get(m)]
            if visible:
                grouped[name] = visible
    return grouped or None


async def _fetch_custom_models(name: str, entry: dict, provider_registry) -> Optional[list[str]]:
    try:
        import httpx

        base_url = entry["base_url"].rstrip("/")
        headers = {}
        api_key = provider_registry.get_api_key(name)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        ids = sorted(m["id"] for m in data.get("data", []) if m.get("id"))
        return ids or None
    except Exception as exc:
        logger.warning("live_custom_models: fetch failed for provider %r: %s", name, exc)
        return None


async def custom_models_with_pricing(refresh: bool = False) -> tuple[dict[str, list[dict]], str]:
    """{provider_name: [{"id","free","input","output"}, ...]} for every
    provider configured in config/providers.yaml — the custom_model/
    native_agent equivalent of hermes_models_with_pricing(), closing a
    real gap where this family had zero pricing/free data at all. Model
    ids come from live_custom_models() (that provider's own /models
    endpoint); pricing for each id comes from bot.model_pricing.get_pricing()
    (models.dev). A model id live_custom_models() found but models.dev
    doesn't know about still appears, with free=False and input/output=None
    — omitting it would hide a real, usable model just because pricing
    data isn't available for it. Source label is the WORST of the two
    fetches ("unavailable" > "cache_fallback" > "live") since a caller
    checking "can I trust free=True here" needs to know if EITHER half of
    this combined data could be stale."""
    from bot import model_pricing

    models = await live_custom_models()
    if not models:
        return {}, "unavailable"

    _rank = {"live": 0, "cache_fallback": 1, "unavailable": 2}
    worst_source = "live"
    grouped: dict[str, list[dict]] = {}
    for provider_name, model_ids in models.items():
        entries = []
        for model_id in model_ids:
            pricing, source = await model_pricing.get_pricing(provider_name, model_id, refresh=refresh)
            if _rank.get(source, 2) > _rank.get(worst_source, 0):
                worst_source = source
            if pricing:
                entries.append({"id": model_id, **pricing})
            else:
                entries.append({"id": model_id, "free": False, "input": None, "output": None})
        grouped[provider_name] = entries
    return grouped, worst_source


def _is_local_base_url(base_url: str) -> bool:
    """True for a loopback/private-network endpoint (Ollama, LM Studio,
    vLLM, llama.cpp's server, all typically 127.0.0.1/localhost) — used
    by browse_provider_models() to treat a self-hosted model with no
    pricing data as free by convention, since it genuinely costs the
    user nothing per token, rather than showing it as "not marked free"
    for lack of a models.dev entry."""
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("192.168.") or host.startswith("10.")


def _sort_browse_entries(entries: list[dict]) -> list[dict]:
    """Free first, then unpriced (free=None), then paid — alphabetical
    within each group, matching the order the user asked the Models page
    to render in."""
    rank = {True: 0, None: 1, False: 2}
    return sorted(entries, key=lambda e: (rank.get(e["free"], 1), e["id"].lower()))


async def browse_provider_models(provider_name: str, refresh: bool = False) -> list[dict]:
    """Every model known for a CONFIGURED provider (config/providers.yaml)
    — for the Models page's own toggle UI, so unlike live_custom_models()/
    custom_models_with_pricing() this deliberately does NOT filter out
    disabled models (the whole point is showing them so they can be
    re-enabled). Combines models.dev's static catalog (via the
    provider's stored catalog_id, if it has one — always available, no
    live call needed) with a live /models fetch (covers local/custom
    models models.dev can't know about, e.g. whatever's actually pulled
    into a user's Ollama), each tagged with its current *effective*
    toggle state — an explicit override when one exists, else the same
    free-default-on/paid-default-off rule _resolve_effective_enabled()
    applies for live_custom_models(), so this page's checkboxes reflect
    reality rather than a flat "everything starts on".
    Sorted free-first per bot.models._sort_browse_entries. Returns []
    for an unconfigured provider name — never raises."""
    from bot import db
    from bot import model_pricing
    from bot import providers as provider_registry

    entry = provider_registry.get_provider(provider_name)
    if entry is None:
        return []

    by_id: dict[str, dict] = {}

    catalog_id = entry.get("catalog_id")
    if catalog_id:
        for m in await model_pricing.list_models_for_provider(catalog_id, refresh=refresh):
            by_id[m["id"]] = dict(m)

    live_ids = await _fetch_custom_models(provider_name, entry, provider_registry)
    is_local = _is_local_base_url(entry.get("base_url", ""))
    for model_id in (live_ids or []):
        if model_id not in by_id:
            by_id[model_id] = {"id": model_id, "free": True if is_local else None, "input": None, "output": None}

    if not by_id:
        return []

    overrides = {row["model_id"]: bool(row["enabled"]) for row in db.list_model_toggles(provider_name)}
    for model_id, e in by_id.items():
        e["enabled"] = overrides[model_id] if model_id in overrides else e.get("free") is True

    return _sort_browse_entries(list(by_id.values()))


# ---------------------------------------------------- real-time default model
# The `cli` and `ui` backends never get told which model to use (see
# bot/router.py's _build_backend — CliBackend/UiBackend take no `model`
# kwarg at all), so whatever they end up using is decided entirely by the
# external program, not by us. For `cli` that's genuinely readable: the
# `claude` binary this bot shells out to reads its own default model from
# this same settings file (same OS user, so the same file) whenever we
# don't pass --model. There's no equivalent for `ui` (Claude Desktop's
# currently-selected model lives in its own account-synced UI state, not
# any local file this process can read) — callers should say so plainly
# rather than invent a value here.
_cli_settings_cache: dict = {"mtime": None, "model": None}


def local_cli_default_model() -> Optional[str]:
    """Claude Code CLI's own configured default model (its settings.json
    "model" field — e.g. "sonnet"), or None if the file/field doesn't
    exist. Re-read only when the file's mtime changes."""
    path = Path.home() / ".claude" / "settings.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cli_settings_cache["mtime"] == mtime:
        return _cli_settings_cache["model"]
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        model = data.get("model") or None
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        logger.warning("local_cli_default_model: failed to read %s: %s", path, exc)
        return None
    _cli_settings_cache["mtime"] = mtime
    _cli_settings_cache["model"] = model
    return model


_hermes_config_cache: dict = {"mtime": None, "model": None}


def local_hermes_default_model() -> Optional[str]:
    """Hermes Agent's own configured default model, or None if the
    file/field doesn't exist or is left blank — Hermes then decides its
    own default at runtime with nothing local for us to read. Re-read
    only when the file's mtime changes. Path resolution matches Hermes's
    own (HERMES_HOME env var, else the platform-native default —
    %LOCALAPPDATA%\\hermes on Windows, ~/.hermes elsewhere), NOT a bare
    ~/.hermes guess — see bot.hermes_config's own module docstring for
    why that distinction is real, not pedantic (a previous version of
    this exact bug silently read/wrote the wrong file on Windows).

    The top-level `model` key itself has been observed in two real
    shapes across real installs: a bare string (older/simpler configs,
    e.g. `model: ''`) and a mapping with the actual default under
    `default` (e.g. `model: {provider: nous, default: "...", ...}`,
    confirmed against a real, actively-used config.yaml) — handled here
    rather than assumed to be only one or the other."""
    from bot.hermes_config import HERMES_CONFIG_PATH

    path = HERMES_CONFIG_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _hermes_config_cache["mtime"] == mtime:
        return _hermes_config_cache["model"]
    try:
        import yaml

        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw_model = data.get("model")
        if isinstance(raw_model, dict):
            model = (raw_model.get("default") or "").strip() or None
        else:
            model = (raw_model or "").strip() or None
    except (OSError, AttributeError, yaml.YAMLError) as exc:
        logger.warning("local_hermes_default_model: failed to read %s: %s", path, exc)
        return None
    _hermes_config_cache["mtime"] = mtime
    _hermes_config_cache["model"] = model
    return model
