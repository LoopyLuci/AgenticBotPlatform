"""mesh-llm as a Local AI backend (bot/localai/mesh.py): a real node - its OpenAI API on one port and its console's
management API on another - discovered, listed in /api/tags, proxied for chat (streaming and not), given ABP's
"+memory" block, and degrading quietly when it is not running.

Everything here is real. The node is a local HTTP server answering the routes and payload shapes mesh-llm itself
answers (taken from its docs and its Rust sources), ABP's server is the real Starlette app talking to it over real
HTTP, and the memory block comes from a real memory fabric with real memories in it. mesh-llm itself is installed on
this machine (the release binary on PATH) but is deliberately NOT started here: it downloads models and loads GPUs.
What stands in for it is the node's own program - the thing ABP talks to, not the thing under test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ID = "GLM-4.7-Flash-Q4_K_M"
OTHER = "Qwen3-8B-Q4_K_M"
ANSWER = "mesh says hello"
STREAMED = ["mesh ", "says ", "hello"]


def write_gguf(path: Path, arch: str = "llama", name: str = "tiny", ctx: int = 2048, pad: int = 0) -> Path:
    """A real GGUF v3 file (metadata only, no tensors), padded so it counts as a model."""
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [("general.architecture", 8, s(arch)), ("general.name", 8, s(name)),
          (f"{arch}.context_length", 4, struct.pack("<I", ctx)), ("general.file_type", 4, struct.pack("<I", 15))]
    out = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv))
    for k, t, v in kv:
        out += s(k) + struct.pack("<I", t) + v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out + b"\0" * pad)
    return path


# ---- a real mesh-llm node --------------------------------------------------------------------------------------------- #

MODELS = {"object": "list", "data": [
    {"id": ID, "display_name": ID, "object": "model", "owned_by": "mesh-llm", "capabilities": ["text", "reasoning"],
     "metadata": {"architecture": "qwen3", "parameter_size": "4B", "quant": "Q4_K_M", "context_length": 131072}},
    {"id": OTHER, "display_name": "Mesh LLM " + OTHER, "object": "model", "owned_by": "mesh-llm", "capabilities": ["text"],
     "metadata": {"architecture": "qwen3", "parameter_size": "8B", "quant": "Q4_K_M", "context_length": 40960}},
]}

# the console's own view of the mesh (crates/mesh-llm-host-runtime/src/api/status.rs: StatusPayload)
STATUS = {
    "version": "0.74.0", "node_id": "node-local", "node_state": "serving", "node_status": "Serving", "is_host": True,
    "mesh_name": "abp-mesh", "my_hostname": "workstation", "my_vram_gb": 24.0,
    "serving_models": [ID], "available_models": [ID, OTHER],
    "gpus": [{"name": "AMD Radeon RX 7900 XTX", "vram_bytes": 25753026560}],
    "peers": [{"id": "peer-laptop", "hostname": "laptop", "state": "serving", "role": "worker", "vram_gb": 16.0,
               "rtt_ms": 3, "available_models": [OTHER], "serving_models": [OTHER],
               "gpus": [{"name": "NVIDIA RTX 4070 Ti", "vram_bytes": 12897573888}]}],
    "runtime": {"backend": "vulkan", "models": [{"name": ID, "profile": "chat", "backend": "skippy", "status": "ready",
                                                 "port": 45123, "context_length": 131072}]},
}


class _Node:
    """One real HTTP server playing one of mesh-llm's two ports: the OpenAI API (`role="api"`) or the console's
    read-only management API (`role="console"`). Every request body is kept, so a test can see what ABP proxied."""

    def __init__(self, role: str = "api") -> None:
        self.role = role
        self.drop = False
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):
                path = self.path.split("?")[0]
                if outer.role == "api" and path == "/health":
                    return self._json(200, {"status": "ok", "mode": "serving",
                                           "mesh": {"status": "connected", "admitted_peer_count": 2, "connected_peer_count": 2},
                                           "serving": {"status": "healthy", "models": [ID]}})
                if outer.role == "api" and path == "/v1/models":
                    return self._json(200, MODELS)
                if outer.role == "console" and path == "/api/status":
                    return self._json(200, STATUS)
                self._json(404, {"error": "not found"})

            def do_POST(self):
                path = self.path.split("?")[0]
                body = self._body()
                if outer.role != "api" or path != "/v1/chat/completions":
                    return self._json(404, {"error": "not found"})
                with outer.lock:
                    outer.requests.append(body)
                if body.get("stream"):
                    return self._sse(body)
                self._json(200, {"id": "chatcmpl-1", "object": "chat.completion", "model": body.get("model"), "created": 1,
                                 "choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant", "content": ANSWER}}],
                                 "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}})

            def _sse(self, body: dict):
                def chunk(delta: dict, finish=None) -> bytes:
                    return ("data: " + json.dumps({"id": "chatcmpl-1", "object": "chat.completion.chunk",
                                                  "model": body.get("model"), "created": 1,
                                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for i, piece in enumerate(STREAMED):
                    self.wfile.write(chunk({"content": piece}))
                    self.wfile.flush()
                    if outer.drop and i == 0:       # the node dies mid-answer: a reset, not a clean end of stream
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        self.connection.close()
                        return
                self.wfile.write(chunk({}, "stop"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name=f"mesh-{role}")

    def __enter__(self) -> "_Node":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def last(self) -> dict:
        with self.lock:
            return self.requests[-1]


def closed_port() -> int:
    """A port on this machine with nothing listening on it (bind, then let go)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- fixtures ---------------------------------------------------------------------------------------------------------- #

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_LOCALAI_HOME", str(tmp_path / "localai"))
    from bot.neurallab import systune
    systune._cache.clear()
    return tmp_path / "localai"


