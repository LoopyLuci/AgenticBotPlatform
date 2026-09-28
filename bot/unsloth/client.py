"""Talking to Unsloth Studio: where it is, how to authenticate, and every operation it offers.

Studio is found from config/providers.yaml: a provider whose address (e.g. http://127.0.0.1:8888/v1) answers
GET /api/health as "Unsloth UI Backend" is Studio, and that provider's API key is Studio's key. `ABP_UNSLOTH_URL` and
`ABP_UNSLOTH_KEY` override both. Studio describes itself in /openapi.json (hundreds of operations: inference, models,
the Hugging Face hub, training, export, datasets, data recipes, RAG, MCP, settings...); `operations()` reads that, so
every one of them can be called by its operation id without ABP hard-coding them, and a new Studio release's new
features are reachable the day it ships.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx

SERVICE = "Unsloth UI Backend"
DEFAULT_URL = "http://127.0.0.1:8888"
READ_METHODS = ("GET", "HEAD")


class StudioError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class Studio:
    root: str                 # e.g. http://127.0.0.1:8888 (no /v1)
    key: Optional[str]
    provider: Optional[str]   # the providers.yaml name it was found under
    version: str = ""

    @property
    def openai_base(self) -> str:
        return self.root + "/v1"


_lock = threading.Lock()
_found: dict[str, Any] = {"at": 0.0, "studio": None}
_spec: dict[str, Any] = {"at": 0.0, "root": None, "spec": None}


def _root_of(url: str) -> str:
    u = urlparse(url.strip())
    return f"{u.scheme or 'http'}://{u.netloc}" if u.netloc else ""


def _health(root: str, timeout: float = 3.0) -> Optional[dict]:
    try:
        r = httpx.get(root + "/api/health", timeout=timeout)
        data = r.json() if r.status_code == 200 else None
        return data if isinstance(data, dict) and data.get("service") == SERVICE else None
    except (httpx.HTTPError, ValueError):
        return None


def find(*, refresh: bool = False) -> Optional[Studio]:
    """The Unsloth Studio this install uses, or None when none is running. Cached for a minute."""
    with _lock:
        if not refresh and time.monotonic() - _found["at"] < 60:
            return _found["studio"]
    studio = _discover()
    with _lock:
        _found.update(at=time.monotonic(), studio=studio)
    return studio



_refreshing = threading.Event()


def find_cached() -> Optional[Any]:
    """What find() last saw, without waiting: when that is over a minute old (or was never looked up) a refresh
    starts in the background. For checks that run on every agent turn (whether to offer the tools), which must
    never wait on the network."""
    with _lock:
        fresh = time.monotonic() - _found["at"] < 60
        value = _found["studio"]
    if not fresh and not _refreshing.is_set():
        _refreshing.set()

        def refresh() -> None:
            try:
                find(refresh=True)
            except Exception:  # noqa: BLE001
                pass
            finally:
                _refreshing.clear()
        threading.Thread(target=refresh, name="unsloth-find", daemon=True).start()
    return value


def _discover() -> Optional[Studio]:
    explicit = os.environ.get("ABP_UNSLOTH_URL", "").strip()
    candidates: list[tuple[str, Optional[str]]] = []
    if explicit:
        candidates.append((_root_of(explicit) or explicit.rstrip("/"), None))
    try:
        from bot import providers as registry

        for name, cfg in sorted(registry.list_providers().items()):
            root = _root_of(str((cfg or {}).get("base_url") or ""))
            if root and (name.lower().startswith("unsloth") or "8888" in root or root == _root_of(explicit)):
                candidates.append((root, name))
        # Any other local provider could be Studio too (it may be configured under another name).
        for name, cfg in sorted(registry.list_providers().items()):
            root = _root_of(str((cfg or {}).get("base_url") or ""))
            host = urlparse(root).hostname or ""
            if root and host in ("127.0.0.1", "localhost", "::1") and (root, name) not in candidates:
                candidates.append((root, name))
    except Exception:  # noqa: BLE001 — no provider store: fall through to the default address
        pass
    candidates.append((DEFAULT_URL, None))
    seen = set()
    for root, name in candidates:
        if root in seen:
            continue
        seen.add(root)
        health = _health(root)
        if health is None:
            continue
        key = os.environ.get("ABP_UNSLOTH_KEY", "").strip() or None
        if not key and name:
            try:
                from bot import providers as registry

                key = registry.get_api_key(name)
            except Exception:  # noqa: BLE001
                key = None
        if not key:
            # Found under no provider: use the key of any provider pointing at this address.
            try:
                from bot import providers as registry

                for other, cfg in registry.list_providers().items():
                    if _root_of(str((cfg or {}).get("base_url") or "")) == root:
                        key = registry.get_api_key(other)
                        name = name or other
                        if key:
                            break
            except Exception:  # noqa: BLE001
                pass
        return Studio(root=root, key=key, provider=name, version=str(health.get("version") or ""))
    return None


def require() -> Studio:
    studio = find()
    if studio is None:
        raise StudioError("Unsloth Studio is not running (nothing answered at a configured provider's address or "
                          f"{DEFAULT_URL}). Start Unsloth Studio, or add it on the Models page.")
    return studio


def _headers(studio: Studio) -> dict[str, str]:
    return {"Authorization": f"Bearer {studio.key}"} if studio.key else {}


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:500]
    if isinstance(data, dict):
        error = data.get("error")
        detail = data.get("detail") or (error.get("message") if isinstance(error, dict) else error)
        if isinstance(detail, list):
            return "; ".join(f"{'.'.join(str(x) for x in d.get('loc', []))}: {d.get('msg')}" for d in detail if isinstance(d, dict))[:500]
        if detail:
            return str(detail)[:500]
    return json.dumps(data)[:500]


def request(method: str, path: str, *, params: Optional[dict] = None, body: Any = None, timeout: float = 60.0,
            studio: Optional[Studio] = None, raw: bool = False, files: Optional[list] = None, form: Optional[dict] = None) -> Any:
    """One call to Studio. Returns the decoded JSON (or text); raises StudioError with Studio's own message. `files`
    ([(field, (filename, bytes))]) and `form` make it a multipart upload instead of a JSON body."""
    studio = studio or require()
    if not path.startswith("/"):
        path = "/" + path
    kwargs: dict[str, Any] = {"params": {k: v for k, v in (params or {}).items() if v is not None}, "headers": _headers(studio),
                              "timeout": timeout}
    if files is not None:
        kwargs.update(files=files, data={k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in (form or {}).items()
                                         if v is not None})
    else:
        kwargs["json"] = body if method.upper() not in READ_METHODS else None
    try:
        r = httpx.request(method.upper(), studio.root + path, **kwargs)
    except httpx.TimeoutException as exc:
        raise StudioError(f"Unsloth Studio did not answer {method} {path} within {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise StudioError(f"could not reach Unsloth Studio at {studio.root}: {exc}") from exc
    if r.status_code == 401:
        raise StudioError("Unsloth Studio refused the key (401). Put a Studio API key on the Unsloth provider (Models page); "
                          "Studio makes one under Settings > API.", 401)
    if r.status_code >= 400:
        raise StudioError(f"Unsloth Studio {method} {path} failed ({r.status_code}): {_error_text(r)}", r.status_code)
    if raw:
        return r
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        return r.json()
    return r.text


# ---- the parity layer: every operation Studio describes -------------------------------------------------------------------
def spec(*, refresh: bool = False, studio: Optional[Studio] = None) -> dict:
    """Studio's own OpenAPI description, cached for ten minutes (and on disk, for when it is briefly unreachable)."""
    studio = studio or require()
    with _lock:
        if not refresh and _spec["spec"] is not None and _spec["root"] == studio.root and time.monotonic() - _spec["at"] < 600:
            return _spec["spec"]
    try:
        data = request("GET", "/openapi.json", studio=studio, timeout=30)
        _cache_path().write_text(json.dumps(data), encoding="utf-8")
    except StudioError:
        cached = _cache_path()
        if not cached.is_file():
            raise
        data = json.loads(cached.read_text(encoding="utf-8"))
    with _lock:
        _spec.update(at=time.monotonic(), root=studio.root, spec=data)
    return data


