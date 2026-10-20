"""OpenAI-compatible chat-completions transport — extracted from what
bot/backends/custom_model_backend.py used to do inline. Covers Ollama,
LM Studio, vLLM, llama.cpp's server, OpenRouter, and real OpenAI, all of
which speak this same wire format.

Fixes a real latent bug found while extracting this: the old
custom_model_backend.py stored a FULL message dict (including its own
redundant "role" key) as the `content` value passed to
bot.db.append_agent_message() for assistant/tool turns — since
list_agent_messages() re-wraps whatever was stored as
`{"role": <db column>, "content": <stored value>}`, reloading a
multi-turn custom_model conversation on a later /ask call would double-
wrap those entries (e.g. `{"role":"assistant","content":{"role":"assistant",
"content":...,"tool_calls":...}}`) before resending them, which is not
valid chat-completions shape. This was never caught because every
existing test only ever calls .ask() once per session. Fixed here by
storing just the meaningful payload (no redundant "role") and having
`_to_wire_messages()` reconstruct the real wire shape on the way out —
tolerant of old rows that still have the stray inner "role" key, since
it's simply never read.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

import httpx

from bot.agent_runtime import text_toolcalls
from bot.agent_runtime.transports.base import NormalizedResponse, ProviderTransport, StreamEvent, ToolCall, report_speed
from bot.backends.base import BackendError

logger = logging.getLogger("bot.agent_runtime.transports.openai_compatible")

API_MODE = "chat_completions"


def to_openai_tools(anthropic_tool_schemas: list[dict]) -> list[dict]:
    """Anthropic-shaped {name, description, input_schema} -> OpenAI's
    function-calling {"type": "function", "function": {...}} shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": schema.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for schema in anthropic_tool_schemas
    ]


def _to_wire_messages(history: list[dict]) -> list[dict]:
    wire: list[dict] = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content")
        if role == "assistant":
            payload = content if isinstance(content, dict) else {"content": content}
            msg: dict = {"role": "assistant", "content": payload.get("content")}
            if payload.get("tool_calls"):
                msg["tool_calls"] = payload["tool_calls"]
            wire.append(msg)
        elif role == "tool":
            payload = content if isinstance(content, dict) else {}
            wire.append({"role": "tool", "tool_call_id": payload.get("tool_call_id"), "content": payload.get("content")})
        elif isinstance(content, list):
            # A multimodal user turn (see user_message(images=...)) —
            # already wire-ready chat-completions content blocks, passed
            # through verbatim.
            wire.append({"role": role, "content": content})
        else:
            wire.append({"role": role, "content": content if isinstance(content, str) else (content or {}).get("content", "")})
    return wire