@pytest.fixture
def node(home):
    """A mesh-llm node on both of its ports, and ABP's settings pointing at them."""
    from bot.localai import engine, mesh
    with _Node("api") as api, _Node("console") as console:
        engine.set_settings({"mesh_url": api.url, "mesh_console_url": console.url})
        mesh.forget()
        yield api
    mesh.forget()


@pytest.fixture
def fabric(temp_db):
    """A real memory fabric with one shared memory in it, so "+memory" has something to add."""
    from bot.fileserver.index import hash_vectors
    from bot.memoryfabric import store
    store.reset_cache()
    store._embed_cache["e"] = ("hash-tfidf", hash_vectors, time.time() + 3600)   # the always-present embedder
    store.set_settings({"shared_approval": False})
    store.remember("The workshop's second GPU lives in the mesh, not in this machine.", shared=True)
    yield store
    store.reset_cache()


def client() -> TestClient:
    from bot.localai import server
    return TestClient(server.build_app())


def _ndjson(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---- discovery ---------------------------------------------------------------------------------------------------------- #

def test_discovery_reads_the_node_its_peers_and_its_models(node):
    from bot.localai import mesh

    st = mesh.status()
    assert st["reachable"] and st["console"] and not st["error"]
    assert st["url"] == node.url
    assert st["node"]["hostname"] == "workstation" and st["node"]["state"] == "serving" and st["node"]["mesh"] == "abp-mesh"
    assert st["gpus"] == [{"name": "AMD Radeon RX 7900 XTX", "vram_gb": 24.0, "backend": ""}]
    assert st["serving"] == [ID] and st["loaded"][0]["context_length"] == 131072
    peer = st["peers"][0]
    assert (peer["hostname"], peer["state"], peer["serving"], peer["rtt_ms"]) == ("laptop", "serving", [OTHER], 3)
    assert peer["gpus"][0]["name"] == "NVIDIA RTX 4070 Ti"
    models = {m["id"]: m for m in mesh.list_models()}
    assert set(models) == {ID, OTHER}
    assert models[ID]["name"] == "mesh/" + ID and models[ID]["context_length"] == 131072
    assert models[ID]["quantization"] == "Q4_K_M" and models[ID]["architecture"] == "qwen3"
    # which model runs where, as the mesh says
    assert models[ID]["where"] == "workstation" and models[OTHER]["where"] == "laptop"


def test_a_local_model_keeps_its_own_name_and_wins_over_the_mesh_namespace(node, tmp_path):
    from bot.localai import mesh, models, server

    models.import_file("mesh/tiny:q4", str(write_gguf(tmp_path / "t.gguf")))
    assert server._record("mesh/tiny:q4")["engine"] == "llamacpp"          # a real model in ABP's own store
    assert server._record("mesh/" + ID)["engine"] == "mesh"
    # ABP's own naming adds Ollama's implicit tag; the node knows the id without it
    assert mesh.mesh_id("mesh/" + ID + ":latest") == ID
    assert mesh.mesh_id("mesh/" + ID) == ID


# ---- what ABP's own server lists -------------------------------------------------------------------------------------- #

def test_tags_lists_mesh_models_next_to_the_local_ones(node, tmp_path):
    from bot.localai import models

    models.import_file("me/tiny:q4", str(write_gguf(tmp_path / "t.gguf")))
    c = client()
    tags = {m["name"]: m for m in c.get("/api/tags").json()["models"]}
    assert set(tags) == {"me/tiny:q4", "mesh/" + ID, "mesh/" + OTHER}
    mesh_tag = tags["mesh/" + ID]
    assert mesh_tag["details"]["format"] == "mesh" and mesh_tag["details"]["engine"] == "mesh-llm"
    assert mesh_tag["details"]["quantization_level"] == "Q4_K_M" and mesh_tag["details"]["parameter_size"] == "4B"
    assert mesh_tag["details"]["where"] == "workstation" and mesh_tag["engine"] == "mesh-llm"
    assert tags["me/tiny:q4"]["details"]["format"] == "gguf"                # ABP's own models are unchanged
    v1 = {m["id"]: m["owned_by"] for m in c.get("/v1/models").json()["data"]}
    assert v1 == {"me/tiny:q4": "abp", "mesh/" + ID: "mesh-llm", "mesh/" + OTHER: "mesh-llm"}


def test_show_answers_for_a_mesh_model_with_what_the_mesh_says(node):
    c = client()
    out = c.post("/api/show", json={"model": "mesh/" + ID}).json()
    assert out["details"]["format"] == "mesh" and out["abp"]["mesh_model"] == ID
    assert out["model_info"]["context_length"] == 131072
    assert "completion" in out["capabilities"] and "thinking" in out["capabilities"] and "tools" in out["capabilities"]
    assert "T" in out["modified_at"] and out["abp"]["where"] == "workstation"


# ---- proxying ------------------------------------------------------------------------------------------------------------ #

def test_chat_is_proxied_to_the_node_with_abps_own_model_name_translated(node):
    c = client()
    out = c.post("/api/chat", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hello"}],
                                    "stream": False}).json()
    assert out["message"]["content"] == ANSWER and out["done"] and out["model"] == "mesh/" + ID
    seen = node.last()
    assert seen["model"] == ID                                     # the mesh routes on its own id, not on ABP's name
    assert seen["messages"] == [{"role": "user", "content": "hello"}] and seen["stream"] is False
    # the bare id works too, since the node serves it
    out = c.post("/api/chat", json={"model": ID, "messages": [{"role": "user", "content": "hi"}], "stream": False})
    assert out.json()["message"]["content"] == ANSWER