def _cache_path():
    from bot.agent_runtime.state import state_dir

    return state_dir() / "unsloth_openapi.json"


def _resolve(schema: Any, components: dict, depth: int = 0) -> Any:
    """A schema with its $refs expanded (three levels deep; enough to show every field of a request)."""
    if depth > 3 or not isinstance(schema, (dict, list)):
        return schema
    if isinstance(schema, list):
        return [_resolve(s, components, depth) for s in schema]
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        target = dict(components.get(name) or {})
        target.setdefault("title", name)
        return _resolve(target, components, depth + 1)
    return {k: _resolve(v, components, depth) for k, v in schema.items()}


def _is_binary(s: Any) -> bool:
    if not isinstance(s, dict):
        return False
    if s.get("format") in ("binary", "base64") or s.get("contentMediaType") or s.get("contentEncoding"):
        return True
    if s.get("type") == "array":
        return _is_binary(s.get("items"))
    return any(_is_binary(x) for x in s.get("anyOf") or [])


def _file_fields(form: dict) -> list[dict]:
    """The file fields of an upload form: [{"name", "many"}]. Studio names them file/files where its schema is unclear."""
    out = []
    for name, s in ((form or {}).get("properties") or {}).items():
        if _is_binary(s) or name in ("file", "files"):
            many = s.get("type") == "array" or any(isinstance(x, dict) and x.get("type") == "array" for x in s.get("anyOf") or [])
            out.append({"name": name, "many": many})
    return out


