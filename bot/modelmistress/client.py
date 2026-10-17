"""Talking to a ModelMistress hub: where it is, its operations (`POST /v1/call/<op>`) and its OpenAI routes.

The hub is found, in this order:
  1. `ABP_MODELMISTRESS_URL` (+ `ABP_MODELMISTRESS_TOKEN`), then `modelmistress.url` in the config (+ `token_env` / `token`);
  2. the module's control file (`<module data>/control.json`, written by a running hub), then the hub's own home
     (`modelmistress.home`, `$MM_HOME`, `LOCALAPPDATA/ModelMistress`, `~/.local/share/ModelMistress`: where the desktop
     app and a headless `model-mistress serve` write theirs). Each is checked with GET /v1/health (and its pid); a stale
     file is skipped and the first live hub wins.
A linked peer's hub is `modelmistress.peers.<name>` ({url, key_env | key}); the key is the peer key that machine's owner
made for this one. A peer key may use only the OpenAI routes (models, chat, completions, embeddings), so operations
(pull, load...) work on a local hub only.

The token or key stays in this process: never shown, logged or sent anywhere but its hub.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
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


def mm_home() -> Path:
    """The hub's own home (control file, model store): `modelmistress.home`, `$MM_HOME`, `LOCALAPPDATA/ModelMistress`
    on Windows, else `~/.local/share/ModelMistress`. The desktop app, a headless hub and ABP share this one."""
    given = str(_cfg().get("home") or "").strip() or os.environ.get("MM_HOME", "").strip()
    if given:
        return Path(given).expanduser()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local:
        return Path(local) / "ModelMistress"
    return Path.home() / ".local" / "share" / "ModelMistress"


def _from_control_file(path: Path) -> Optional[Hub]:
    """The hub a control file names, when it answers /v1/health (and is the pid the file says); None for a stale file."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        url, token = str(d["url"]).rstrip("/"), str(d.get("token") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    health = _health(url)
    if not health:
        return None
    want, got = d.get("pid"), health.get("pid")
    try:
        if want and got and int(want) != int(got):
            return None
    except (TypeError, ValueError):
        return None
    return Hub(url, token, str(health.get("version") or d.get("version") or ""))


def _from_module() -> Optional[Hub]:
    if os.environ.get("ABP_NO_MODULE_PROVIDERS"):       # tests: never pick up a hub that happens to run on this machine
        return None
    try:
        from bot.modules import client as modules_client
        from bot.modules import registry

        h = modules_client.find(registry.get(MODULE_ID))
    except Exception:  # noqa: BLE001 - no such module, or a broken control file: no hub
        h = None
    if h:
        return Hub(h.url, h.token, h.version)
    try:
        return _from_control_file(mm_home() / "control.json")
    except Exception:  # noqa: BLE001
        return None


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
    if hub is None and not peer:
        try:        # one try to start it (modelmistress.autostart on, not a sandbox); a hub that is already up is reused
            from bot.modelmistress import supervisor

            hub = supervisor.ensure(refresh=True)
        except Exception:  # noqa: BLE001
            hub = None
    if hub is None:
        if peer:
            raise MMError(f"no ModelMistress hub answers for peer {peer!r} (modelmistress.peers.{peer} in the config)",
                          code="unavailable")
        raise MMError("ModelMistress is not running (autostart is off or failed; start its hub from the Modules page)", code="unavailable")
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


def ollama_allowed() -> bool:
    """`modelmistress.allow_ollama` (default false): Ollama stays on the default path even while a hub runs."""
    return _cfg().get("allow_ollama", False) is True


def unsloth_allowed() -> bool:
    """`modelmistress.allow_unsloth` (default false): Unsloth Studio stays on the default path (and may auto-load)."""
    return _cfg().get("allow_unsloth", False) is True


def fallback_kind(name: str, pcfg: Optional[dict] = None) -> str:
    """"ollama" / "unsloth" for those local-model providers, else ""."""
    base = str((pcfg or {}).get("base_url") or "").lower()
    n = name.lower()
    if "ollama" in n or ":11434" in base:
        return "ollama"
    if "unsloth" in n or ":8888" in base:
        return "unsloth"
    return ""


def hub_available() -> bool:
    """A hub is up, by the provider registry or by the last (cached) discovery; never waits on the network."""
    if provider_running():
        return True
    return prefer_enabled() and find_cached() is not None