def test_chat_streams_the_nodes_deltas_as_ollama_chunks(node):
    c = client()
    body = c.post("/api/chat", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hello"}]}).text
    chunks = _ndjson(body)
    assert [x["message"]["content"] for x in chunks if not x["done"]] == STREAMED
    assert chunks[-1]["done"] and chunks[-1]["done_reason"] == "stop"
    assert all(x["model"] == "mesh/" + ID for x in chunks)
    assert node.last()["stream"] is True


def test_generate_proxies_the_prompt_and_the_openai_route_passes_sse_through(node):
    c = client()
    out = c.post("/api/generate", json={"model": "mesh/" + ID, "prompt": "hello", "stream": False}).json()
    assert out["response"] == ANSWER and out["done"] and out["model"] == "mesh/" + ID
    assert node.last()["messages"][-1]["content"] == "hello"
    body = c.post("/v1/chat/completions", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hello"}],
                                               "stream": True}).text
    assert "data: " in body and all(p in body for p in STREAMED)
    one = c.post("/v1/chat/completions", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hello"}],
                                               "stream": False}).json()
    assert one["choices"][0]["message"]["content"] == ANSWER and one["model"] == "mesh/" + ID


def test_raw_and_fill_in_the_middle_are_local_engine_features(node):
    c = client()
    r = c.post("/api/generate", json={"model": "mesh/" + ID, "prompt": "hi", "raw": True})
    assert r.status_code == 400 and "llama.cpp" in r.json()["error"]
    assert node.requests == []                                        # nothing was proxied


