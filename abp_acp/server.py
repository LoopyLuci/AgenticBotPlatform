"""An Agent Client Protocol (ACP) server: lets an editor (Zed and other ACP clients) use the ABP agent.

ACP is JSON-RPC 2.0, one message per line, over the agent process's standard input and output. This
server implements the parts an editor needs to hold a conversation:

    client -> agent   initialize, authenticate, session/new, session/prompt, session/cancel (notification)
    agent -> client   session/update (notification: message chunks, tool activity),
                      session/request_permission (request: approve a tool call)

Written from the public protocol description (agentclientprotocol.com) and tested against this repository's
own client stand-in only. It has **not been run against Zed** or another real editor; treat field names as
"believed right" until it has. Not implemented: loading old sessions, terminals, the client's file-system
methods (the agent works on the folder directly), images and audio in prompts, session modes.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("abp_acp")

PROTOCOL_VERSION = 1
E_PARSE, E_REQUEST, E_METHOD, E_PARAMS, E_INTERNAL = -32700, -32600, -32601, -32602, -32603


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Session:
    id: str
    cwd: Path
    history_key: str = ""
    task: Optional[asyncio.Task] = None
    cancelled: bool = False
    tool_ids: list = field(default_factory=list)


def prompt_text(blocks: list) -> str:
    """The text of an ACP prompt: text blocks as they are, embedded files and links as labelled context."""
    parts = []
    for b in blocks or []:
        kind = (b or {}).get("type")
        if kind == "text":
            parts.append(str(b.get("text") or ""))
        elif kind == "resource":
            res = b.get("resource") or {}
            body = res.get("text")
            label = res.get("uri") or "attachment"
            parts.append(f"[{label}]\n{body}" if body else f"[attached: {label}]")
        elif kind == "resource_link":
            parts.append(f"[see {b.get('uri') or b.get('name') or 'link'}]")
        elif kind in ("image", "audio"):
            parts.append(f"[a {kind} was attached; this agent cannot read it over ACP yet]")
    return "\n\n".join(p for p in parts if p).strip()


class AcpServer:
    """`read_line()` returns the next line or None at end of input; `write_line(text)` sends one line.
    `runner(prompt, session, on_text, progress, approval_notify)` runs one agent turn and returns a
    `abp_run.core.RunResult`."""

    def __init__(self, read_line: Callable[[], Awaitable[Optional[str]]], write_line: Callable[[str], Awaitable[None]],
                 runner: Callable[..., Awaitable[Any]], *, name: str = "abp", version: str = "0"):
        self.read_line, self.write_line, self.runner = read_line, write_line, runner
        self.name, self.version = name, version
        self.sessions: dict[str, Session] = {}
        self._ids = itertools.count(1)
        self._waiting: dict[Any, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._write_lock = asyncio.Lock()
        self.initialized = False

    # ---- wire ----------------------------------------------------------------------------
    async def _send(self, message: dict) -> None:
        async with self._write_lock:
            await self.write_line(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False))

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"method": method, "params": params})

    async def call(self, method: str, params: dict, timeout_s: float = 600.0) -> Any:
        """A request to the client (for example asking permission); waits for its answer."""
        rid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout=timeout_s)
        finally:
            self._waiting.pop(rid, None)

    async def serve(self) -> None:
        while True:
            line = await self.read_line()
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("not an object")
            except ValueError:
                await self._send({"id": None, "error": {"code": E_PARSE, "message": "parse error"}})
                continue
            await self._dispatch(message)
        for session in list(self.sessions.values()):
            if session.task and not session.task.done():
                session.cancelled = True
                session.task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _dispatch(self, message: dict) -> None:
        if "method" not in message:                                   # an answer to something we asked
            fut = self._waiting.get(message.get("id"))
            if fut is not None and not fut.done():
                if "error" in message:
                    fut.set_exception(RpcError(int((message["error"] or {}).get("code", E_INTERNAL)), str((message["error"] or {}).get("message", ""))))
                else:
                    fut.set_result(message.get("result"))
            return
        method, params, rid = message["method"], message.get("params") or {}, message.get("id")
        if method == "session/cancel":                                # a notification, handled at once
            self._cancel(str(params.get("sessionId") or ""))
            return
        task = asyncio.create_task(self._handle(method, params, rid))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, method: str, params: dict, rid: Any) -> None:
        try:
            handler = {"initialize": self._initialize, "authenticate": self._authenticate, "session/new": self._new_session,
                       "session/prompt": self._prompt}.get(method)
            if handler is None:
                raise RpcError(E_METHOD, f"method not found: {method}")
            result = await handler(params)
            if rid is not None:
                await self._send({"id": rid, "result": result})
        except RpcError as exc:
            if rid is not None:
                await self._send({"id": rid, "error": {"code": exc.code, "message": exc.message}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ACP request %s failed", method)
            if rid is not None:
                await self._send({"id": rid, "error": {"code": E_INTERNAL, "message": f"{type(exc).__name__}: {exc}"}})

    # ---- methods -----------------------------------------------------------------------------
    async def _initialize(self, params: dict) -> dict:
        self.initialized = True
        return {"protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": False,
                                      "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True}},
                "agentInfo": {"name": self.name, "title": "ABP agent", "version": self.version},
                "authMethods": []}

    async def _authenticate(self, params: dict) -> dict:
        return {}

    async def _new_session(self, params: dict) -> dict:
        cwd = Path(str(params.get("cwd") or ""))
        if not cwd.is_absolute() or not cwd.is_dir():
            raise RpcError(E_PARAMS, "cwd must be the absolute path of an existing folder")
        sid = "sess_" + uuid.uuid4().hex[:16]
        self.sessions[sid] = Session(id=sid, cwd=cwd)
        return {"sessionId": sid}

    def _session(self, params: dict) -> Session:
        session = self.sessions.get(str(params.get("sessionId") or ""))
        if session is None:
            raise RpcError(E_PARAMS, "unknown sessionId")
        return session

    def _cancel(self, sid: str) -> None:
        session = self.sessions.get(sid)
        if session and session.task and not session.task.done():
            session.cancelled = True
            session.task.cancel()

    async def _prompt(self, params: dict) -> dict:
        session = self._session(params)
        if session.task and not session.task.done():
            raise RpcError(E_REQUEST, "this session is already working on a prompt")
        text = prompt_text(params.get("prompt") or [])
        if not text:
            raise RpcError(E_PARAMS, "the prompt is empty")
        session.cancelled = False
        session.tool_ids.clear()

        sent = []

        async def on_text(chunk: str) -> None:
            sent.append(chunk)
            await self.notify("session/update", {"sessionId": session.id, "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": chunk}}})

        async def progress(line: str) -> None:
            tid = f"call_{len(session.tool_ids) + 1}"
            session.tool_ids.append(tid)
            await self.notify("session/update", {"sessionId": session.id, "update": {
                "sessionUpdate": "tool_call", "toolCallId": tid, "title": str(line)[:200], "kind": "other", "status": "in_progress"}})

        async def approval_notify(approval_id: int, tool: str, tool_input: dict) -> None:
            """Ask the editor's user; the answer resolves the agent's waiting approval."""
            from bot.agent_runtime import approval

            async def ask() -> None:
                try:
                    answer = await self.call("session/request_permission", {
                        "sessionId": session.id,
                        "toolCall": {"toolCallId": f"approval_{approval_id}", "title": _describe(tool, tool_input), "kind": "other",
                                     "status": "pending", "rawInput": tool_input},
                        "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                                    {"optionId": "allow_always", "name": "Allow for this session", "kind": "allow_always"},
                                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}]})
                    outcome = (answer or {}).get("outcome") or {}
                    option = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
                except (RpcError, asyncio.TimeoutError):
                    option = None
                choice = {"allow_once": "once", "allow_always": "session"}.get(option or "", "deny")
                approval.resolve(approval_id, choice, actor="acp")

            t = asyncio.create_task(ask())
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

        session.task = asyncio.current_task()
        try:
            result = await self.runner(text, session, on_text, progress, approval_notify)
        except asyncio.CancelledError:
            if session.cancelled:
                return {"stopReason": "cancelled"}
            raise
        finally:
            for tid in session.tool_ids:
                await self.notify("session/update", {"sessionId": session.id, "update": {
                    "sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "completed"}})
        if not result.ok:
            raise RpcError(E_INTERNAL, result.error or "the run failed")
        if getattr(result, "stopped", ""):
            return {"stopReason": "max_turn_requests"}
        if not sent and result.reply:                 # a transport that does not stream delivers the reply whole
            await on_text(result.reply)
        return {"stopReason": "end_turn"}


def _describe(tool: str, tool_input: dict) -> str:
    detail = tool_input.get("command") or tool_input.get("path") or tool_input.get("url") or ""
    return f"{tool}: {str(detail)[:160]}" if detail else tool
