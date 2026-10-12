"""Test doubles for the work board: a clock tests can move, and a fake AgentSentinel board server.

The fake implements the board protocol's HTTP surface by delegating to a LocalBoard (so the semantics are the ones under
test in the conformance suite) behind the real wire shapes: 201 + task at top level and under `task`, 200
`{task, deduped}`, 204 + X-Board-Hint, errors as JSON `{error: CODE, message, holders?}`, Bearer auth.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from bot.coordination.local import LocalBoard
from bot.coordination.model import BoardError

TOKEN = "fake-token"
PREFIX = "/api/v1/board"


class FakeClock:
    def __init__(self, start_ms: int = 1_000_000):
        self.ms = start_ms

    def __call__(self) -> int:
        return self.ms

    def advance(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)


class FakeSentinel:
    """Serve `board` (a LocalBoard) as AgentSentinel's /api/v1/board on an ephemeral port."""

    def __init__(self, board: LocalBoard, token: str = TOKEN):
        self.board = board
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 - silence
                pass

            def _send(self, status, body=None, headers=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if status != 204:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if status != 204:
                    self.wfile.write(data)

            def _handle(self, method):
                url = urlparse(self.path)
                outer.requests.append((method, url.path))
                if self.headers.get("Authorization") != f"Bearer {token}":
                    return self._send(401, {"error": "UNAUTHORIZED", "message": "bad token"})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                path = url.path[len(PREFIX):] if url.path.startswith(PREFIX) else None
                if path is None:
                    return self._send(404, {"error": "NOT_FOUND", "message": "no such route"})
                try:
                    status, out, headers = outer.route(method, path, body, q)
                except BoardError as exc:
                    payload = {"error": exc.code, "message": exc.message}
                    if exc.holders:
                        payload["holders"] = exc.holders
                    return self._send(exc.status, payload)
                self._send(status, out, headers)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def route(self, method, path, b, q):
        bd = self.board
        parts = [p for p in path.split("/") if p]
        if parts == ["tasks"] and method == "POST":
            task, deduped = bd.create_task(
                b["board"], b.get("title", ""), key=b.get("key"), detail=b.get("detail", ""), priority=b.get("priority", 0),
                needs=b.get("needs"), deps=b.get("deps"), scope=b.get("scope"), max_attempts=b.get("max_attempts", 3),
                dedupe_done=bool(b.get("dedupe_done")), created_by=b.get("agent") or "")
            if deduped:
                return 200, {"task": task, "deduped": True}, None
            return 201, {**task, "task": task}, None
        if parts == ["tasks"] and method == "GET":
            return 200, {"tasks": bd.list_tasks(q.get("board"), state=q.get("state"), owner=q.get("owner"),
                                                limit=int(q.get("limit") or 200))}, None
        if parts == ["tasks", "next"]:
            got = bd.next(b["board"], b["agent"], capabilities=b.get("capabilities"), lease_s=b.get("lease_s"))
            if got is None:
                return 204, None, {"X-Board-Hint": json.dumps(bd.last_hint)}
            return 200, got, None
        if len(parts) == 2 and parts[0] == "tasks" and method == "GET":
            return 200, bd.get_task(parts[1]), None
        if len(parts) == 3 and parts[0] == "tasks":
            tid, op = parts[1], parts[2]
            if op == "claim":
                return 200, bd.claim(tid, b["agent"], lease_s=b.get("lease_s"), expect_version=b.get("expect_version")), None
            if op == "heartbeat":
                return 200, bd.heartbeat(tid, b["agent"], b["fence"], lease_s=b.get("lease_s"), progress=b.get("progress")), None
            if op == "state":
                return 200, bd.set_state(tid, b["agent"], b["fence"], b["state"], result=b.get("result"), note=b.get("note", "")), None
            if op == "cancel":
                return 200, bd.cancel(tid, agent=b.get("agent") or "", note=b.get("note") or ""), None
        if parts == ["locks", "acquire"]:
            return 200, bd.acquire(b["agent"], b["resources"], task=b.get("task"), lease_s=b.get("lease_s")), None
        if parts == ["locks", "heartbeat"]:
            return 200, bd.lock_heartbeat(b["agent"], b["fence"], lease_s=b.get("lease_s")), None
        if parts == ["locks", "release"]:
            return 200, bd.release_locks(b["agent"], b["fence"], b.get("resources")), None
        if parts == ["locks"] and method == "GET":
            return 200, {"locks": bd.list_locks(resource_prefix=q.get("resource_prefix", ""), owner=q.get("owner", ""))}, None
        if parts == ["presence"] and method == "POST":
            return 200, bd.presence(b["agent"], b["status"], task=b.get("task"), capabilities=b.get("capabilities")), None
        if parts == ["presence"] and method == "GET":
            return 200, {"agents": bd.list_presence()}, None
        if parts == ["summary"]:
            return 200, bd.summary(q.get("board")), None
        raise BoardError("NOT_FOUND", f"no route {method} {path}", status=404)
