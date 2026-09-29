"""ABP's client for CacheIt (the `cacheit` module): a tiered, crash-safe cache (RAM, then disk) shared by everything
ABP runs on this machine.

    from bot import cache
    cache.put("web", url, body, ttl_s=600)
    body = cache.get("web", url)                                   # bytes, or None
    models = cache.get_or_compute("models", "openrouter", fetch_models, ttl_s=900)   # JSON-serializable values

When CacheIt isn't installed or its hub isn't running, every call is a miss (get returns None, put does nothing,
get_or_compute just computes): the cache can only ever make ABP faster, never break or slow it. Calls use short
timeouts for the same reason. Keys longer than 200 characters are stored under their SHA-256.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger("bot.cache")
TIMEOUT_S = 2.0
_hub: dict[str, Any] = {"at": 0.0, "url": None, "token": None}
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "puts": 0, "errors": 0}
# One long-lived client: connection reuse, and no per-call setup (a new httpx client per call costs ~0.1-0.2 s).
_http = httpx.Client(timeout=TIMEOUT_S, limits=httpx.Limits(max_keepalive_connections=8, max_connections=16))


def _find() -> tuple[Optional[str], Optional[str]]:
    """The running hub's url and token (looked up at most every 10 s)."""
    with _lock:
        if time.monotonic() - _hub["at"] < 10:
            return _hub["url"], _hub["token"]
    url = token = None
    try:
        from bot.modules import client, registry
        m = registry.get("cacheit")
        hub = client.find(m, timeout=1.0)
        if hub is not None:
            url, token = hub.url, hub.token
    except Exception:  # noqa: BLE001  (not installed, no manifest, not running: all mean "no cache")
        pass
    with _lock:
        _hub.update(at=time.monotonic(), url=url, token=token)
    return url, token


def available() -> bool:
    return _find()[0] is not None


def _key(key: str) -> str:
    return key if len(key) <= 200 else "h:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _obj_url(url: str, ns: str, key: str) -> str:
    return f"{url}/v1/objects/{urllib.parse.quote(ns, safe='')}/{urllib.parse.quote(_key(key), safe='')}"


def _failed(what: str, e: Exception) -> None:
    _stats["errors"] += 1
    logger.debug("cache %s failed: %s", what, e)
    with _lock:
        _hub["at"] = 0.0                    # look for the hub again next time


def get(ns: str, key: str) -> Optional[bytes]:
    url, token = _find()
    if not url:
        _stats["misses"] += 1
        return None
    try:
        r = _http.get(_obj_url(url, ns, key), headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_S)
    except httpx.HTTPError as e:
        _failed("get", e)
        _stats["misses"] += 1
        return None
    if r.status_code == 200:
        _stats["hits"] += 1
        return r.content
    _stats["misses"] += 1
    return None


def put(ns: str, key: str, value: bytes | str, ttl_s: Optional[int] = None, pin: bool = False) -> bool:
    url, token = _find()
    if not url:
        return False
    data = value.encode("utf-8") if isinstance(value, str) else value
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
    if ttl_s:
        headers["X-TTL-Seconds"] = str(int(ttl_s))
    if pin:
        headers["X-Pin"] = "1"
    try:
        r = _http.put(_obj_url(url, ns, key), content=data, headers=headers, timeout=max(TIMEOUT_S, len(data) / 50e6))
    except httpx.HTTPError as e:
        _failed("put", e)
        return False
    if r.status_code == 200:
        _stats["puts"] += 1
        return True
    return False


def delete(ns: str, key: str) -> bool:
    url, token = _find()
    if not url:
        return False
    try:
        r = _http.delete(_obj_url(url, ns, key), headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_S)
        return r.status_code == 200 and bool(r.json().get("deleted"))
    except (httpx.HTTPError, ValueError) as e:
        _failed("delete", e)
        return False


def get_json(ns: str, key: str) -> Any:
    raw = get(ns, key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def put_json(ns: str, key: str, value: Any, ttl_s: Optional[int] = None) -> bool:
    return put(ns, key, json.dumps(value, separators=(",", ":"), default=str), ttl_s)


_MISSING = object()


def get_or_compute(ns: str, key: str, compute: Callable[[], Any], ttl_s: Optional[int] = None) -> Any:
    """The cached value, or compute() stored for next time (the value must be JSON-serializable). A value of None is
    not cached."""
    raw = get(ns, key)
    if raw is not None:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    value = compute()
    if value is not None:
        put_json(ns, key, value, ttl_s)
    return value


def stats() -> dict:
    """This process's use of the cache, and the hub's own stats when it is running."""
    out: dict[str, Any] = {"available": available(), "client": dict(_stats)}
    url, token = _find()
    if url:
        try:
            r = _http.post(f"{url}/v1/call/cache.stats", json={}, headers={"Authorization": f"Bearer {token}"},
                           timeout=TIMEOUT_S)
            out["hub"] = r.json().get("result")
        except (httpx.HTTPError, ValueError):
            pass
    return out
