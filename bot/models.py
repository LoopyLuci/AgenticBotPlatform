"""Known model choices per backend, for validation and dashboard dropdowns.

Static fallback lives in KNOWN_MODELS below. Prefer the live_* functions —
they fetch what's actually available right now (Anthropic's own /v1/models
for the api backend, Hermes Agent's own live provider model cache for the
two Hermes backends) and only fall back to the static list when a live
fetch isn't possible (no API key, no Hermes install, network error).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bot.models")

KNOWN_MODELS: dict[str, list[str]] = {
    "api": [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-haiku-4-5-20251001",
    ],
}

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
}

_API_CACHE_TTL_S = 300.0
_api_cache: dict = {"at": 0.0, "models": None}


async def live_api_models() -> Optional[list[str]]:
    """Live model IDs from Anthropic's own /v1/models, via the same
    ANTHROPIC_API_KEY the api backend already uses. Returns None (never
    raises) if there's no key configured or the call fails, so callers can
    fall back to KNOWN_MODELS["api"] — this is a nice-to-have, not a
    dependency anything else should break on."""
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
