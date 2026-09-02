"""Hermes Agent gateway backend — talks to `hermes serve`'s JSON-RPC-over-
WebSocket API instead of shelling out per-prompt (see hermes_cli_backend.py
for the simpler one-shot alternative).

Protocol facts below were confirmed live against a real running gateway
this session (not guessed from docs):

  - `hermes serve --host 127.0.0.1 --port <port> --isolated --skip-build`
    prints a line containing "HERMES_BACKEND_READY" to stdout once it's
    listening. `--isolated` gives this invocation its own web/API server
    process and port — but the underlying agent *gateway* (session store,
    MCP connections, cwd) is still the one shared machine-wide instance
    Hermes calls "single" gateway mode; there is currently no flag that
    fully sandboxes agent state per Bot Server instance. This is worth
    knowing (a prompt sent here can see the same sessions/MCP servers as
    the user's own interactive `hermes` usage) even though it doesn't
    block a working integration.
  - Auth for the WS route specifically is a **query parameter**
    (`?token=<HERMES_DASHBOARD_SESSION_TOKEN>`), *not* the
    `X-Hermes-Session-Token` header — that header authenticates Hermes's
    plain HTTP endpoints only. Set `HERMES_DASHBOARD_SESSION_TOKEN` in the
    spawned process's env so this backend knows the token deterministically
    instead of scraping it from a log.
  - On connect the server pushes an unsolicited
    `{"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready",...}}`
    frame before anything else.
  - `session.create` (id-correlated request/response) returns
    `{"result": {"session_id": "..."}}`.
  - `prompt.background` with `{"session_id","text"}` returns immediately
    `{"result": {"task_id": "bg_xxxxx"}}` — the actual answer arrives later
    as an event frame `{"method":"event","params":{"type":"background.complete",
    "session_id":..., "payload": {"task_id":..., "text": "..."}}}`. Other
    unrelated event frames (e.g. "session.info", "sessions.changed") can
    arrive interleaved before it — this backend dispatches by matching
    `id` for RPC responses and `payload.task_id` for background completion,
    not by assuming strict frame ordering.

Session identity is threaded through from Router.ask() via
`context["desktop_session_key"]` (this instance's persisted, real Hermes
`session_id` — see bot_instances.desktop_session_key / Router.create_session)
so a given bot instance's messages keep landing in the *same* Hermes
session/conversation instead of a throwaway one-shot session per call. If
`context` carries no key (first-ever call for that instance, or the caller
passed `force_new_session`), a fresh `session.create` is issued and its
`session_id` is returned via `BackendResult.raw["desktop_session_key"]` for
the router to persist. A call with no bot instance at all (instance_id is
None — e.g. an ad-hoc /ask with no linked instance) still gets a fresh
throwaway session each time, matching the old stateless behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
from typing import Any, Optional

from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.hermes_gateway")

_READY_MARKER = b"HERMES_BACKEND_READY"


class HermesGatewayBackend(Backend):
    name = "hermes_gateway"

    def __init__(self, binary: str = "hermes", port: int = 8799, spawn_timeout_s: float = 30, model: Optional[str] = None):
        self.binary = binary
        self.port = port
        self.spawn_timeout_s = spawn_timeout_s
        self.model = model
        self._token = secrets.token_urlsafe(24)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._ws: Optional[Any] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending: dict[int, asyncio.Future] = {}
        self._bg_pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self._connect_lock = asyncio.Lock()

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 60) -> BackendResult:
        context = context or {}
        instance_id = context.get("instance_id")
        session_key = context.get("desktop_session_key") if not context.get("force_new_session") else None
        try:
            text, session_id, created = await self._ask_once(prompt, timeout_s, session_key, instance_id)
        except (ConnectionError, asyncio.IncompleteReadError, BackendError) as first_exc:
            logger.warning("hermes_gateway connection issue, retrying once: %s", first_exc)
            await self._teardown_connection()
            try:
                text, session_id, created = await self._ask_once(prompt, timeout_s, session_key, instance_id)
            except Exception as exc:
                raise BackendError(f"hermes_gateway failed after retry: {exc}") from exc
        raw = {"desktop_session_key": session_id} if created else None
        return BackendResult(text=text, tokens=None, raw=raw)

    async def fetch_model_options(self, refresh: bool = False, timeout_s: float = 20) -> dict:
        """GET this gateway's own `/api/model/options` — the same
        pricing-aware provider/model inventory Hermes's own dashboard/TUI
        picker uses (`hermes_cli.inventory.build_model_options_payload()`),
        including a real `free: bool` per model, not a naming-convention
        guess. Auth here is the `X-Hermes-Session-Token` header (Hermes's
        plain-HTTP auth) — distinct from the WS route's `?token=` query
        param `_connect()` uses; see this module's docstring. Spawns the
        gateway process first (via _ensure_connected(), same lazy-spawn
        path ask() uses) if it isn't already running — discovery should
        work even before the first ask()."""
        import httpx

        await self._ensure_connected()
        url = f"http://127.0.0.1:{self.port}/api/model/options"
        params = {"refresh": "true"} if refresh else {}
        headers = {"X-Hermes-Session-Token": self._token}
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            raise BackendError(f"hermes gateway /api/model/options failed: {exc}") from exc

    async def create_session(self, timeout_s: float = 15) -> str:
        """Explicitly opens a brand-new Hermes session and returns its
        session_id as the key the caller (Router.create_session) should
        persist against the bot instance."""
        await self._ensure_connected()
        session_params = {"model": self.model} if self.model else {}
        session = await self._call("session.create", session_params, timeout_s=timeout_s)
        session_id = session.get("session_id")
        if not session_id:
            raise BackendError("hermes session.create returned no session_id")
        return session_id

    async def _ask_once(
        self, prompt: str, timeout_s: float, session_key: Optional[str], instance_id: Optional[int]
    ) -> tuple[str, Optional[str], bool]:
        await self._ensure_connected()
        created = False
        session_id = session_key
        if not session_id:
            if instance_id is None:
                # No linked bot instance at all — keep the old stateless
                # behavior (a fresh throwaway session every call) rather than
                # persisting anything nowhere.
                session_params = {"model": self.model} if self.model else {}
                session = await self._call("session.create", session_params, timeout_s=15)
                session_id = session.get("session_id")
                if not session_id:
                    raise BackendError("hermes session.create returned no session_id")
            else:
                session_id = await self.create_session(timeout_s=15)
                created = True

        bg = await self._call("prompt.background", {"session_id": session_id, "text": prompt}, timeout_s=15)
        task_id = bg.get("task_id")
        if not task_id:
            raise BackendError("hermes prompt.background returned no task_id")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._bg_pending[task_id] = fut
        try:
            text = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            self._bg_pending.pop(task_id, None)
            raise BackendError(f"hermes_gateway timed out after {timeout_s}s waiting for a response") from exc
        return text, session_id, created

    # ------------------------------------------------------- connection ---

    async def _ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._ws is not None:
                return
            await self._spawn_if_needed()
            await self._connect()

    async def _spawn_if_needed(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        env = os.environ.copy()
        env["HERMES_DASHBOARD_SESSION_TOKEN"] = self._token
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.binary, "serve", "--host", "127.0.0.1", "--port", str(self.port),
                "--isolated", "--skip-build",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise BackendError(f"'{self.binary}' not found on PATH — is Hermes Agent installed?") from exc

        try:
            async with asyncio.timeout(self.spawn_timeout_s):
                while True:
                    line = await self._proc.stdout.readline()
                    if not line:
                        raise BackendError("hermes serve exited before becoming ready")
                    if _READY_MARKER in line:
                        break
        except TimeoutError as exc:
            raise BackendError(f"hermes serve did not become ready within {self.spawn_timeout_s}s") from exc

    async def _connect(self) -> None:
        import websockets

        uri = f"ws://127.0.0.1:{self.port}/api/ws?token={self._token}"
        try:
            self._ws = await websockets.connect(uri, open_timeout=15)
        except Exception as exc:
            raise BackendError(f"failed to connect to hermes gateway: {exc}") from exc

        try:
            first = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=15))
        except Exception as exc:
            raise BackendError(f"hermes gateway didn't send a ready event: {exc}") from exc
        if not (first.get("method") == "event" and (first.get("params") or {}).get("type") == "gateway.ready"):
            logger.warning("hermes gateway's first frame wasn't gateway.ready: %r", first)

        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                frame_id = frame.get("id")
                if frame_id is not None and frame_id in self._pending:
                    fut = self._pending.pop(frame_id)
                    if not fut.done():
                        fut.set_result(frame)
                    continue
                if frame.get("method") == "event":
                    params = frame.get("params") or {}
                    if params.get("type") == "background.complete":
                        payload = params.get("payload") or {}
                        task_id = payload.get("task_id")
                        fut = self._bg_pending.pop(task_id, None) if task_id else None
                        if fut and not fut.done():
                            fut.set_result(payload.get("text", ""))
        except Exception as exc:
            logger.info("hermes gateway read loop ended: %s", exc)
        finally:
            self._fail_all_pending(BackendError("hermes gateway connection closed"))
            self._ws = None

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for fut in list(self._bg_pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._bg_pending.clear()

    async def _call(self, method: str, params: dict, timeout_s: float) -> dict:
        self._next_id += 1
        req_id = self._next_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        try:
            frame = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise BackendError(f"hermes {method} timed out after {timeout_s}s") from exc
        if "error" in frame:
            raise BackendError(f"hermes {method} error: {frame['error']}")
        return frame.get("result") or {}

    async def _teardown_connection(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def shutdown(self) -> None:
        """Terminates the dedicated hermes serve subprocess this backend
        spawned — called from bot/main.py's shutdown path. Never touches
        any other Hermes process."""
        await self._teardown_connection()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None

    def __repr__(self) -> str:
        return f"HermesGatewayBackend(port={self.port})"
