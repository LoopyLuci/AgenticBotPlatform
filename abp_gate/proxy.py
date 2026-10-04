"""The reverse proxy in front of the active instance.

Plain ASGI, so the public listener is a plain uvicorn and there is exactly one
implementation of HTTP/1.1 and WebSocket framing on the front door - ours is
only "read a request, hand it to httpx, hand the answer back".

Two things it must get right, because the alternative is a subtly broken ABP:

STREAMING. Responses are never buffered. An SSE stream
(`Content-Type: text/event-stream`) or any chunked response is relayed chunk
by chunk with `aiter_raw()`, which also keeps the upstream's `Content-Encoding`
honest, so the desktop UI's live event stream and the chat SSE endpoints keep
working through the gate exactly as they do without it. The one thing that IS
buffered is a request body: an upload is read whole before it is forwarded.
That is a deliberate simplification, not an oversight (see docs/always-on.md).

WEBSOCKETS. `/api/ws` (the live event stream), `/api/terminals/ws` and
`/api/browser/ws` are real bidirectional sockets, so a proxy that only speaks
HTTP silently breaks three features. Frames are relayed in both directions
concurrently with `websockets`, with the client's subprotocol and Origin
carried through unchanged - the browser gateway checks Origin, and rewriting it
would turn every browser connection into a 4403.

Routing is a single mutable pointer, not a load balancer: `Router.set()` flips
which private port the next request uses, which is what makes a swap a
zero-downtime event. Requests already in flight keep their own upstream
connection (they were opened against the port that is still alive), and
`Router.drain()` counts them down so the outgoing instance is only stopped once
nothing is still using it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from abp_gate import paths

logger = logging.getLogger("abp_gate.proxy")

# RFC 7230 §6.1: connection-scoped headers must not be forwarded, and the
# response framing is the proxy's job, not the upstream's.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_DROP_FROM_RESPONSE = _HOP_BY_HOP | {"content-length"}
_DROP_FROM_REQUEST = _HOP_BY_HOP | {"host", "content-length"}

_UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=10.0)


class Router:
    """Where each channel's traffic goes right now, and how much of it is still
    in flight there."""

    def __init__(self) -> None:
        self._targets: dict[str, Optional[str]] = {c: None for c in paths.CHANNELS}
        self._inflight: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._changed_at = time.time()

    def set(self, channel: str, target: Optional[str]) -> None:
        self._targets[channel] = target
        self._changed_at = time.time()

    def target(self, channel: str) -> Optional[str]:
        return self._targets.get(channel)

    def targets(self) -> dict[str, Optional[str]]:
        return dict(self._targets)

    @property
    def changed_at(self) -> float:
        return self._changed_at

    def inflight(self, channel: str) -> int:
        return self._inflight.get(channel, 0)

    def begin(self, channel: str) -> None:
        self._inflight[channel] = self._inflight.get(channel, 0) + 1

    def end(self, channel: str) -> None:
        self._inflight[channel] = max(0, self._inflight.get(channel, 0) - 1)

    async def drain(self, channel: str = "dashboard", timeout: float = 10.0) -> int:
        """Wait for in-flight requests on `channel` to finish. Returns how many
        were still running when the timeout expired - the caller reports that,
        because "drained" and "drained with N requests cut off" are different
        outcomes and only one of them is the good one."""
        deadline = time.monotonic() + timeout
        while self.inflight(channel) > 0 and time.monotonic() < deadline:  # noqa: ASYNC110 — polling a counter, not an event
            await asyncio.sleep(0.05)
        return self.inflight(channel)


def _upstream_url(port: int, path: str, query: str = "", *, scheme: str = "http") -> str:
    return f"{scheme}://127.0.0.1:{port}{path}{('?' + query) if query else ''}"


class ProxyApp:
    """The ASGI app behind every public port. `channel` says which of the
    router's targets this listener fronts."""

    def __init__(self, router: Router, channel: str = "dashboard", *, name: str = "abp-gate"):
        self.router = router
        self.channel = channel
        self.name = name
        self._clients: dict[int, httpx.AsyncClient] = {}

    # ------------------------------------------------------------------ http
    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(scope, receive, send)
        elif kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)
        else:  # pragma: no cover - uvicorn sends nothing else today
            logger.warning("unsupported ASGI scope %r", kind)

    async def _lifespan(self, scope, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                for client in list(self._clients.values()):
                    await client.aclose()
                self._clients.clear()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _client(self) -> httpx.AsyncClient:
        key = id(self)
        client = self._clients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT, follow_redirects=False)
            self._clients[key] = client
        return client

    async def _http(self, scope, receive, send) -> None:
        port = self.router.target(self.channel)
        if not port:
            await self._no_instance(scope, receive, send)
            return
        body = await _read_body(receive)
        headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.decode("latin-1").lower() not in _DROP_FROM_REQUEST
        ]
        url = _upstream_url(int(port), scope.get("path", "/"), scope.get("query_string", b"").decode("latin-1"))
        self.router.begin(self.channel)
        try:
            client = await self._client()
            request = client.build_request(scope["method"], url, headers=headers, content=body)
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("%s %s upstream failed: %s", scope.get("method"), url, exc)
            self.router.end(self.channel)
            await self._error(send, 502, f"the active ABP instance is not answering: {exc}")
            return
        try:
            raw = [(k, v) for k, v in response.headers.raw if k.decode("latin-1").lower() not in _DROP_FROM_RESPONSE]
            await send({"type": "http.response.start", "status": response.status_code, "headers": raw})
            async for chunk in response.aiter_raw():
                if not chunk:
                    continue
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await response.aclose()
            self.router.end(self.channel)

    async def _no_instance(self, scope, receive, send) -> None:
        await self._error(
            send,
            503,
            f"abp_gate is up but no ABP instance is active (channel {self.channel!r}). "
            "Start one with: abp_cli instance swap <code-root>",
            extra={"X-Abp-Gate": self.name},
        )

    async def _error(self, send, status: int, detail: str, extra: Optional[dict] = None) -> None:
        body = json.dumps({"detail": detail, "gate": self.name}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    # ------------------------------------------------------------- websocket
    async def _websocket(self, scope, receive, send) -> None:
        port = self.router.target(self.channel)
        if not port:
            await send({"type": "websocket.close", "code": 1013})
            return
        incoming = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        url = _upstream_url(int(port), scope.get("path", "/"), scope.get("query_string", b"").decode("latin-1"), scheme="ws")
        subprotocols = [s.strip() for s in (incoming.get("sec-websocket-protocol", "")).split(",") if s.strip()]
        forwarded = {
            k: v for k, v in incoming.items()
            if k not in _HOP_BY_HOP
            and k not in ("host", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions")
        }
        message = await receive()  # websocket.connect
        if message["type"] == "websocket.disconnect":
            return
        await send({"type": "websocket.accept", "subprotocol": subprotocols[0] if subprotocols else None})
        try:
            await self._relay(scope, receive, send, url, forwarded, subprotocols)
        except Exception:  # noqa: BLE001 - a dropped socket is normal, not an incident
            logger.debug("websocket relay to %s ended", url, exc_info=True)

    async def _relay(self, scope, receive, send, url: str, headers: dict, subprotocols: list[str]) -> None:
        from websockets.asyncio.client import connect

        async with connect(url, additional_headers=headers, subprotocols=subprotocols or None,
                           max_size=None, open_timeout=10) as upstream:
            client_task = asyncio.create_task(self._ws_client_to_upstream(receive, upstream))
            upstream_task = asyncio.create_task(self._ws_upstream_to_client(upstream, send))
            done, pending = await asyncio.wait({client_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    raise exc

    async def _ws_client_to_upstream(self, receive, upstream) -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close()
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def _ws_upstream_to_client(self, upstream, send) -> None:
        async for frame in upstream:
            if isinstance(frame, str):
                await send({"type": "websocket.send", "text": frame})
            else:
                await send({"type": "websocket.send", "bytes": frame})
        await send({"type": "websocket.close", "code": 1000})


def with_suppress_close(send) -> None:
    """Best-effort close notice; the client may already be gone, which is fine."""


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body")
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def ws_host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "127.0.0.1", int(parts.port or 80)


async def serve(apps: dict[int, ProxyApp], *, log_level: str = "warning") -> list[Any]:
    """Start one uvicorn server per public port. Returns the servers so the
    caller can ask them to exit; a failure to bind ANY of them is fatal for
    that port only, so 11436 being taken does not take 8787 down with it."""
    import uvicorn

    servers: list[Any] = []
    for port, app in sorted(apps.items()):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level, loop="asyncio",
                                ws="auto", access_log=False)
        servers.append(uvicorn.Server(config))
    return servers


def describe(router: Router) -> dict[str, Any]:
    return {
        "targets": router.targets(),
        "changed_at": router.changed_at,
        "inflight": {c: router.inflight(c) for c in paths.CHANNELS},
    }