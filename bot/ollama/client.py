"""Talking to Ollama: where it is, plain and streamed calls, and every route it serves.

Ollama is found from config/providers.yaml: a provider whose address (e.g. http://127.0.0.1:11434/v1) answers GET
/api/version is Ollama. `ABP_OLLAMA_URL` overrides that; the default is http://127.0.0.1:11434.

Ollama publishes no machine-readable API description, so OPERATIONS lists every route Ollama 0.34 serves (read from its
own binary and its API documentation), with the fields each takes. `call()` reaches any of them by id, and a route this
table does not know is still reachable by "METHOD /path".
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import httpx

DEFAULT_URL = "http://127.0.0.1:11434"
READ_METHODS = ("GET", "HEAD")


class OllamaError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class Ollama:
    root: str
    key: Optional[str]
    provider: Optional[str]
    version: str = ""

    @property
    def openai_base(self) -> str:
        return self.root + "/v1"


_lock = threading.Lock()
_found: dict[str, Any] = {"at": 0.0, "value": None}


def _root_of(url: str) -> str:
    u = urlparse(url.strip())
    return f"{u.scheme or 'http'}://{u.netloc}" if u.netloc else ""


def _version(root: str, timeout: float = 3.0) -> Optional[str]:
    try:
        r = httpx.get(root + "/api/version", timeout=timeout)
        data = r.json() if r.status_code == 200 else None
        return str(data["version"]) if isinstance(data, dict) and data.get("version") else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def find(*, refresh: bool = False) -> Optional[Ollama]:
    """The Ollama this install uses, or None when none is running. Cached for a minute."""
    with _lock:
        if not refresh and time.monotonic() - _found["at"] < 60:
            return _found["value"]
    value = _discover()
    with _lock:
        _found.update(at=time.monotonic(), value=value)
    return value


def _discover() -> Optional[Ollama]:
    candidates: list[tuple[str, Optional[str]]] = []
    explicit = os.environ.get("ABP_OLLAMA_URL", "").strip()
    if explicit:
        candidates.append((_root_of(explicit) or explicit.rstrip("/"), None))
    try:
        from bot import providers as registry

        items = sorted(registry.list_providers().items())
        # Named for Ollama first, then anything on port 11434.
        for name, cfg in items:
            root = _root_of(str((cfg or {}).get("base_url") or ""))
            if root and ("ollama" in name.lower() or ":11434" in root):
                candidates.append((root, name))
    except Exception:  # noqa: BLE001
        pass
    candidates.append((DEFAULT_URL, None))
    seen = set()
    for root, name in candidates:
        if root in seen:
            continue
        seen.add(root)
        version = _version(root)
        if version is None:
            continue
        key = None
        if name:
            try:
                from bot import providers as registry

                key = registry.get_api_key(name)
            except Exception:  # noqa: BLE001
                key = None
        return Ollama(root=root, key=key, provider=name or _provider_for(root), version=version)
    return None


def _provider_for(root: str) -> Optional[str]:
    try:
        from bot import providers as registry

        for name, cfg in sorted(registry.list_providers().items()):
            if _root_of(str((cfg or {}).get("base_url") or "")) == root:
                return name
    except Exception:  # noqa: BLE001
        pass
    return None


def require() -> Ollama:
    found = find()
    if found is None:
        raise OllamaError(f"Ollama is not running (nothing answered at a configured provider's address or {DEFAULT_URL}). "
                          "Start Ollama, or add it on the Models page.")
    return found


def _headers(o: Ollama) -> dict[str, str]:
    return {"Authorization": f"Bearer {o.key}"} if o.key else {}


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:500]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        return str(err or data)[:500]
    return str(data)[:500]


def request(method: str, path: str, *, body: Any = None, params: Optional[dict] = None, timeout: float = 60.0,
            ollama: Optional[Ollama] = None, content: Optional[bytes] = None, raw: bool = False) -> Any:
    o = ollama or require()
    if not path.startswith("/"):
        path = "/" + path
    kwargs: dict[str, Any] = {"headers": _headers(o), "timeout": timeout,
                              "params": {k: v for k, v in (params or {}).items() if v is not None}}
    if content is not None:
        kwargs["content"] = content
    elif method.upper() not in READ_METHODS and body is not None:
        kwargs["json"] = body
    try:
        r = httpx.request(method.upper(), o.root + path, **kwargs)
    except httpx.TimeoutException as exc:
        raise OllamaError(f"Ollama did not answer {method} {path} within {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise OllamaError(f"could not reach Ollama at {o.root}: {exc}") from exc
    if raw:
        return r
    if r.status_code >= 400:
        raise OllamaError(f"Ollama {method} {path} failed ({r.status_code}): {_error_text(r)}", r.status_code)
    if not r.content:
        return {}
    if "json" in r.headers.get("content-type", ""):
        # A non-streamed call can still answer as NDJSON; keep the last object.
        try:
            return r.json()
        except ValueError:
            lines = [ln for ln in r.text.splitlines() if ln.strip()]
            return json.loads(lines[-1]) if lines else {}
    return r.text


def stream(path: str, body: dict, *, on_event: Optional[Callable[[dict], None]] = None, timeout: float = 6 * 3600,
           ollama: Optional[Ollama] = None) -> dict:
    """A streamed POST (pull, push, create): each progress line goes to `on_event`; returns the last one. Raises on an
    {"error": ...} line."""
    o = ollama or require()
    last: dict = {}
    try:
        with httpx.stream("POST", o.root + path, json={**body, "stream": True}, headers=_headers(o),
                          timeout=httpx.Timeout(timeout, connect=10.0, read=600.0)) as r:
            if r.status_code >= 400:
                r.read()
                raise OllamaError(f"Ollama {path} failed ({r.status_code}): {_error_text(r)}", r.status_code)
            for line in r.iter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("error"):
                    raise OllamaError(f"Ollama {path}: {event['error']}")
                last = event
                if on_event:
                    on_event(event)
    except httpx.TimeoutException as exc:
        raise OllamaError(f"Ollama {path} stopped sending progress") from exc
    except httpx.HTTPError as exc:
        raise OllamaError(f"could not reach Ollama at {o.root}: {exc}") from exc
    return last


# ---- every route Ollama serves ------------------------------------------------------------------------------------------------
def _f(type_: str, desc: str = "", *, required: bool = False, default: Any = None, enum: Optional[list] = None) -> dict:
    s: dict[str, Any] = {"type": type_, "description": desc}
    if default is not None:
        s["default"] = default
    if enum:
        s["enum"] = enum
    return {"schema": s, "required": required}


_MODEL = _f("string", "model name, e.g. qwen3.5:9b", required=True)
_OPTIONS = _f("object", "runtime options: num_ctx, temperature, top_p, top_k, seed, num_predict, stop, num_gpu, repeat_penalty...")
_KEEP = _f("string", "how long the model stays loaded afterwards, e.g. 5m, 1h, 0 to unload at once, -1 for ever")
_THINK = _f("string", "thinking: true/false, or low/medium/high for models that support levels")
_FORMAT = _f("object", "\"json\", or a JSON schema the reply must follow")

OPERATIONS: list[dict] = []


def _op(id_: str, method: str, path: str, group: str, summary: str, body: Optional[dict] = None,
        params: Optional[dict] = None, streams: bool = False) -> None:
    OPERATIONS.append({"id": id_, "method": method, "path": path, "group": group, "summary": summary,
                       "body": {"type": "object", "properties": {k: v["schema"] for k, v in (body or {}).items()},
                                "required": [k for k, v in (body or {}).items() if v["required"]]} if body is not None else None,
                       "params": [{"name": k, "in": "path" if "{" + k + "}" in path else "query", "required": v["required"],
                                   "schema": v["schema"]} for k, v in (params or {}).items()],
                       "streams": streams, "mutating": method not in READ_METHODS and id_ not in _READ_POSTS,
                       "multipart": False, "file_fields": []})


# POSTs that only read or compute (running a model is not a change to Ollama).
_READ_POSTS = {"show", "generate", "chat", "embed", "embeddings", "me", "openai_chat", "openai_completions", "openai_embeddings",
               "openai_responses", "openai_responses_compact", "anthropic_messages", "tokenize", "web_search", "web_fetch",
               "experimental_web_search", "experimental_web_fetch", "openai_transcriptions"}

_op("version", "GET", "/api/version", "server", "Ollama's version")
_op("status", "GET", "/api/status", "server", "Server status: whether cloud models are enabled")
_op("tags", "GET", "/api/tags", "models", "Installed models, with size, family, parameters and quantization")
_op("ps", "GET", "/api/ps", "models", "Loaded models: memory and VRAM used, context length, when each unloads")
_op("show", "POST", "/api/show", "models", "A model's details: capabilities, parameters, template, Modelfile, license",
    {"model": _MODEL, "verbose": _f("boolean", "include the full tensor and tokenizer information")})
_op("pull", "POST", "/api/pull", "models", "Download a model from the registry (or register a :cloud model)",
    {"model": _MODEL, "insecure": _f("boolean", "allow an insecure registry")}, streams=True)
_op("push", "POST", "/api/push", "models", "Upload a model to ollama.com (needs <namespace>/<model>:<tag> and your key)",
    {"model": _MODEL, "insecure": _f("boolean", "allow an insecure registry")}, streams=True)
_op("create", "POST", "/api/create", "models", "Create a model: from another model, from GGUF/safetensors blobs, with a system "
    "prompt, template, parameters, adapters, license, or quantized",
    {"model": _MODEL, "from": _f("string", "an existing model to build on"),
     "files": _f("object", "file name -> sha256 digest of an uploaded blob (GGUF or safetensors)"),
     "adapters": _f("object", "file name -> digest of a LoRA adapter blob"),
     "template": _f("string", "the prompt template"), "system": _f("string", "the system prompt"),
     "parameters": _f("object", "default runtime options, e.g. {\"num_ctx\": 32768}"),
     "messages": _f("array", "example conversation"), "license": _f("string", "license text"),
     "quantize": _f("string", "quantize a float16 model", enum=["q4_K_M", "q4_K_S", "q8_0"])}, streams=True)
_op("copy", "POST", "/api/copy", "models", "Copy a model under a new name", {"source": _MODEL, "destination": _f("string", "new name", required=True)})
_op("delete", "DELETE", "/api/delete", "models", "Delete a model", {"model": _MODEL})
_op("blob_exists", "HEAD", "/api/blobs/{digest}", "models", "Whether a blob (sha256:...) is already on the server",
    params={"digest": _f("string", "sha256:<hex>", required=True)})
_op("blob_upload", "POST", "/api/blobs/{digest}", "models", "Upload a file as a blob (use the Import GGUF action to upload a file)",
    params={"digest": _f("string", "sha256:<hex>", required=True)})
_op("generate", "POST", "/api/generate", "run", "Generate a completion (an empty prompt just loads the model)",
    {"model": _MODEL, "prompt": _f("string", "the prompt"), "suffix": _f("string", "text after the completion (fill-in-the-middle)"),
     "images": _f("array", "base64 images for a vision model"), "system": _f("string", "system prompt"),
     "template": _f("string", "override the template"), "format": _FORMAT, "think": _THINK, "raw": _f("boolean", "no templating"),
     "options": _OPTIONS, "keep_alive": _KEEP, "stream": _f("boolean", "stream the reply", default=False)})
_op("chat", "POST", "/api/chat", "run", "Chat, with tools, images, thinking and structured output",
    {"model": _MODEL, "messages": _f("array", "[{role, content, images?, tool_calls?}]", required=True),
     "tools": _f("array", "tool definitions"), "format": _FORMAT, "think": _THINK, "options": _OPTIONS, "keep_alive": _KEEP,
     "stream": _f("boolean", "stream the reply", default=False)})
_op("embed", "POST", "/api/embed", "run", "Embeddings for one or more inputs",
    {"model": _MODEL, "input": _f("array", "texts", required=True), "truncate": _f("boolean", "cut inputs to the context", default=True),
     "dimensions": _f("integer", "fewer dimensions, for models that allow it"), "options": _OPTIONS, "keep_alive": _KEEP})
_op("embeddings", "POST", "/api/embeddings", "run", "Embedding for one text (the older endpoint)",
    {"model": _MODEL, "prompt": _f("string", "text", required=True), "options": _OPTIONS, "keep_alive": _KEEP})
_op("me", "POST", "/api/me", "account", "The ollama.com account this Ollama is signed in to, and its plan")
_op("signout", "POST", "/api/signout", "account", "Sign this Ollama out of ollama.com")
_op("user_keys", "GET", "/api/user/keys/", "account", "The public keys registered with your ollama.com account")
_op("user_key_delete", "DELETE", "/api/user/keys/{encoded}", "account", "Remove a public key from your ollama.com account",
    params={"encoded": _f("string", "the encoded key", required=True)})
_op("web_search", "POST", "/api/web_search", "web", "Search the web through ollama.com (needs sign-in)",
    {"query": _f("string", "what to search for", required=True), "max_results": _f("integer", "1-10", default=5)})
_op("web_fetch", "POST", "/api/web_fetch", "web", "Fetch a web page's text through ollama.com (needs sign-in)",
    {"url": _f("string", "the page", required=True)})
_op("experimental_web_search", "POST", "/api/experimental/web_search", "web", "Web search (experimental endpoint)",
    {"query": _f("string", "what to search for", required=True), "max_results": _f("integer", "1-10", default=5)})
_op("experimental_web_fetch", "POST", "/api/experimental/web_fetch", "web", "Fetch a page (experimental endpoint)",
    {"url": _f("string", "the page", required=True)})
_op("model_recommendations", "GET", "/api/experimental/model-recommendations", "models", "Ollama's recommended models, local and cloud")
_op("openai_models", "GET", "/v1/models", "openai", "OpenAI-compatible: list models")
_op("openai_model", "GET", "/v1/models/{model}", "openai", "OpenAI-compatible: one model", params={"model": _MODEL})
_op("openai_chat", "POST", "/v1/chat/completions", "openai", "OpenAI-compatible chat completions",
    {"model": _MODEL, "messages": _f("array", "messages", required=True), "tools": _f("array", "tools"), "stream": _f("boolean", "", default=False),
     "temperature": _f("number", ""), "max_tokens": _f("integer", ""), "response_format": _f("object", ""), "reasoning_effort": _f("string", "")})
_op("openai_completions", "POST", "/v1/completions", "openai", "OpenAI-compatible completions",
    {"model": _MODEL, "prompt": _f("string", "", required=True), "max_tokens": _f("integer", "")})
_op("openai_embeddings", "POST", "/v1/embeddings", "openai", "OpenAI-compatible embeddings",
    {"model": _MODEL, "input": _f("array", "", required=True)})
_op("openai_responses", "POST", "/v1/responses", "openai", "OpenAI Responses API",
    {"model": _MODEL, "input": _f("array", "", required=True), "tools": _f("array", ""), "stream": _f("boolean", "", default=False)})
_op("openai_responses_compact", "POST", "/v1/responses/compact", "openai", "OpenAI Responses API: compact a conversation",
    {"model": _MODEL, "input": _f("array", "", required=True)})
_op("anthropic_messages", "POST", "/v1/messages", "anthropic", "Anthropic-compatible Messages API",
    {"model": _MODEL, "messages": _f("array", "", required=True), "max_tokens": _f("integer", "", required=True), "system": _f("string", ""),
     "tools": _f("array", ""), "stream": _f("boolean", "", default=False)})
_op("tokenize", "POST", "/v1/tokenize", "openai", "Tokenize text with a model's tokenizer",
    {"model": _MODEL, "text": _f("string", "", required=True)})
_op("openai_transcriptions", "POST", "/v1/audio/transcriptions", "openai", "Transcribe audio (OpenAI-compatible; upload a file)",
    {"model": _MODEL})
OPERATIONS[-1]["multipart"] = True
OPERATIONS[-1]["file_fields"] = [{"name": "file", "many": False}]


def operations() -> list[dict]:
    return sorted(OPERATIONS, key=lambda o: (o["group"], o["path"], o["method"]))


def find_operation(ref: str) -> dict:
    m = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD)\s+(/\S*)$", ref.strip(), re.I)
    for op in OPERATIONS:
        if op["id"] == ref or (m and op["method"] == m.group(1).upper() and op["path"] == m.group(2)):
            return op
    if m:   # a route this table does not know yet: still callable
        return {"id": ref, "method": m.group(1).upper(), "path": m.group(2), "group": "other", "summary": "", "body": {"type": "object"},
                "params": [], "streams": False, "mutating": m.group(1).upper() not in READ_METHODS, "multipart": False, "file_fields": []}
    raise OllamaError(f"Ollama has no operation {ref!r}")


def call(ref: str, args: Optional[dict] = None, *, timeout: float = 600.0, on_event: Optional[Callable[[dict], None]] = None,
         ollama: Optional[Ollama] = None) -> Any:
    """Call any Ollama operation. Path parameters by name; the JSON body is args["body"] or the remaining args. A
    streaming operation (pull, push, create) reports progress to `on_event` and returns its last line. A file upload
    takes args["files"] = {field: [(filename, bytes)]}."""
    o = ollama or require()
    op = find_operation(ref)
    args = dict(args or {})
    path = op["path"]
    for p in op["params"]:
        if p["in"] == "path":
            if p["name"] not in args:
                raise OllamaError(f"{op['id']} needs {p['name']!r}")
            path = path.replace("{" + p["name"] + "}", quote(str(args.pop(p["name"])), safe=":@"))
    params = {p["name"]: args.pop(p["name"]) for p in op["params"] if p["in"] == "query" and p["name"] in args}
    if op["multipart"]:
        files = args.pop("files", None)
        if not files:
            raise OllamaError(f"{op['id']} uploads a file: pass files as {{field: [(filename, bytes)]}}")
        flat = [(field, item) for field, items in files.items() for item in (items if isinstance(items, list) else [items])]
        try:
            r = httpx.post(o.root + path, files=flat, data={k: str(v) for k, v in (args.pop("body", None) or args).items()},
                           headers=_headers(o), timeout=timeout)
        except httpx.HTTPError as exc:
            raise OllamaError(f"could not reach Ollama at {o.root}: {exc}") from exc
        if r.status_code >= 400:
            raise OllamaError(f"Ollama {path} failed ({r.status_code}): {_error_text(r)}", r.status_code)
        return r.json() if "json" in r.headers.get("content-type", "") else r.text
    body = args.pop("body", None)
    if body is None and op["method"] not in READ_METHODS:
        body = args
    if op["streams"] and (body or {}).get("stream", True) is not False:
        return stream(path, body or {}, on_event=on_event, timeout=timeout, ollama=o)
    if op["method"] == "HEAD":
        r = request("HEAD", path, ollama=o, timeout=timeout, raw=True)
        return {"exists": r.status_code == 200, "status": r.status_code}
    if op["id"] in ("generate", "chat") and isinstance(body, dict):
        body.setdefault("stream", False)
    return request(op["method"], path, body=body, params=params, timeout=timeout, ollama=o)


def _reset_for_tests() -> None:
    with _lock:
        _found.update(at=0.0, value=None)