def _group(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return "root"
    if parts[0] == "api" and len(parts) > 1:
        return parts[1]
    return parts[0]


def operations(*, studio: Optional[Studio] = None) -> list[dict]:
    """Every operation Studio offers: id, method, path, group, summary, parameters, request body and whether it changes
    anything (every method but GET and HEAD)."""
    data = spec(studio=studio)
    components = (data.get("components") or {}).get("schemas") or {}
    out = []
    for path, ops in (data.get("paths") or {}).items():
        for method, op in ops.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
                continue
            content = (op.get("requestBody") or {}).get("content") or {}
            body = (content.get("application/json") or {}).get("schema")
            multipart = bool(content.get("multipart/form-data"))
            form = _resolve((content.get("multipart/form-data") or {}).get("schema"), components) if multipart else None
            out.append({
                "id": op.get("operationId") or f"{method}_{path}",
                "method": method.upper(), "path": path, "group": _group(path),
                "summary": op.get("summary") or "", "description": (op.get("description") or "")[:2000],
                "params": [{"name": p.get("name"), "in": p.get("in"), "required": bool(p.get("required")),
                            "schema": p.get("schema") or {}} for p in op.get("parameters") or []
                           if p.get("in") in ("path", "query")],
                "body": _resolve(body, components) if body else None, "multipart": multipart,
                "form": form, "file_fields": _file_fields(form) if form else [],
                "mutating": method.upper() not in READ_METHODS,
            })
    out.sort(key=lambda o: (o["group"], o["path"], o["method"]))
    return out


def find_operation(ref: str, *, studio: Optional[Studio] = None) -> dict:
    """An operation by its id, or by "METHOD /path"."""
    ops = operations(studio=studio)
    m = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD)\s+(/\S*)$", ref.strip(), re.I)
    for op in ops:
        if op["id"] == ref or (m and op["method"] == m.group(1).upper() and op["path"] == m.group(2)):
            return op
    raise StudioError(f"Unsloth Studio has no operation {ref!r}")


def call(ref: str, args: Optional[dict] = None, *, timeout: float = 120.0, studio: Optional[Studio] = None) -> Any:
    """Call any Studio operation. `args` holds its path and query parameters by name; the JSON body is args["body"], or
    else every remaining argument."""
    studio = studio or require()
    op = find_operation(ref, studio=studio)
    args = dict(args or {})
    path = op["path"]
    params = {}
    uploads = args.pop("files", None) if op["multipart"] else None
    for p in op["params"]:
        name = p["name"]
        if p["in"] == "path":
            if name not in args:
                raise StudioError(f"{op['id']} needs the path parameter {name!r}")
            path = path.replace("{" + name + "}", quote(str(args.pop(name)), safe="/:@"))
        elif name in args:
            params[name] = args.pop(name)
        elif p["required"]:
            raise StudioError(f"{op['id']} needs the parameter {name!r}")
    if op["multipart"]:
        if not uploads:
            raise StudioError(f"{op['id']} uploads a file: pass files as {{field: [(filename, bytes), ...]}}")
        files = [(field, item) for field, items in uploads.items() for item in (items if isinstance(items, list) else [items])]
        return request(op["method"], path, params=params, files=files, form=args.pop("body", None) or args, timeout=timeout, studio=studio)
    body = args.pop("body", None)
    if body is None and op["body"] is not None and args:
        body = args
    return request(op["method"], path, params=params, body=body, timeout=timeout, studio=studio)


def _reset_for_tests() -> None:
    with _lock:
        _found.update(at=0.0, studio=None)
        _spec.update(at=0.0, root=None, spec=None)