def replaced_by_modelmistress(name: str, pcfg: Optional[dict] = None) -> bool:
    """True for a local-model provider ModelMistress replaces (Ollama, Unsloth Studio) while ModelMistress is available and
    the owner has not opted that provider in (`modelmistress.allow_ollama` / `allow_unsloth`). With the hub down they are the
    fallback and stay available. ModelMistress serves the Ollama store and the Hugging Face cache itself."""
    if name == PROVIDER:
        return False
    kind = fallback_kind(name, pcfg)
    if not kind or (ollama_allowed() if kind == "ollama" else unsloth_allowed()):
        return False
    return hub_available()


# ---- the context window a model is loaded with ----------------------------------------------------------------
_ctx_lock = threading.Lock()
_ctx_cache: dict[tuple[str, str], tuple[float, Optional[int]]] = {}
CTX_TTL_S = 30.0          # a loaded model's window changes only when it is reloaded
CTX_MISS_TTL_S = 10.0     # a hub that said nothing is asked again soon
_CTX_KEYS = ("n_ctx", "ctx_size", "context_length", "context", "num_ctx")


def _positive(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _ctx_of(entry: dict, keys: tuple = _CTX_KEYS) -> Optional[int]:
    for source in (entry, entry.get("metadata"), entry.get("meta"), entry.get("runtime")):
        if isinstance(source, dict):
            for k in keys:
                n = _positive(source.get(k))
                if n:
                    return n
    return None


def _same_model(entry: dict, model: str) -> bool:
    want = model.lower()
    return any(str(entry.get(k) or "").lower() == want for k in ("id", "name", "model"))


def _lookup_context(hub: Hub, model: str) -> Optional[int]:
    """The hub's figure for `model`, in order: (1) its list of loaded models, which carries the window each was loaded
    with; (2) an `n_ctx` / `ctx_size` field on its OpenAI model list (`context_length` there is the model's trained
    maximum, not what it runs with, and is not used); (3) llama-server's /props through the hub, when exposed."""
    if not hub.remote:
        try:
            loaded = call("model.loaded", hub=hub, timeout=2.5)
            rows = loaded.get("models") if isinstance(loaded, dict) else loaded
            for row in rows or []:
                if isinstance(row, dict) and _same_model(row, model):
                    n = _ctx_of(row)
                    if n:
                        return n
        except Exception:  # noqa: BLE001 - an op this hub does not have, or no answer
            pass
    try:
        for row in models(hub):
            if _same_model(row, model):
                n = _ctx_of(row, ("n_ctx", "ctx_size", "loaded_context", "num_ctx"))
                if n:
                    return n
    except Exception:  # noqa: BLE001
        pass
    for path in ("/props", "/v1/props"):
        try:
            r = httpx.get(hub.url + path, params={"model": model}, headers=_headers(hub), timeout=2.5)
            data = r.json() if r.status_code == 200 and r.content else None
        except (httpx.HTTPError, ValueError):
            continue
        if isinstance(data, dict):
            settings = data.get("default_generation_settings")
            n = _positive((settings or {}).get("n_ctx")) if isinstance(settings, dict) else None
            n = n or _positive(data.get("n_ctx"))
            if n:
                return n
    return None


_ctx_inflight: set[tuple[str, str]] = set()


def _refresh_context(h: Hub, model: str, key: tuple[str, str]) -> Optional[int]:
    try:
        value = _lookup_context(h, model)
    except Exception:  # noqa: BLE001 - a broken hub answers "unknown", never an exception to the caller
        value = None
    with _ctx_lock:
        _ctx_cache[key] = (time.monotonic(), value)
        _ctx_inflight.discard(key)
    return value


def context_length(model: str, *, hub: Optional[Hub] = None, wait: bool = True) -> Optional[int]:
    """The context window (n_ctx) the hub runs `model` with, or None when it cannot say (no hub, model not loaded, a hub
    that does not report it). Cached briefly, misses too, so a turn never waits on the hub more than once per interval.

    wait=False never blocks (the agent loop calls this from async code): it answers from the cache, even a stale entry,
    and when the entry is missing or stale it asks the hub on a background thread for next time."""
    h = hub or (find() if wait else find_cached())
    if h is None:
        return None
    key = (h.url, model)
    now = time.monotonic()
    with _ctx_lock:
        hit = _ctx_cache.get(key)
        fresh = bool(hit) and now - hit[0] < (CTX_TTL_S if hit[1] else CTX_MISS_TTL_S)
        if fresh:
            return hit[1]
        if not wait:
            if key not in _ctx_inflight:
                _ctx_inflight.add(key)
                threading.Thread(target=_refresh_context, args=(h, model, key), name="mm-context-length",
                                 daemon=True).start()
            return hit[1] if hit else None
    return _refresh_context(h, model, key)


def forget_context() -> None:
    with _ctx_lock:
        _ctx_cache.clear()
