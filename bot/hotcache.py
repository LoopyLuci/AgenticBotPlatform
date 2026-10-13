"""ABP's hot paths on CacheIt (roadmap CI-E): web fetches, embeddings and provider model lists, each opt-in.

config/backends.yaml:

    cache:
      web_fetch: false          # GET page fetches by URL, 2xx only, nothing sent with credentials
      embeddings: false         # key = hash(model id + text)
      model_lists: false        # key = provider + base URL + a hash of the key (never the key)
      web_fetch_ttl_s: 300
      embeddings_ttl_s: 2592000
      model_lists_ttl_s: 600
      web_fetch_max_bytes: 1000000
      embeddings_max_lookups: 64   # bigger batches skip the per-text lookups

Everything here is a miss when a feature is off or CacheIt isn't running, so ABP behaves exactly as it did without
it. bot/cache.py is synchronous HTTP, so async callers use the a* helpers (asyncio.to_thread). Keys are hashes and
values are the public payloads; secrets are never stored. Hit/miss counters per feature are in-process and appear in
bot.cache.stats()["hot_paths"].
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
from typing import Any, Callable

from bot import cache

FEATURES = ("web_fetch", "embeddings", "model_lists")
_DEFAULT_TTL = {"web_fetch": 300, "embeddings": 30 * 86400, "model_lists": 600}
DEFAULT_MAX_BYTES = 1_000_000
#: CacheIt has no multi-get, so a batch costs one GET per text; above this many texts the lookups are skipped (the
#: embedder gets the whole batch, and its vectors are stored) rather than adding a round trip per text.
DEFAULT_MAX_LOOKUPS = 64

_lock = threading.Lock()
_counters: dict[str, dict[str, int]] = {f: {"hits": 0, "misses": 0, "puts": 0} for f in FEATURES}


def _block() -> dict:
    try:
        from bot.config import config
        block = config.current.get("cache")
    except Exception:  # noqa: BLE001  (no config: everything stays off)
        return {}
    return block if isinstance(block, dict) else {}


def enabled(feature: str) -> bool:
    return _block().get(feature) is True


def ttl_s(feature: str) -> int:
    try:
        v = int(_block().get(f"{feature}_ttl_s") or _DEFAULT_TTL[feature])
    except (TypeError, ValueError):
        v = _DEFAULT_TTL[feature]
    return v if v > 0 else _DEFAULT_TTL[feature]


def max_bytes() -> int:
    try:
        v = int(_block().get("web_fetch_max_bytes") or DEFAULT_MAX_BYTES)
    except (TypeError, ValueError):
        v = DEFAULT_MAX_BYTES
    return v if v > 0 else DEFAULT_MAX_BYTES


def max_lookups() -> int:
    try:
        v = int(_block().get("embeddings_max_lookups") or DEFAULT_MAX_LOOKUPS)
    except (TypeError, ValueError):
        v = DEFAULT_MAX_LOOKUPS
    return v if v > 0 else DEFAULT_MAX_LOOKUPS


def digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _count(feature: str, what: str) -> None:
    with _lock:
        _counters[feature][what] += 1


def get(feature: str, key: str) -> Any:
    """The cached JSON value, or None (also when the feature is off). Counts a hit or a miss."""
    if not enabled(feature):
        return None
    value = cache.get_json(feature, key)
    _count(feature, "misses" if value is None else "hits")
    return value


def put(feature: str, key: str, value: Any) -> bool:
    if not enabled(feature) or value is None:
        return False
    ok = cache.put_json(feature, key, value, ttl_s(feature))
    if ok:
        _count(feature, "puts")
    return ok


async def aget(feature: str, key: str) -> Any:
    if not enabled(feature):
        return None
    return await asyncio.to_thread(get, feature, key)


async def aput(feature: str, key: str, value: Any) -> bool:
    if not enabled(feature) or value is None:
        return False
    return await asyncio.to_thread(put, feature, key, value)


def embed_cached(model_id: str, fn: Callable[[list[str]], Any], texts: list[str]) -> Any:
    """fn(texts) with each text's vector cached under hash(model id + text); only the missing texts reach fn. Returns
    what fn returns (None if the embedder fails). A batch of more than max_lookups() texts skips the lookups (one GET
    each; CacheIt has no multi-get) and only stores."""
    if not enabled("embeddings") or not texts:
        return fn(texts)
    import numpy as np
    keys = [digest(model_id, t) for t in texts]
    if len(texts) > max_lookups():
        found: list = [None] * len(texts)
    else:
        found = [get("embeddings", k) for k in keys]
    todo = [i for i, v in enumerate(found) if v is None]
    if todo:
        fresh = fn([texts[i] for i in todo])
        if fresh is None:
            return None
        for i, v in zip(todo, fresh):
            found[i] = [float(x) for x in v]
            put("embeddings", keys[i], found[i])
    return np.asarray(found, dtype=np.float32)


def stats() -> dict:
    with _lock:
        out = {f: dict(c) for f, c in _counters.items()}
    for f, c in out.items():
        total = c["hits"] + c["misses"]
        c["enabled"] = enabled(f)
        c["hit_rate"] = round(c["hits"] / total, 3) if total else None
    return out


def reset_stats() -> None:
    with _lock:
        for c in _counters.values():
            for k in c:
                c[k] = 0