def test_a_node_that_goes_away_mid_answer_says_so_instead_of_falling_over(node):
    node.drop = True
    c = client()
    chunks = _ndjson(c.post("/api/chat", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hi"}]}).text)
    assert chunks[0]["message"]["content"] == STREAMED[0]                    # what did arrive is kept
    assert any("error" in x for x in chunks) and not chunks[-1].get("done")
    body = c.post("/v1/chat/completions", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hi"}],
                                               "stream": True}).text
    assert "api_error" in body


def test_a_mesh_model_is_not_in_abps_store_to_copy_or_delete(node):
    c = client()
    r = c.request("DELETE", "/api/delete", json={"model": "mesh/" + ID})
    assert r.status_code == 404 and "mesh" in r.json()["error"]
    r = c.post("/api/copy", json={"source": "mesh/" + ID, "destination": "me/copied:q4"})
    assert r.status_code == 404 and "mesh" in r.json()["error"]
    assert len(mesh_names(c)) == 2                                    # the mesh still serves them


def mesh_names(c: TestClient) -> list[str]:
    return [m["name"] for m in c.get("/api/tags").json()["models"] if m["name"].startswith("mesh/")]


# ---- "+memory" ---------------------------------------------------------------------------------------------------------- #

def test_plus_memory_puts_the_memory_block_in_the_proxied_request(node, fabric):
    c = client()
    out = c.post("/api/chat", json={"model": f"mesh/{ID}+memory", "messages": [{"role": "user", "content": "where is the second GPU?"}],
                                    "stream": False}).json()
    assert out["message"]["content"] == ANSWER and out["model"] == "mesh/" + ID      # the suffix is taken off
    seen = node.last()
    assert seen["model"] == ID
    assert seen["messages"][0]["role"] == "system" and "second GPU lives in the mesh" in seen["messages"][0]["content"]
    assert seen["messages"][1] == {"role": "user", "content": "where is the second GPU?"}
    # the same block through the OpenAI route, where it joins the messages it is sent
    c.post("/v1/chat/completions", json={"model": f"mesh/{ID}+memory", "messages": [{"role": "user", "content": "hi"}],
                                          "stream": False})
    assert "second GPU lives in the mesh" in node.last()["messages"][0]["content"]
    # and without the suffix nothing is added
    c.post("/api/chat", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hi"}], "stream": False})
    assert [m["role"] for m in node.last()["messages"]] == ["user"]


# ---- a node that is not running ---------------------------------------------------------------------------------------- #

def test_a_mesh_that_is_not_running_changes_nothing_but_its_own_status(home, tmp_path):
    from bot.localai import engine, mesh, models
    from bot.localai.paths import LocalAIError

    models.import_file("me/tiny:q4", str(write_gguf(tmp_path / "t.gguf")))
    engine.set_settings({"mesh_url": f"http://127.0.0.1:{closed_port()}"})
    mesh.forget()

    st = mesh.status()
    assert st["reachable"] is False and st["models"] == [] and st["peers"] == []
    assert "not reachable" in st["error"] and st["url"] in st["error"]
    assert mesh.listing() == [] and mesh.routed("a-name-only-a-local-model-would-have") is None
    with pytest.raises(LocalAIError, match="not reachable"):
        mesh.routed("mesh/" + ID)

    c = client()
    r = c.get("/api/tags")
    assert r.status_code == 200 and [m["name"] for m in r.json()["models"]] == ["me/tiny:q4"]
    assert [m["id"] for m in c.get("/v1/models").json()["data"]] == ["me/tiny:q4"]
    out = c.post("/api/chat", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hi"}], "stream": False})
    assert out.status_code == 400 and "not reachable" in out.json()["error"]
    v1 = c.post("/v1/chat/completions", json={"model": "mesh/" + ID, "messages": [{"role": "user", "content": "hi"}]})
    assert v1.status_code == 400 and "not reachable" in v1.json()["error"]["message"]
    raw = c.post("/api/generate", json={"model": "mesh/" + ID, "prompt": "hi", "raw": True})
    assert raw.status_code == 400                                      # the store-only paths are checked first


def test_the_dashboard_route_and_the_overview_report_the_mesh(node):
    from fastapi import FastAPI
    from bot.dashboard import localai_api

    app = FastAPI()
    localai_api.register(app, lambda: None)
    c = TestClient(app)
    out = c.get("/api/localai/mesh").json()
    assert out["reachable"] and {m["id"] for m in out["models"]} == {ID, OTHER}
    assert out["peers"][0]["hostname"] == "laptop" and out["node"]["mesh"] == "abp-mesh"
    assert c.get("/api/localai").json()["mesh"]["reachable"] is True


# ---- the CLI ------------------------------------------------------------------------------------------------------------ #

class _Cli:
    """What abp_cli/ai.py asks the dashboard for, answered from one payload."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def _request(self, method, path, **kw):
        assert method == "GET" and path == "/api/localai/mesh", (method, path)
        return self.payload


def _mesh_args(action: str) -> argparse.Namespace:
    return argparse.Namespace(ai_cmd="mesh", action=action, json=False)


def test_the_cli_parses_the_mesh_subcommand():
    from abp_cli import ai

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    ai.add_parser(sub)
    assert (lambda a: (a.ai_cmd, a.action))(p.parse_args(["ai", "mesh", "status"])) == ("mesh", "status")
    assert p.parse_args(["ai", "mesh", "models"]).action == "models"


def test_the_cli_prints_the_mesh_status_and_its_models(node, capsys):
    from abp_cli import ai
    from bot.localai import mesh

    assert asyncio.run(ai.run(_mesh_args("status"), _Cli(mesh.status()))) == 0
    out = capsys.readouterr().out
    assert f"reachable at {node.url}" in out and "workstation" in out and "AMD Radeon RX 7900 XTX 24.0 GB" in out
    assert "peer: laptop" in out and f"serving: {ID}" in out
    assert asyncio.run(ai.run(_mesh_args("models"), _Cli(mesh.status()))) == 0
    listed = capsys.readouterr().out
    assert f"mesh/{ID}" in listed and "131072" in listed and "Q4_K_M" in listed and "workstation" in listed


def test_the_cli_says_so_when_the_mesh_is_not_there(home, capsys):
    from abp_cli import ai
    from bot.localai import engine, mesh

    engine.set_settings({"mesh_url": f"http://127.0.0.1:{closed_port()}"})
    mesh.forget()
    assert asyncio.run(ai.run(_mesh_args("status"), _Cli(mesh.status()))) == 1
    assert "not reachable" in capsys.readouterr().out
    assert asyncio.run(ai.run(_mesh_args("models"), _Cli(mesh.status()))) == 1
    assert "not reachable" in capsys.readouterr().out
