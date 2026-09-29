"""A tiny module hub for the module framework's tests: it follows the contract in docs/modules/ROADMAP.md §2.

    python hub.py <data_dir>

Serves 127.0.0.1 on a free port with a random token, writes <data_dir>/control.json, and answers:
    GET  /v1/health           {ok, pid, version}             (no token needed)
    GET  /v1/operations       {"operations": [...]}
    POST /v1/call/<op>        {"result": ...}
    POST /v1/service/stop     stops the hub
"""
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
TOKEN = secrets.token_hex(16)
STATE = {"count": 0}
OPS = [
    {"id": "echo.say", "group": "echo", "summary": "Say something back", "mutating": False,
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"id": "counter.add", "group": "counter", "summary": "Add to the counter", "mutating": True,
     "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    {"id": "counter.get", "group": "counter", "summary": "Read the counter", "mutating": False,
     "input_schema": {"type": "object"}},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path == "/v1/health":
            return self._send(200, {"ok": True, "pid": os.getpid(), "version": "0.1.0"})
        if not self._authorized():
            return self._send(401, {"error": {"code": "unauthorized", "message": "a token is needed"}})
        if self.path == "/v1/operations":
            return self._send(200, {"operations": OPS})
        return self._send(404, {"error": {"code": "not_found", "message": self.path}})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": {"code": "unauthorized", "message": "a token is needed"}})
        n = int(self.headers.get("Content-Length") or 0)
        args = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path == "/v1/service/stop":
            self._send(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        if self.path.startswith("/v1/call/"):
            op = self.path[len("/v1/call/"):]
            if op == "echo.say":
                return self._send(200, {"result": {"said": args.get("text", "")}})
            if op == "counter.add":
                STATE["count"] += int(args.get("n", 1))
                return self._send(200, {"result": {"count": STATE["count"]}})
            if op == "counter.get":
                return self._send(200, {"result": {"count": STATE["count"]}})
            return self._send(404, {"error": {"code": "not_found", "message": f"no operation {op}"}})
        return self._send(404, {"error": {"code": "not_found", "message": self.path}})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    DATA.mkdir(parents=True, exist_ok=True)
    control = DATA / "control.json"
    control.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}", "token": TOKEN,
                                   "pid": os.getpid(), "version": "0.1.0"}), encoding="utf-8")
    try:
        server.serve_forever()
    finally:
        try:
            control.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