class OpenAICompatibleTransport(ProviderTransport):
    def __init__(self, base_url: str, api_key: Optional[str] = None, catalog_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Used only to look up a per-vendor quirk profile (see
        # bot/agent_runtime/provider_quirks.py) — None is fine, it just
        # means no profile matches and every request behaves exactly as
        # it did before quirk handling existed.
        self.catalog_id = catalog_id
        # Generated once per transport instance (which itself lives for
        # one bot instance's/subagent's whole conversation — see
        # CustomModelBackend/NativeAgentBackend) rather than per request,
        # matching OpenCode's own "stable per-conversation id" contract
        # for its x-opencode-session header. Harmless to generate even
        # for providers that never use it — see provider_quirks.py.
        self._session_id = uuid.uuid4().hex
        # Set when this server answered a request carrying `response_format` with a client error: grammar mode then
        # falls back to the prompt-only text mode for the rest of this transport's life.
        self._no_response_format = False

    supports_vision = True
    # No documents support — `document` blocks are an Anthropic-specific
    # shape with no standardized OpenAI-compatible equivalent (see the
    # Claude API/Claude Code parity plan's Phase C). `documents` is
    # accepted-and-ignored here purely so every transport's user_message()
    # keeps the same call signature — NativeAgentBackend.ask() never
    # actually passes it when supports_documents is False.
    supports_documents = False

    def user_message(
        self, text: str, *,
        images: Optional[list[dict[str, str]]] = None, documents: Optional[list[dict[str, str]]] = None,
    ) -> dict:
        if not images:
            return {"role": "user", "content": text}
        blocks: list[dict] = [{"type": "text", "text": text}]
        for img in images:
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{img['data_b64']}"}})
        return {"role": "user", "content": blocks}

    def prune_tool_results(self, history: list[dict], keep: int) -> tuple[list[dict], int]:
        from bot.agent_runtime import context_window

        holders = [i for i, e in enumerate(history) if e.get("role") == "tool"]
        old = set(holders[:-keep] if keep > 0 else holders)
        out, cleared = [], 0
        for i, entry in enumerate(history):
            payload = entry.get("content")
            if i in old and isinstance(payload, dict) and isinstance(payload.get("content"), str) \
                    and not payload["content"].startswith("[Output cleared"):
                entry = {**entry, "content": {**payload, "content": context_window.placeholder(len(payload["content"]), payload["content"])}}
                cleared += 1
            out.append(entry)
        return out, cleared

    def dangling_tool_calls(self, history: list[dict]) -> list[ToolCall]:
        if not history or history[-1].get("role") != "assistant":
            return []
        payload = history[-1].get("content")
        calls = payload.get("tool_calls") if isinstance(payload, dict) else None
        return [ToolCall(id=c.get("id"), name=(c.get("function") or {}).get("name", ""), arguments={})
                for c in (calls or []) if c.get("id")]

    def tool_result_messages(self, results: list[tuple[ToolCall, str]]) -> list[dict]:
        return [{"role": "tool", "content": {"tool_call_id": tc.id, "content": output}} for tc, output in results]

    def _build_request(
        self, *, model: str, history: list[dict], tool_schemas: list[dict], max_tokens: int,
        system_prompt: Optional[str], effort: Optional[str], mode: str = "native",
    ) -> tuple[dict, dict]:
        """(payload, headers) — shared by send() and send_stream(). `mode` is the model's tool_call_mode (see
        text_toolcalls.mode_for): `text` and `grammar` describe the tools in the system prompt instead of sending the
        API's `tools` field, and `grammar` also constrains the reply with a JSON schema (`response_format`) unless
        this server already refused one."""
        wire_messages = _to_wire_messages(history)
        text_mode = mode in ("text", "grammar") and bool(tool_schemas)
        if text_mode:
            section = text_toolcalls.tools_prompt(tool_schemas, grammar=(mode == "grammar"))
            system_prompt = f"{system_prompt}\n\n{section}" if system_prompt else section
        if system_prompt:
            wire_messages = [{"role": "system", "content": system_prompt}] + wire_messages

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": model, "messages": wire_messages, "max_tokens": max_tokens}
        if tool_schemas and not text_mode:
            payload["tools"] = to_openai_tools(tool_schemas)
            payload["tool_choice"] = "auto"
        if text_mode and mode == "grammar" and not self._no_response_format:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "tool_call", "strict": True, "schema": text_toolcalls.grammar_schema(tool_schemas)}}
        # Best-effort only: "reasoning_effort" is the real parameter name
        # OpenAI's o-series Chat Completions API accepts, and several
        # OpenRouter-routed models pass it straight through — but this is
        # genuinely provider/model-dependent, not a guarantee. An endpoint
        # that doesn't recognize the field is expected to just ignore it
        # (safe), not error — never asserted or verified for every
        # possible custom_model provider.
        from bot import effort as effort_module

        openai_effort = effort_module.to_openai_reasoning_effort(effort)
        if openai_effort is not None:
            payload["reasoning_effort"] = openai_effort

        # Real per-vendor wire quirks (confirmed against Hermes Agent's
        # own per-vendor adapters) — a provider with no matching profile
        # is completely untouched by this call.
        from bot.agent_runtime import provider_quirks

        quirk_profile = provider_quirks.profile_for(self.catalog_id, self.base_url)
        provider_quirks.apply(payload, profile=quirk_profile, effort=effort)
        headers.update(provider_quirks.extra_headers(profile=quirk_profile, session_id=self._session_id))
        return payload, headers

    async def send(
        self,
        *,
        model: str,
        history: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
        timeout_s: float,
        system_prompt: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> NormalizedResponse:
        mode = text_toolcalls.mode_for(model, self._router_name(), self)
        payload, headers = self._build_request(
            model=model, history=history, tool_schemas=tool_schemas, max_tokens=max_tokens,
            system_prompt=system_prompt, effort=effort, mode=mode,
        )
        response = _normalize(await self._post(payload, headers, timeout_s), self.base_url)
        report_speed(model, response.tokens_per_s)
        return await self._recover_text_calls(response, payload=payload, headers=headers, timeout_s=timeout_s,
                                              model=model, tool_schemas=tool_schemas, mode=mode)

    def _router_name(self) -> str:
        return str(getattr(self, "router_provider", None) or self.provider_key or "")

    async def _post(self, payload: dict, headers: dict, timeout_s: float) -> dict:
        """One non-streamed chat-completions call; the parsed JSON body. A server that refuses `response_format`
        is asked again once without it."""
        try:
            return await self._post_once(payload, headers, timeout_s)
        except _Refused as refused:
            if "response_format" not in payload:
                raise refused.error from None
            self._no_response_format = True
            logger.info("openai-compatible transport (%s) does not accept response_format; using prompt-only tool calls",
                        self.base_url)
            payload = {k: v for k, v in payload.items() if k != "response_format"}
            return await self._post_once(payload, headers, timeout_s, final=True)

    async def _post_once(self, payload: dict, headers: dict, timeout_s: float, final: bool = False) -> dict:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                self.capture(resp)
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) timed out after {timeout_s}s") from exc
            except httpx.HTTPStatusError as exc:
                error = BackendError(
                    f"openai-compatible transport ({self.base_url}) returned "
                    f"{exc.response.status_code}: {exc.response.text[:500]}"
                )
                if not final and "response_format" in payload and exc.response.status_code in (400, 404, 422, 501):
                    raise _Refused(error) from exc
                raise error from exc
            except Exception as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) error: {exc}") from exc

    async def _recover_text_calls(
        self, response: NormalizedResponse, *, payload: dict, headers: dict, timeout_s: float, model: str,
        tool_schemas: list[dict], mode: str,
    ) -> NormalizedResponse:
        """Native tool calls pass through untouched (and are counted). A reply with none is checked for calls
        written as text (text_toolcalls.parse); an attempt that cannot be used is put back to the model ONCE with a
        short corrective message, and then given up on: the reply is returned as it is, never looped on."""
        ref = f"{self._router_name()}/{model}"
        if response.tool_calls:
            text_toolcalls.record(ref, "native", len(response.tool_calls))
            return response
        if not tool_schemas or not response.text:
            return response
        grammar = mode == "grammar"
        parsed = text_toolcalls.parse(response.text, tool_schemas, allow_final=grammar)
        if not parsed.attempted:
            return response
        total = response.tokens
        reasked = False
        if parsed.errors:
            reasked = True
            retry = {k: v for k, v in payload.items() if k not in ("stream", "stream_options")}
            if self._no_response_format:
                retry.pop("response_format", None)
            retry["messages"] = list(payload["messages"]) + [
                {"role": "assistant", "content": response.text},
                {"role": "user", "content": text_toolcalls.corrective_message(parsed.errors, tool_schemas, grammar=grammar)},
            ]
            try:
                second = _normalize(await self._post(retry, headers, timeout_s), self.base_url)
            except BackendError:
                logger.info("tool-call re-ask failed on %s; giving up on the call", ref)
                text_toolcalls.record(ref, "failed")
                return response
            if second.tokens or response.tokens:
                total = (response.tokens or 0) + (second.tokens or 0)
            if second.tool_calls:
                text_toolcalls.record(ref, "repaired", len(second.tool_calls))
                second.tokens = total or None
                return second
            parsed = text_toolcalls.parse(second.text, tool_schemas, allow_final=grammar)
            if not parsed.attempted or parsed.errors:
                text_toolcalls.record(ref, "failed")
                second.tokens = total or None
                return second
            response = second
        if not parsed.calls:                          # grammar mode: the model answered in words
            if parsed.final_answer is None:
                return response
            return NormalizedResponse(
                text=parsed.text, tokens=total or None, input_tokens=response.input_tokens,
                assistant_message={"role": "assistant", "content": {"content": parsed.text}})
        raw_calls = [{"id": text_toolcalls.call_id(), "type": "function",
                      "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in parsed.calls]
        text_toolcalls.record(ref, "repaired" if (parsed.repaired or reasked) else "text", len(raw_calls))
        return NormalizedResponse(
            text=parsed.text,
            tool_calls=[ToolCall(id=rc["id"], name=rc["function"]["name"], arguments=c.arguments)
                        for rc, c in zip(raw_calls, parsed.calls)],
            tokens=total or None, input_tokens=response.input_tokens,
            assistant_message={"role": "assistant", "content": {"content": parsed.text or None, "tool_calls": raw_calls}},
        )

    supports_streaming = True

    async def send_stream(
        self,
        *,
        on_event,
        model: str,
        history: list[dict],
        tool_schemas: list[dict],
        max_tokens: int,
        timeout_s: float,
        system_prompt: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> NormalizedResponse:
        """Server-sent-events variant of send(): reply text goes to `on_event` as it
        arrives and the deltas are assembled into the same NormalizedResponse."""
        mode = text_toolcalls.mode_for(model, self._router_name(), self)
        if mode == "grammar" and tool_schemas:
            # A schema-constrained reply is JSON, not prose to show as it arrives: take it whole.
            response = await self.send(model=model, history=history, tool_schemas=tool_schemas, max_tokens=max_tokens,
                                       timeout_s=timeout_s, system_prompt=system_prompt, effort=effort)
            if response.text:
                await on_event(StreamEvent("text", response.text))
            return response
        payload, headers = self._build_request(
            model=model, history=history, tool_schemas=tool_schemas, max_tokens=max_tokens,
            system_prompt=system_prompt, effort=effort, mode=mode,
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                try:
                    streamed = await self._consume_stream(client, url, payload, headers, on_event)
                except _RetryWithoutUsage:
                    # A few OpenAI-compatible servers reject stream_options; usage is optional.
                    payload.pop("stream_options", None)
                    streamed = await self._consume_stream(client, url, payload, headers, on_event)
            except httpx.TimeoutException as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) timed out after {timeout_s}s") from exc
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"openai-compatible transport ({self.base_url}) error: {exc}") from exc
        report_speed(model, streamed.tokens_per_s)
        # Calls written as text were streamed as prose: when they turn into real calls, replace what was shown.
        recovered = await self._recover_text_calls(streamed, payload=payload, headers=headers, timeout_s=timeout_s,
                                                   model=model, tool_schemas=tool_schemas, mode=mode)
        if recovered is not streamed:
            await on_event(StreamEvent("reset"))
            if recovered.text:
                await on_event(StreamEvent("text", recovered.text))
        return recovered

    async def _consume_stream(self, client, url, payload, headers, on_event) -> NormalizedResponse:
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        usage: dict = {}
        timings: dict = {}
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            self.capture(resp)
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                if resp.status_code in (400, 422) and "stream_options" in body and "stream_options" in payload:
                    raise _RetryWithoutUsage()
                raise BackendError(
                    f"openai-compatible transport ({self.base_url}) returned {resp.status_code}: {body[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if event.get("error") and not (event.get("choices") or []):
                    raise _provider_error(event, self.base_url)   # an error can arrive inside a 200 stream too
                if event.get("usage"):
                    usage = event["usage"]
                if isinstance(event.get("timings"), dict):      # llama.cpp's own timing, on the last chunk
                    timings = event["timings"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                        await on_event(StreamEvent("text", delta["content"]))
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        message: dict = {"content": "".join(text_parts) or None}
        if calls:
            message["tool_calls"] = [
                {"id": c["id"] or f"call_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for i, c in sorted(calls.items())
            ]
        return _normalize({"usage": usage, "timings": timings, "choices": [{"message": message}]}, self.base_url)


class _RetryWithoutUsage(Exception):
    pass


class _Refused(Exception):
    """The server answered a request carrying `response_format` with a client error."""

    def __init__(self, error: BackendError):
        super().__init__(str(error))
        self.error = error


def _provider_error(data: dict, base_url: str) -> BackendError:
    """A 200 with no choices is not an empty answer. OpenRouter, among others, reports an upstream
    failure - provider overloaded, rate-limited, no provider left for the model - as HTTP 200 with an
    `error` object and no choices at all, so "returned no choices" hid both what the provider said
    and which status it was: a free model that was merely busy read as a model that cannot do the
    work. Say what came back, with its own code."""
    error = data.get("error")
    if not error:
        return BackendError(f"openai-compatible transport ({base_url}) returned no choices")
    if not isinstance(error, dict):
        return BackendError(f"openai-compatible transport ({base_url}) returned an error: {str(error)[:400]}")
    code = error.get("code")
    status = f" {code}" if isinstance(code, int) and code >= 400 else ""
    return BackendError(f"openai-compatible transport ({base_url}) returned{status}: "
                        f"{str(error.get('message') or error)[:400]}")


def _normalize(data: dict, base_url: str) -> NormalizedResponse:
    usage = data.get("usage") or {}
    tokens = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)

    choices = data.get("choices") or []
    if not choices:
        raise _provider_error(data, base_url)
    message = choices[0].get("message") or {}
    tool_calls_raw = message.get("tool_calls") or []

    tool_calls = []
    for tc in tool_calls_raw:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(ToolCall(id=tc.get("id"), name=fn.get("name", ""), arguments=args))

    assistant_payload: dict = {"content": message.get("content")}
    if tool_calls_raw:
        assistant_payload["tool_calls"] = tool_calls_raw

    timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
    try:
        tps = float(timings.get("predicted_per_second") or 0) or None
    except (TypeError, ValueError):
        tps = None
    return NormalizedResponse(
        text=message.get("content") or "",
        tool_calls=tool_calls,
        tokens=tokens or None,
        input_tokens=usage.get("prompt_tokens") or None,
        assistant_message={"role": "assistant", "content": assistant_payload},
        tokens_per_s=tps,
    )
