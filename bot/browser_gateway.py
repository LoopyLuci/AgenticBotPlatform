"""The model gateway: an OpenAI-compatible surface (`/api/browser/v1/*`) in front of every kind of model ABP can reach
(docs/browser-extension/DESIGN.md sections 7 and 9).

Routing is by the model id's first segment:

  web/<adapter>[:<variant>]   a chat product the person is logged into (Grok, Gemini, ...), driven by the extension's adapter engine
  browser-local/<model>       a model running inside the browser (extension phase 3; answers "unavailable" until an engine reports one)
  <provider>/<model>          any provider configured in config/providers.yaml (cloud or a local server), forwarded as-is
  auto                        the model router's pick among the configured candidates (never a web model unless the person opted in)

Web models are chat UIs, not APIs, so three things are emulated here and flagged honestly in /models: tool calling (a compact
`<abp_tool>` protocol in the prompt, parsed from the reply, one repair turn on malformed output), streaming (text deltas seen in the
page), and token usage (an estimate).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator, Optional

from bot import browser_bridge as bb

logger = logging.getLogger("bot.browser_gateway")

WEB_PREFIX = "web"
LOCAL_PREFIX = "browser-local"
RESERVED_PROVIDERS = (WEB_PREFIX, LOCAL_PREFIX)
GATEWAY_PATH = "/api/browser/v1"

DEFAULT_WEB_DEADLINE_MS = 180_000
MAX_PROMPT_CHARS = 60_000


class GatewayError(Exception):
    """Carries an OpenAI-style error: {"error": {"message", "type", "code"}}."""

    def __init__(self, status: int, message: str, *, code: str = "gateway_error", kind: str = "invalid_request_error", hint: str = "") -> None:
        super().__init__(message)
        self.status, self.message, self.code, self.kind, self.hint = status, message, code, kind, hint

    def body(self) -> dict:
        err: dict[str, Any] = {"message": self.message, "type": self.kind, "code": self.code}
        if self.hint:
            err["hint"] = self.hint
        return {"error": err}


_BRIDGE_STATUS = {"E_NOT_CONNECTED": 503, "E_NOT_LOGGED_IN": 409, "E_RATE_LIMITED": 429, "E_TIMEOUT": 504, "E_ADAPTER_BROKEN": 502,
                  "E_NOT_ALLOWED": 403, "E_MODEL_UNAVAILABLE": 503, "E_METHOD": 501, "E_BUSY": 429, "E_CANCELLED": 499, "E_PARAMS": 400,
                  "E_TOO_LARGE": 413, "E_SENSITIVE_SITE": 403}


def from_bridge(exc: bb.BridgeError) -> GatewayError:
    return GatewayError(_BRIDGE_STATUS.get(exc.code, 502), exc.message or exc.code, code=exc.code.lower(),
                        kind="rate_limit_error" if exc.code == "E_RATE_LIMITED" else "api_error", hint=exc.hint)


# ------------------------------------------------------------------------------------------------ prompt building (web models)
def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") in ("text", "input_text"):
                parts.append(str(p.get("text", "")))
            elif isinstance(p, dict) and p.get("type") in ("image_url", "input_image"):
                parts.append("[an image was attached; this model cannot receive it through the chat page]")
        return "\n".join(x for x in parts if x)
    return str(content)


def _compact_schema(schema: Any) -> str:
    try:
        return json.dumps(schema, separators=(",", ":"))[:1200]
    except (TypeError, ValueError):
        return "{}"


def tool_protocol(tools: list[dict]) -> str:
    lines = [
        "You can call tools. To call a tool, reply with ONLY one or more blocks in exactly this form and no other text:",
        '<abp_tool name="TOOL_NAME">{"argument": "value"}</abp_tool>',
        "The body is one JSON object of arguments. Call several tools by writing several blocks. When no tool is needed, answer normally.",
        "Tools you may call:",
    ]
    for t in tools:
        fn = t.get("function") or t
        lines.append(f"- {fn.get('name')}: {str(fn.get('description') or '')[:400]} | parameters: {_compact_schema(fn.get('parameters') or {})}")
    first = (tools[0].get("function") or tools[0]).get("name", "tool") if tools else "tool"
    lines.append(f'Example: <abp_tool name="{first}">{{}}</abp_tool>')
    return "\n".join(lines)


def _render_tool_call(name: str, args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    return f'<abp_tool name="{name}">{json.dumps(args, separators=(",", ":"))}</abp_tool>'


def flatten(messages: list[dict], tools: Optional[list[dict]] = None, *, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """One prompt for a chat page. Oldest middle turns are dropped (with a note) when the transcript would exceed `max_chars`; the
    system messages, the tool protocol and the newest turns are always kept."""
    head: list[str] = []
    if tools:
        head.append(tool_protocol(tools))
    turns: list[str] = []
    for m in messages:
        role = str(m.get("role") or "user")
        text = _content_text(m.get("content"))
        if role in ("system", "developer"):
            head.append(text)
        elif role == "assistant":
            calls = [_render_tool_call((c.get("function") or {}).get("name", ""), (c.get("function") or {}).get("arguments", "{}"))
                     for c in (m.get("tool_calls") or [])]
            turns.append("Assistant: " + "\n".join([t for t in [text] + calls if t]))
        elif role == "tool":
            turns.append(f"Tool result ({m.get('name') or m.get('tool_call_id') or 'tool'}): {text}")
        else:
            turns.append(f"User: {text}")
    prefix = ("Instructions:\n" + "\n\n".join(h for h in head if h) + "\n\n") if head else ""
    body_budget = max(200, max_chars - len(prefix) - 40)
    kept: list[str] = []
    used = 0
    for t in reversed(turns):
        if used + len(t) > body_budget and kept:
            kept.append("[earlier conversation omitted to fit the page's input limit]")
            break
        kept.append(t[-body_budget:] if len(t) > body_budget else t)
        used += len(t)
    kept.reverse()
    return prefix + "Conversation so far:\n" + "\n\n".join(kept) + "\n\nAssistant:"


# ------------------------------------------------------------------------------------------------ tool-call parsing
_TOOL_RE = re.compile(r'<abp_tool\s+name\s*=\s*"([^"]+)"\s*>(.*?)</abp_tool>', re.DOTALL)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_tool_calls(text: str, tools: list[dict]) -> tuple[str, list[dict], Optional[str]]:
    """(text outside the blocks, well-formed calls, a problem description or None). A block naming an unknown tool or carrying
    invalid JSON is a problem, never silently dropped."""
    known = {(t.get("function") or t).get("name") for t in tools}
    calls: list[dict] = []
    problem: Optional[str] = None
    for name, body in _TOOL_RE.findall(text):
        raw = _FENCE.sub("", body.strip()).strip() or "{}"
        if name not in known:
            problem = f"there is no tool called {name!r}"
            continue
        try:
            args = json.loads(raw)
        except ValueError as exc:
            problem = f"the arguments of {name} were not valid JSON ({exc})"
            continue
        if not isinstance(args, dict):
            problem = f"the arguments of {name} must be one JSON object"
            continue
        calls.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
    if not calls and problem is None and "<abp_tool" in text:
        problem = "a tool block was opened but never closed"
    outside = _TOOL_RE.sub("", text).strip()
    return outside, calls, problem


REPAIR = ("Your last reply tried to call a tool but it could not be used: {why}. Reply again with ONLY valid "
          '<abp_tool name="TOOL_NAME">{{json arguments}}</abp_tool> blocks, or a plain answer if no tool is needed.')


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ------------------------------------------------------------------------------------------------ OpenAI shapes
def completion_body(model: str, text: str, tool_calls: list[dict], prompt: str, *, cid: Optional[str] = None) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    pt, ct = estimate_tokens(prompt), estimate_tokens(text + json.dumps(tool_calls))
    return {"id": cid or "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct, "estimated": True}}


def chunk(cid: str, model: str, delta: dict, finish: Optional[str] = None) -> str:
    return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


def split_model(model: str) -> tuple[str, str]:
    prefix, _, rest = (model or "").partition("/")
    return prefix, rest


# ------------------------------------------------------------------------------------------------ the gateway
class Gateway:
    def __init__(self, bridge: Optional[bb.Bridge] = None) -> None:
        self.bridge = bridge or bb.bridge
        self._streams: dict[str, "asyncio.Queue[dict]"] = {}
        self.bridge.listen(self._on_event)

    # extension -> ABP progress events (called on the bridge's loop)
    def _on_event(self, name: str, data: dict) -> None:
        if name not in ("event.web.delta", "event.llm.delta"):
            return
        q = self._streams.get(str(data.get("req") or ""))
        if q is not None:
            q.put_nowait(data)

    # ---- catalogue
    async def web_adapters(self) -> list[dict]:
        if not self.bridge.connected():
            return []
        try:
            r = await self.bridge.call("web.adapters.list", {}, deadline_ms=8000)
        except bb.BridgeError:
            return []
        return list(r.get("adapters") or []) if isinstance(r, dict) else []

    async def models(self) -> dict:
        data: list[dict] = []
        now = int(time.time())
        for a in await self.web_adapters():
            if not a.get("enabled"):
                continue
            base = f"web/{a['id']}"
            variants = [base] + [f"{base}:{v}" for v in (a.get("models") or [])]
            for mid in variants:
                data.append({"id": mid, "object": "model", "created": now, "owned_by": "browser-session",
                             "abp": {"kind": "web", "tools": "emulated", "context": "unknown", "streaming": "page-diff", "usage": "estimated",
                                     "vision": bool(a.get("vision")), "degraded": bool(a.get("degraded")), "logged_in": a.get("logged_in"),
                                     "trust": "third-party", "note": a.get("tos_note", "")}})
        for m in (self.bridge_models() or []):
            data.append({"id": f"{LOCAL_PREFIX}/{m['id']}", "object": "model", "created": now, "owned_by": "browser-local",
                         "abp": {"kind": "browser-local", "trust": "local", **{k: v for k, v in m.items() if k != "id"}}})
        return {"object": "list", "data": data}

    def bridge_models(self) -> list[dict]:
        for c in self.bridge.connections.values():
            ms = (c.caps.get("models") or {}).get("installed") if isinstance(c.caps.get("models"), dict) else None
            if ms:
                return list(ms)
        return []

    async def local_catalog(self, task: Optional[str] = None) -> list[dict]:
        """Every model in the extension's curated in-browser catalog (src/models/catalog.ts), with a fit score for the
        connected browser's actual hardware and whether it has been loaded (cached) there before. Empty, not an error,
        when no browser is connected - there is nothing to score against."""
        if not self.bridge.connected():
            return []
        try:
            r = await self.bridge.call("llm.catalog", {"task": task} if task else {}, deadline_ms=8000)
        except bb.BridgeError:
            return []
        return list(r.get("models") or []) if isinstance(r, dict) else []

    # ---- chat completions
    async def chat(self, body: dict, *, sensitive: bool = False) -> tuple[str, Any]:
        """("json", dict) for a normal reply, ("stream", async-iterator-of-SSE-strings) when body["stream"] is set."""
        model = str(body.get("model") or "")
        messages = body.get("messages")
        if not model or not isinstance(messages, list) or not messages:
            raise GatewayError(400, "model and a non-empty messages list are required", code="invalid_request")
        prefix, rest = split_model(model)
        if model == "auto":
            model = await self._auto(messages)
            prefix, rest = split_model(model)
            body = {**body, "model": model}
        if prefix == WEB_PREFIX:
            if sensitive:
                raise GatewayError(403, "this request is marked sensitive, and sensitive content is never sent to a chat website", code="egress_blocked",
                                   hint="use a local or api model for this content")
            return await self._web(body, rest)
        if prefix == LOCAL_PREFIX:
            return await self._browser_local(body, rest)
        return await self._provider(body, prefix, rest)

    async def _auto(self, messages: list[dict]) -> str:
        from bot import model_router
        try:
            from bot.config import config
            allow_web = bool(((config.current.get("native_agent") or {}).get("router") or {}).get("allow_web_in_auto"))
        except Exception:  # noqa: BLE001
            allow_web = False
        task = next((_content_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
        try:
            _cls, ranked, _skipped = await asyncio.to_thread(model_router.recommend, task)
        except Exception as exc:  # noqa: BLE001
            raise GatewayError(503, f"the model router could not pick a model ({exc})", code="router_failed") from exc
        for r in ranked:
            prefix = split_model(r.model)[0]
            # A web model is never picked by "auto" unless the person explicitly opted in
            # (native_agent.router.allow_web_in_auto) - it spends someone else's chat session, not a metered API call.
            if prefix != WEB_PREFIX or allow_web:
                return r.model
        raise GatewayError(503, "no configured model fits this request", code="no_model", hint="add a provider in Models, or set native_agent.router.candidates")

    # ---- web/<adapter>
    async def _web(self, body: dict, rest: str) -> tuple[str, Any]:
        adapter, _, variant = rest.partition(":")
        if not adapter:
            raise GatewayError(400, "web models look like web/grok", code="invalid_model")
        tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
        if str(body.get("tool_choice") or "") == "none":
            tools = []
        prompt = flatten(body["messages"], tools)
        req = uuid.uuid4().hex
        model = str(body["model"])
        params = {"adapter": adapter, "prompt": prompt, "model": variant or None, "new_chat": True, "timeout_ms": DEFAULT_WEB_DEADLINE_MS,
                  "stream": bool(body.get("stream")) and not tools}

        if not body.get("stream") or tools:
            text, calls = await self._web_once(req, adapter, params, tools)
            payload = completion_body(model, text, calls, prompt)
            if body.get("stream"):                                     # tools + stream: one buffered burst, still valid SSE
                return "stream", _replay(payload)
            return "json", payload
        return "stream", self._web_stream(req, model, params)

    async def _web_once(self, req: str, adapter: str, params: dict, tools: list[dict]) -> tuple[str, list[dict]]:
        text = await self._web_call(req, params)
        if not tools:
            return text, []
        outside, calls, problem = parse_tool_calls(text, tools)
        if problem and not calls:
            repaired = await self._web_call(uuid.uuid4().hex, {**params, "prompt": REPAIR.format(why=problem), "new_chat": False})
            outside, calls, problem = parse_tool_calls(repaired, tools)
            if problem and not calls:                                    # give up gracefully: the model's words as a plain answer
                return repaired, []
        return outside, calls

    async def _web_call(self, req: str, params: dict) -> str:
        try:
            r = await self.bridge.call("web.prompt", params, deadline_ms=int(params.get("timeout_ms") or DEFAULT_WEB_DEADLINE_MS),
                                       idem=req, session=f"gw-{req[:8]}")
        except bb.BridgeError as exc:
            raise from_bridge(exc) from exc
        return str((r or {}).get("text") or "")

    async def _web_stream(self, req: str, model: str, params: dict) -> AsyncIterator[str]:
        task = asyncio.ensure_future(self._web_call(req, params))
        async for piece in self._stream_text(req, model, task):
            yield piece

    async def _stream_text(self, req: str, model: str, task: "asyncio.Task[str]") -> AsyncIterator[str]:
        """SSE chunks for a call whose only progress signal is `event.web.delta`/`event.llm.delta` notifications keyed by
        `req` (the idempotency key), landing in `self._streams[req]` via `_on_event`, plus a final plain-text result from
        `task`. Shared by web/<adapter> and browser-local/<model>: the two engines differ, the streaming shape does not."""
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        q: "asyncio.Queue[dict]" = asyncio.Queue()
        self._streams[req] = q
        sent = ""
        try:
            yield chunk(cid, model, {"role": "assistant", "content": ""})
            while True:
                getter = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    ev = getter.result()
                    text = str(ev.get("replace") if ev.get("replace") is not None else sent + str(ev.get("text") or ""))
                    if text.startswith(sent) and len(text) > len(sent):
                        yield chunk(cid, model, {"content": text[len(sent):]})
                        sent = text
                    continue
                getter.cancel()
                break
            while not q.empty():                                         # events that raced the final result
                ev = q.get_nowait()
                text = str(ev.get("replace") if ev.get("replace") is not None else sent + str(ev.get("text") or ""))
                if text.startswith(sent) and len(text) > len(sent):
                    yield chunk(cid, model, {"content": text[len(sent):]})
                    sent = text
            try:
                final = task.result()
            except GatewayError as exc:
                yield "data: " + json.dumps(exc.body()) + "\n\n"
                yield "data: [DONE]\n\n"
                return
            if final.startswith(sent) and len(final) > len(sent):
                yield chunk(cid, model, {"content": final[len(sent):]})
            elif not sent and final:
                yield chunk(cid, model, {"content": final})
            yield chunk(cid, model, {}, "stop")
            yield "data: [DONE]\n\n"
        finally:
            self._streams.pop(req, None)
            if not task.done():                                          # the client went away: stop the page too
                task.cancel()

    # ---- embeddings (browser-local only - a web chat page has no such API)
    async def embeddings(self, body: dict) -> dict:
        model = str(body.get("model") or "")
        prefix, rest = split_model(model)
        if prefix != LOCAL_PREFIX or not rest:
            raise GatewayError(400, "embeddings are served by a browser-local/<embedding-model>", code="invalid_model",
                               hint="see /api/browser/v1/browser-local/models for what is loaded")
        raw = body.get("input")
        texts = [raw] if isinstance(raw, str) else [str(x) for x in (raw or []) if isinstance(x, str)]
        if not texts:
            raise GatewayError(400, "input is required", code="invalid_request")
        try:
            r = await self.bridge.call("llm.embed", {"model": rest, "texts": texts}, deadline_ms=DEFAULT_WEB_DEADLINE_MS)
        except bb.BridgeError as exc:
            raise from_bridge(exc) from exc
        vectors = list((r or {}).get("embeddings") or [])
        tokens = sum(estimate_tokens(t) for t in texts)
        return {"object": "list", "model": model, "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]}

    # ---- browser-local/<model>
    async def _browser_local(self, body: dict, rest: str) -> tuple[str, Any]:
        if not rest:
            raise GatewayError(400, "browser-local models look like browser-local/qwen2.5-1.5b (see /api/browser/v1/browser-local/models)", code="invalid_model")
        model = f"{LOCAL_PREFIX}/{rest}"
        req = uuid.uuid4().hex
        params = {"model": rest, "messages": body["messages"], "max_tokens": body.get("max_tokens"), "temperature": body.get("temperature"), "stream": bool(body.get("stream"))}
        if not body.get("stream"):
            text = await self._llm_call(req, params)
            return "json", completion_body(model, text, [], json.dumps(body["messages"]))
        task = asyncio.ensure_future(self._llm_call(req, params))
        return "stream", self._stream_text(req, model, task)

    async def _llm_call(self, req: str, params: dict) -> str:
        try:
            r = await self.bridge.call("llm.generate", params, deadline_ms=DEFAULT_WEB_DEADLINE_MS, idem=req, session=f"gw-{req[:8]}")
        except bb.BridgeError as exc:
            if exc.code == "E_METHOD":
                raise GatewayError(503, "this browser has no in-browser model engine yet", code="model_unavailable",
                                   hint="update the ABP Bridge extension - in-browser models need phase 3 or later") from exc
            raise from_bridge(exc) from exc
        return str((r or {}).get("text") or "")

    # ---- <provider>/<model>
    async def _provider(self, body: dict, provider: str, model_id: str) -> tuple[str, Any]:
        import httpx
        from bot import providers
        if not provider or not model_id:
            raise GatewayError(400, "model must look like <provider>/<model> (or web/<site>, or auto)", code="invalid_model")
        entry = providers.get_provider(provider)
        if entry is None:
            raise GatewayError(404, f"no provider called {provider!r} is configured", code="model_not_found",
                               hint="providers are listed in Models; web/... and browser-local/... are handled by the browser")
        base = str(entry.get("base_url") or "").rstrip("/")
        if GATEWAY_PATH in base:
            raise GatewayError(400, f"provider {provider!r} points back at this gateway", code="provider_loop")
        if entry.get("protocol", "openai") != "openai":
            raise GatewayError(400, f"provider {provider!r} does not speak chat completions through this gateway", code="unsupported_protocol")
        headers = {"Content-Type": "application/json"}
        key = providers.get_api_key(provider)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {**body, "model": model_id}
        url = f"{base}/chat/completions"
        if not body.get("stream"):
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                try:
                    r = await client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise GatewayError(502, f"{provider} did not answer ({exc.__class__.__name__})", code="upstream_unreachable") from exc
            try:
                data = r.json()
            except ValueError:
                raise GatewayError(502, f"{provider} returned something that is not JSON", code="bad_upstream") from None
            if r.status_code >= 400:
                raise GatewayError(r.status_code, str((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data)[:500],
                                   code="upstream_error", kind="api_error")
            return "json", data
        return "stream", _proxy_stream(url, payload, headers, provider)


async def _proxy_stream(url: str, payload: dict, headers: dict, provider: str) -> AsyncIterator[str]:
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    raw = (await r.aread()).decode("utf-8", "replace")[:500]
                    yield "data: " + json.dumps({"error": {"message": raw, "type": "api_error", "code": "upstream_error"}}) + "\n\n"
                    yield "data: [DONE]\n\n"
                    return
                async for line in r.aiter_lines():
                    yield line + "\n"
        except httpx.HTTPError as exc:
            yield "data: " + json.dumps({"error": {"message": f"{provider} did not answer ({exc.__class__.__name__})", "type": "api_error"}}) + "\n\n"
            yield "data: [DONE]\n\n"


async def _replay(payload: dict) -> AsyncIterator[str]:
    """A finished completion as an SSE stream (used where the reply cannot be incremental)."""
    cid, model = payload["id"], payload["model"]
    choice = payload["choices"][0]
    msg = choice["message"]
    yield chunk(cid, model, {"role": "assistant", "content": ""})
    if msg.get("content"):
        yield chunk(cid, model, {"content": msg["content"]})
    if msg.get("tool_calls"):
        yield chunk(cid, model, {"tool_calls": [{"index": i, **c} for i, c in enumerate(msg["tool_calls"])]})
    yield chunk(cid, model, {}, choice["finish_reason"])
    yield "data: [DONE]\n\n"


gateway = Gateway()


# ------------------------------------------------------------------------------------------------ registering the providers
def register_providers(port: int) -> dict:
    """Add `web` and `browser-local` to config/providers.yaml (only what is missing), pointing at this gateway, authenticated with the
    dashboard token through the environment (never written into the file). After this the native agent loop, sub-agents, swarms and
    routines can use `web/grok` and friends like any other provider."""
    from bot import providers
    added: list[str] = []
    for name in RESERVED_PROVIDERS:
        if providers.get_provider(name) is None:
            providers.set_provider(name, f"http://127.0.0.1:{port}{GATEWAY_PATH}/{name}", api_key_env="DASHBOARD_TOKEN", actor="browser_gateway")
            added.append(name)
    return {"added": added, "providers": [n for n in RESERVED_PROVIDERS if providers.get_provider(n) is not None]}
