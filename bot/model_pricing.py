"""Real per-model pricing for the custom_model/native_agent provider
families — a family that had ZERO pricing/free-classification data
before this module existed (confirmed: bot.models.live_custom_models()
returns bare model ids only). Sourced from models.dev's public catalog
(https://models.dev/api.json, confirmed live: a flat {provider_id:
{models: {model_id: {..., "cost": {"input": $/1M tokens, "output": $/1M
tokens}}}}} shape — verified against the real endpoint, not assumed),
the same general pricing source Hermes Agent's own code falls back to
for providers it has no bespoke fetcher for. BotServer goes straight to
this one general source for every provider rather than accumulating
Hermes's per-provider bespoke fetchers (OpenRouter-specific, Nous-
specific, etc.) — one real public feed already covers the practical
space this app cares about.

Prices are converted from models.dev's $-per-million-tokens convention
to $-per-token on the way out, matching bot.swarm_budget's existing
`estimate_dispatch_cost()` formula (`tokens * price`) and
bot.models.hermes_models_with_pricing()'s established pricing_row shape
— every pricing consumer in this app assumes $/token, so this is the
one place that conversion happens.

Matches hermes_models_with_pricing()'s three-value source vocabulary
("live"/"cache_fallback"/"unavailable") for consistency across the app.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.model_pricing")

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_PATH = PROJECT_ROOT / "data" / "models_dev_cache.json"
CACHE_TTL_S = 24 * 3600.0
_MILLION = 1_000_000.0

_memory_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


async def _fetch_live() -> Optional[dict]:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(MODELS_DEV_URL)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.info("models.dev fetch failed: %s", exc)
        return None


def _read_disk_cache() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_disk_cache(data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        logger.exception("failed to write models.dev disk cache")


async def _catalog(refresh: bool = False) -> tuple[Optional[dict], str]:
    now = time.time()
    if not refresh and _memory_cache["data"] is not None and (now - _memory_cache["fetched_at"]) < CACHE_TTL_S:
        return _memory_cache["data"], "live"

    live = await _fetch_live()
    if live is not None:
        _memory_cache["data"] = live
        _memory_cache["fetched_at"] = now
        _write_disk_cache(live)
        return live, "live"

    disk = _read_disk_cache()
    if disk is not None:
        return disk, "cache_fallback"
    return None, "unavailable"


async def get_pricing(provider: str, model_id: str, *, refresh: bool = False) -> tuple[Optional[dict], str]:
    """`{"free": bool, "input": float, "output": float}` ($/token) for
    `provider`/`model_id` from models.dev, plus a source label
    ("live"/"cache_fallback"/"unavailable"). Returns (None, source) when
    the catalog itself couldn't be loaded at all, OR when this exact
    provider/model isn't listed in it, OR when it's listed with no usable
    `cost` figures (e.g. a synthetic routing entry like
    "openrouter/auto") — never raises, mirroring
    hermes_models_with_pricing()'s "always degrade, never crash" shape."""
    catalog, source = await _catalog(refresh=refresh)
    if catalog is None:
        return None, "unavailable"

    provider_entry = catalog.get(provider)
    if not isinstance(provider_entry, dict):
        return None, source
    model_entry = (provider_entry.get("models") or {}).get(model_id)
    if not isinstance(model_entry, dict):
        return None, source

    cost = model_entry.get("cost")
    if not isinstance(cost, dict):
        return None, source
    input_per_mtok = cost.get("input")
    output_per_mtok = cost.get("output")
    if input_per_mtok is None or output_per_mtok is None:
        return None, source

    input_per_token = input_per_mtok / _MILLION
    output_per_token = output_per_mtok / _MILLION
    return {"free": input_per_mtok == 0 and output_per_mtok == 0, "input": input_per_token, "output": output_per_token}, source
