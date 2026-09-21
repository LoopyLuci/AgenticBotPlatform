"""A tiny Language Server Protocol server for tests: it reports a problem on every line containing
"BROKEN" (an error) or "TODO" (a warning), answers documentSymbol / definition / references / hover, and
publishes twice per change (an empty list first, then the real one) the way real servers do."""
import json
import os
import sys

PULL = os.environ.get("FAKE_LSP_PULL") == "1"     # answer textDocument/diagnostic instead of pushing
LOADING = int(os.environ.get("FAKE_LSP_LOADING", "0"))   # refuse this many diagnostic requests first, like rust-analyzer while loading


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(message):
    body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()


docs = {}


def diagnostics(text):
    out = []
    for i, line in enumerate(text.splitlines()):
        for word, severity in (("BROKEN", 1), ("TODO", 2)):
            col = line.find(word)
            if col >= 0:
                out.append({"range": {"start": {"line": i, "character": col}, "end": {"line": i, "character": col + len(word)}},
                            "severity": severity, "source": "fake", "message": f"found {word}"})
    return out


def publish(uri):
    if PULL:
        return
    send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}})
    send({"method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": diagnostics(docs[uri])}})


while True:
    msg = read()
    if msg is None:
        break
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        caps = {"textDocumentSync": 1, "documentSymbolProvider": True, "hoverProvider": True}
        if PULL:
            caps["diagnosticProvider"] = {"identifier": "fake", "interFileDependencies": False, "workspaceDiagnostics": False}
        send({"id": rid, "result": {"capabilities": caps}})
        send({"id": 900, "method": "workspace/configuration", "params": {"items": []}})     # a server-initiated request
    elif method == "textDocument/didOpen":
        doc = params["textDocument"]
        docs[doc["uri"]] = doc["text"]
        publish(doc["uri"])
    elif method == "textDocument/didChange":
        uri = params["textDocument"]["uri"]
        docs[uri] = params["contentChanges"][-1]["text"]
        publish(uri)
    elif method == "textDocument/diagnostic" and LOADING > 0:
        LOADING -= 1
        send({"id": rid, "error": {"code": -32802, "message": "server cancelled the request"}})
    elif method == "textDocument/diagnostic":
        send({"id": rid, "result": {"kind": "full", "items": diagnostics(docs[params["textDocument"]["uri"]])}})
    elif method == "textDocument/documentSymbol":
        send({"id": rid, "result": [{"name": "Outer", "kind": 5, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 5, "character": 0}},
                                     "children": [{"name": "inner", "kind": 6, "range": {"start": {"line": 1, "character": 0}, "end": {"line": 2, "character": 0}}}]}]})
    elif method == "textDocument/definition":
        send({"id": rid, "result": {"uri": params["textDocument"]["uri"], "range": {"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 9}}}})
    elif method == "textDocument/references":
        send({"id": rid, "result": [{"uri": params["textDocument"]["uri"], "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
                                    {"uri": params["textDocument"]["uri"], "range": {"start": {"line": 7, "character": 2}, "end": {"line": 7, "character": 3}}}]})
    elif method == "textDocument/hover":
        send({"id": rid, "result": {"contents": {"kind": "markdown", "value": "def add(a, b) -> int"}}})
    elif method == "shutdown":
        send({"id": rid, "result": None})
    elif method == "exit":
        break
    elif rid is not None and method:
        send({"id": rid, "error": {"code": -32601, "message": "not supported"}})
