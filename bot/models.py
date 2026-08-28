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


async def live_custom_models() -> Optional[dict[str, list[str]]]:
    """{provider_name: [model_ids]} for every provider configured in
    config/providers.yaml, fetched live from that provider's own
    OpenAI-compatible /models endpoint (Ollama, LM Studio, vLLM, and
    llama.cpp's server all implement this) — same "no hardcoded catalog,
    ever" rule live_api_models() follows, just for custom endpoints.
    A provider whose fetch fails is simply omitted, not treated as a
    reason to fail the whole call — one dead local server shouldn't hide
    every other configured provider's models."""
    from bot import providers as provider_registry

    configured = provider_registry.list_providers()
    if not configured:
        return None

    now = time.monotonic()
    grouped: dict[str, list[str]] = {}
    for name, entry in configured.items():
        cached = _custom_cache.get(name)
        if cached is not None and (now - cached["at"]) < _CUSTOM_CACHE_TTL_S:
            if cached["models"] is not None:
                grouped[name] = cached["models"]
            continue
        models = await _fetch_custom_models(name, entry, provider_registry)
        _custom_cache[name] = {"at": now, "models": models}
        if models is not None:
            grouped[name] = models
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
    """Hermes Agent's own configured default model (~/.hermes/config.yaml's
    top-level "model" key), or None if the file/field doesn't exist or is
    left blank — Hermes then decides its own default at runtime with
    nothing local for us to read. Re-read only when the file's mtime
    changes."""
    path = Path.home() / ".hermes" / "config.yaml"
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
        model = (data.get("model") or "").strip() or None
    except (OSError, AttributeError, yaml.YAMLError) as exc:
        logger.warning("local_hermes_default_model: failed to read %s: %s", path, exc)
        return None
    _hermes_config_cache["mtime"] = mtime
    _hermes_config_cache["model"] = model
    return model
