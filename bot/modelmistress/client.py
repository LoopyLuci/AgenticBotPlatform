"""Talking to a ModelMistress hub: where it is, its operations (`POST /v1/call/<op>`) and its OpenAI routes.

The hub is found, in this order:
  1. `ABP_MODELMISTRESS_URL` (+ `ABP_MODELMISTRESS_TOKEN`), then `modelmistress.url` in the config (+ `token_env` / `token`);
  2. the module's control file (`<module data>/control.json`, written by a running hub), checked with GET /v1/health.
A linked peer's hub is `modelmistress.peers.<name>` ({url, key_env | key}); the key is the peer key that machine's owner
made for this one. A peer key may use only the OpenAI routes (models, chat, completions, embeddings), so operations
(pull, load...) work on a local hub only.

The token or key stays in this process: never shown, logged or sent anywhere but its hub.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

MODULE_ID = "modelmistress"
PROVIDER = "modelmistress"


class MMError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, code: str = "error"):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class Hub:
    url: str
    token: str
    version: str = ""
    peer: str = ""          # the linked machine's name, "" for this machine's hub

    @property
    def openai_base(self) -> str:
        return self.url + "/v1"

    @property
    def remote(self) -> bool:
        return bool(self.peer)


def _cfg() -> dict:
    try:
        from bot.config import config

        c = config.current.get("modelmistress")
        return c if isinstance(c, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _secret(spec: dict, env_key: str, inline_key: str) -> str:
    env = str(spec.get(env_key) or "").strip()
    if env:
        return os.environ.get(env, "")
    return str(spec.get(inline_key) or "")


def prefer_enabled() -> bool:
    return _cfg().get("prefer", True) is not False and not os.environ.get("ABP_NO_MODELMISTRESS")


def _health(url: str, timeout: float = 2.0) -> Optional[dict]:
    try:
        r = httpx.get(url + "/v1/health", timeout=timeout)
        data = r.json() if r.status_code == 200 and r.content else None
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _configured() -> Optional[tuple[str, str]]:
    url = os.environ.get("ABP_MODELMISTRESS_URL", "").strip()
    if url:
        return url.rstrip("/"), os.environ.get("ABP_MODELMISTRESS_TOKEN", "")
    cfg = _cfg()
    url = str(cfg.get("url") or "").strip()
    if url:
        return url.rstrip("/"), _secret(cfg, "token_env", "token")
    return None


def _from_module() -> Optional[Hub]:
    if os.environ.get("ABP_NO_MODULE_PROVIDERS"):       # tests: never pick up a hub that happens to run on this machine
        return None
    try:
        from bot.modules import client as modules_client
        from bot.modules import registry

        h = modules_client.find(registry.get(MODULE_ID))
    except Exception:  # noqa: BLE001 - no such module, or a broken control file: no hub
        return None
    return Hub(h.url, h.token, h.version) if h else None


def _discover(peer: str = "") -> Optional[Hub]:
    if peer:
        spec = (_cfg().get("peers") or {}).get(peer)
        if not isinstance(spec, dict) or not spec.get("url"):
            return None
        url = str(spec["url"]).rstrip("/")
        health = _health(url, 3.0)
        return Hub(url, _secret(spec, "key_env", "key"), str((health or {}).get("version") or ""), peer) if health else None
    if os.environ.get("ABP_NO_MODELMISTRESS"):
        return None
    given = _configured()
    if given:
        health = _health(given[0])
        if health:
            return Hub(given[0], given[1], str(health.get("version") or ""))
    return _from_module()


_lock = threading.Lock()
_found: dict[str, tuple[float, Optional[Hub]]] = {}
TTL_S = 20.0


def find(peer: str = "", *, refresh: bool = False) -> Optional[Hub]:
    """The hub this install uses (`peer`: that linked machine's), or None when none answers. Cached for 20 seconds."""
    with _lock:
        hit = _found.get(peer)
        if hit and not refresh and time.monotonic() - hit[0] < TTL_S:
            return hit[1]
    value = _discover(peer)
    with _lock:
        _found[peer] = (time.monotonic(), value)
    return value


_refreshing = threading.Event()


def find_cached() -> Optional[Hub]:
    """What find() last saw, without waiting; a refresh starts in the background when that is stale. For checks that run
    on every agent turn (whether to offer the tools), which must never wait on the network."""
    with _lock:
        hit = _found.get("")
    fresh = hit is not None and time.monotonic() - hit[0] < TTL_S
    if not fresh and not _refreshing.is_set():
        _refreshing.set()

        def refresh() -> None:
            try:
                find(refresh=True)
            except Exception:  # noqa: BLE001
                pass
            finally:
                _refreshing.clear()
        threading.Thread(target=refresh, name="modelmistress-find", daemon=True).start()
    return hit[1] if hit else None


def forget() -> None:
    with _lock:
        _found.clear()


def require(peer: str = "") -> Hub:
    hub = find(peer)
    if hub is None:
        if peer:
            raise MMError(f"no ModelMistress hub answers for peer {peer!r} (modelmistress.peers.{peer} in the config)",
                          code="unavailable")
        raise MMError("ModelMistress is not running (start its hub from the Modules page)", code="unavailable")
    return hub


def _error(r: httpx.Response) -> MMError:
    try:
        data = r.json()
    except ValueError:
        data = None
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return MMError(str(err.get("message") or f"HTTP {r.status_code}"), r.status_code, str(err.get("code") or "http"))
    return MMError(str(err or f"HTTP {r.status_code}")[:500], r.status_code, "http")


def _headers(hub: Hub) -> dict:
    return {"Authorization": f"Bearer {hub.token}", "X-ABP-Client": "abp"} if hub.token else {"X-ABP-Client": "abp"}


def request(method: str, path: str, *, body: Any = None, hub: Optional[Hub] = None, timeout: float = 120.0) -> Any:
    h = hub or require()
    try:
        r = httpx.request(method, h.url + path, json=body, timeout=timeout, headers=_headers(h))
    except httpx.HTTPError as e:
        raise MMError(f"the ModelMistress hub at {h.url} is not reachable: {e}", code="unavailable") from None
    if r.status_code >= 400:
        raise _error(r)
    try:
        return r.json() if r.content else None
    except ValueError:
        return {"text": r.text[:4000]}


def call(op: str, args: Optional[dict] = None, *, hub: Optional[Hub] = None, timeout: float = 120.0) -> Any:
    """Run one of the hub's operations (model.list, model.load, model.pull, service.status...); the result."""
    h = hub or require()
    if h.remote:
        raise MMError("a linked machine's hub accepts only the OpenAI routes with a peer key (models, chat, "
                      "completions, embeddings); load or pull on that machine", 403, "forbidden")
    data = request("POST", f"/v1/call/{op}", body=dict(args or {}), hub=h, timeout=timeout)
    return data.get("result") if isinstance(data, dict) and "result" in data else data


def models(hub: Optional[Hub] = None) -> list[dict]:
    """The OpenAI model list (`GET /v1/models`: what this key may use, on a peer's hub)."""
    data = request("GET", "/v1/models", hub=hub, timeout=15.0)
    return [m for m in (data or {}).get("data") or [] if isinstance(m, dict) and m.get("id")]


def chat(model: str, messages: list, *, hub: Optional[Hub] = None, timeout: float = 600.0, **options: Any) -> dict:
    """A whole (not streamed) chat answer in OpenAI's shape."""
    body = {"model": model, "messages": messages, "stream": False}
    body.update({k: v for k, v in options.items() if v is not None})
    return request("POST", "/v1/chat/completions", body=body, hub=hub, timeout=timeout)


def provider_running() -> bool:
    """ModelMistress is a provider ABP can route to (the module hub running, or a provider of that name configured),
    and `modelmistress.prefer` is not off. This is what makes the local callers prefer it over Ollama and Unsloth."""
    if not prefer_enabled():
        return False
    try:
        from bot import providers as registry

        return PROVIDER in registry.list_providers()
    except Exception:  # noqa: BLE001
        return False


def replaced_by_modelmistress(name: str, pcfg: Optional[dict] = None) -> bool:
    """True for a local-model provider ModelMistress replaces (Ollama, Unsloth Studio) while ModelMistress is available.
    ModelMistress serves the Ollama store and the Hugging Face cache itself, so their models are in its list."""
    if name == PROVIDER or not provider_running():
        return False
    base = str((pcfg or {}).get("base_url") or "").lower()
    n = name.lower()
    return "ollama" in n or ":11434" in base or "unsloth" in n or ":8888" in base
