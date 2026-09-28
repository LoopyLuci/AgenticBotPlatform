"""Ollama from ABP: finding it, every route it serves, pull jobs with progress, loading at a context that fits (and the
-abp serving model that keeps requests from loading at a huge default), GGUF import, moving old models in, the agent's
tools, the dashboard API and the page. Everything runs against a stand-in Ollama."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from bot.ollama import client, harness

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:11434"


class FakeOllama:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.models = {"qwen3.5:9b": {"parameters": "temperature 1"}}
        self.loaded: dict[str, int] = {}
        self.blobs: set[str] = set()

    def handle(self, method, url, body=None, content=None):
        path = httpx.URL(url).path
        self.calls.append((method, path, body))
        if path == "/api/version":
            return 200, {"version": "0.34.4"}
        if path == "/api/tags":
            return 200, {"models": [{"name": n, "size": 6_000_000_000, "details": {"family": "qwen35", "parameter_size": "9B",
                                                                                    "quantization_level": "Q4_K_M"}} for n in self.models]}
        if path == "/api/ps":
            return 200, {"models": [{"name": n, "size_vram": 6_500_000_000, "context_length": c} for n, c in self.loaded.items()]}
        if path == "/api/show":
            m = self.models.get(body["model"])
            if m is None:
                return 404, {"error": f"model '{body['model']}' not found"}
            return 200, {"capabilities": ["completion", "tools"], "parameters": m["parameters"], "modelfile": "FROM x",
                         "model_info": {"qwen35.context_length": 262144}}
        if path == "/api/generate":
            if body.get("keep_alive") == 0:
                self.loaded.pop(body["model"], None)
            else:
                self.loaded[body["model"]] = (body.get("options") or {}).get("num_ctx", 262144)
            return 200, {"done": True}
        if path == "/api/copy":
            self.models[body["destination"]] = dict(self.models[body["source"]])
            return 200, {}
        if path == "/api/delete":
            self.models.pop(body["model"], None)
            return 200, {}
        if path.startswith("/api/blobs/"):
            digest = path.rsplit("/", 1)[-1]
            if method == "HEAD":
                return (200 if digest in self.blobs else 404), {}
            self.blobs.add(digest)
            return 201, {}
        if path == "/api/status":
            return 200, {"cloud": {"disabled": False}}
        if path == "/api/me":
            return 200, {"name": "someone", "plan": "free"}
        if path == "/api/experimental/model-recommendations":
            return 200, {"recommendations": [{"model": "gemma4:31b-cloud", "required_plan": "free"}]}
        if path == "/api/web_search":
            return 200, {"results": [{"title": "t", "url": "u"}]}
        return 404, {"error": f"no route {path}"}

    def stream(self, path, body):
        self.calls.append(("POST", path, body))
        if path == "/api/pull":
            if body["model"] == "missing:1b":
                return [{"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist"}]
            self.models[body["model"]] = {"parameters": ""}
            return [{"status": "pulling manifest"}, {"status": "pulling abc", "total": 100, "completed": 40},
                    {"status": "pulling abc", "total": 100, "completed": 100}, {"status": "success"}]
        if path == "/api/create":
            params = "\n".join(f"{k} {v}" for k, v in (body.get("parameters") or {}).items())
            self.models[body["model"]] = {"parameters": params, "spec": body}
            return [{"status": "using existing layer"}, {"status": "success"}]
        return [{"error": "no"}]


@pytest.fixture
def ollama(monkeypatch, temp_db):
    from bot import providers

    fake = FakeOllama()

    def resp(status, data, method, url):
        return httpx.Response(status, json=data, request=httpx.Request(method, url))

    class Streamed:
        def __init__(self, lines, url):
            self.status_code = 200
            self._lines = [json.dumps(x) for x in lines]
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_lines(self):
            return iter(self._lines)

        def read(self):
            return b""

    class Shim:
        HTTPError = httpx.HTTPError
        TimeoutException = httpx.TimeoutException
        Timeout = httpx.Timeout

        @staticmethod
        def get(url, timeout=None, **_k):
            return resp(*fake.handle("GET", url), "GET", url)

        @staticmethod
        def request(method, url, json=None, content=None, **_k):
            return resp(*fake.handle(method, url, json, content), method, url)

        @staticmethod
        def post(url, content=None, **_k):
            return resp(*fake.handle("POST", url, None, content), "POST", url)

        @staticmethod
        def stream(method, url, json=None, **_k):
            return Streamed(fake.stream(httpx.URL(url).path, json), url)

    monkeypatch.setattr(client, "httpx", Shim)
    monkeypatch.setattr("httpx.post", Shim.post)
    monkeypatch.setattr(harness, "server_settings", lambda: {"OLLAMA_CONTEXT_LENGTH": "262144", "OLLAMA_HOST": "0.0.0.0",
                                                             "OLLAMA_MODELS": str(ROOT / "nonexistent-models-dir")})
    providers.set_provider("ollama", base_url=BASE + "/v1")
    client._reset_for_tests()
    with harness._jobs_lock:
        harness._jobs.clear()
    client.find()                 # the tools' per-turn check reads this cache
    yield fake
    client._reset_for_tests()


def _wait_job(job_id: str) -> dict:
    for _ in range(200):
        j = harness.job(job_id)
        if j and j["state"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


# ---- finding Ollama, and every route -------------------------------------------------------------------------------------
def test_ollama_is_found_from_the_provider(ollama):
    o = client.find()
    assert o.root == BASE and o.provider == "ollama" and o.version == "0.34.4"


def test_the_route_table_covers_ollama_and_unknown_routes_still_work(ollama):
    ids = {o["id"] for o in client.operations()}
    for need in ("tags", "ps", "show", "pull", "push", "create", "copy", "delete", "blob_exists", "blob_upload", "generate", "chat", "embed",
                 "me", "signout", "web_search", "web_fetch", "model_recommendations", "openai_chat", "openai_responses", "anthropic_messages",
                 "tokenize", "openai_transcriptions"):
        assert need in ids, need
    assert not client.find_operation("chat")["mutating"] and client.find_operation("delete")["mutating"]
    assert client.call("web_search", {"query": "x"})["results"][0]["url"] == "u"
    assert client.call("blob_exists", {"digest": "sha256:ab"}) == {"exists": False, "status": 404}
    assert client.call("GET /api/version") == {"version": "0.34.4"}, "a route the table does not know is still callable"
    with pytest.raises(client.OllamaError, match="needs 'digest'"):
        client.call("blob_exists", {})


# ---- pulling, loading, the serving model --------------------------------------------------------------------------------------
def test_a_pull_is_a_job_with_progress_and_a_failed_pull_says_why(ollama):
    j = _wait_job(harness.pull("gemma4:26b")["id"])
    assert j["state"] == "done" and j["total"] == 100 and j["completed"] == 100 and "gemma4:26b" in ollama.models
    again = harness.pull("gemma4:26b")
    assert again["id"] != j["id"], "a finished pull does not block a new one"
    bad = _wait_job(harness.pull("missing:1b")["id"])
    assert bad["state"] == "failed" and "does not exist" in bad["error"]
    with pytest.raises(client.OllamaError, match="does not exist"):
        harness.pull("missing:1b", wait=True)


def test_load_uses_a_context_that_fits(ollama, monkeypatch):
    out = harness.load("qwen3.5:9b")
    assert out["context_length"] == harness.DEFAULT_CONTEXT and ollama.loaded["qwen3.5:9b"] == 32768
    from bot.config import config

    monkeypatch.setattr(config, "_data", {**config._data, "ollama": {"context_length": 8192}})
    assert harness.load("qwen3.5:9b")["context_length"] == 8192
    harness.unload("qwen3.5:9b")
    assert "qwen3.5:9b" not in ollama.loaded


def test_requests_go_to_a_serving_model_that_will_not_load_huge(ollama):
    """OpenAI-style requests carry no context, so a model without its own num_ctx loads at OLLAMA_CONTEXT_LENGTH (262144 here)."""
    name = harness.serving_model("qwen3.5:9b")
    assert name == "qwen3.5-abp:9b" and "num_ctx 32768" in ollama.models[name]["parameters"]
    assert ollama.models[name]["spec"]["from"] == "qwen3.5:9b", "built on the model: it shares its files"
    creates = sum(1 for c in ollama.calls if c[1] == "/api/create")
    assert harness.serving_model("qwen3.5:9b") == name and sum(1 for c in ollama.calls if c[1] == "/api/create") == creates, "made once"
    ollama.models["tidy:1b"] = {"parameters": "num_ctx 8192"}
    assert harness.serving_model("tidy:1b") == "tidy:1b", "a model with a sane context of its own is used as it is"
    assert harness.serving_model("gemma4:31b-cloud") == "gemma4:31b-cloud"


def test_status_warns_about_the_servers_settings(ollama):
    s = harness.summary()
    assert s["version"] == "0.34.4" and s["account"] == {"name": "someone", "plan": "free"} and s["cloud_enabled"]
    assert any("262,144" in w for w in s["warnings"]) and any("every network address" in w for w in s["warnings"])
    assert harness.models()[0]["model"] == "qwen3.5:9b" and harness.show("qwen3.5:9b")["context_length"] == 262144


def test_copy_delete_and_create(ollama):
    harness.copy("qwen3.5:9b", "mine:latest")
    assert "mine:latest" in ollama.models
    harness.delete("mine:latest")
    assert "mine:latest" not in ollama.models
    harness.create("helper:latest", {"from": "qwen3.5:9b", "system": "be brief", "template": None, "parameters": {"temperature": 0.2}})
    spec = ollama.models["helper:latest"]["spec"]
    assert spec["system"] == "be brief" and "template" not in spec


def test_a_gguf_file_is_imported_once(ollama, tmp_path):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF" + b"x" * 1000)
    digest = "sha256:" + hashlib.sha256(gguf.read_bytes()).hexdigest()
    harness.import_gguf(str(gguf), "imported:latest")
    spec = ollama.models["imported:latest"]["spec"]
    assert spec["files"] == {"m.gguf": digest} and spec["parameters"]["num_ctx"] == harness.DEFAULT_CONTEXT
    uploads = sum(1 for c in ollama.calls if c[0] == "POST" and c[1].startswith("/api/blobs/"))
    harness.import_gguf(str(gguf), "imported2:latest")
    assert sum(1 for c in ollama.calls if c[0] == "POST" and c[1].startswith("/api/blobs/")) == uploads, "a blob Ollama has is not sent again"
    with pytest.raises(client.OllamaError, match="not a .gguf"):
        harness.import_gguf(str(tmp_path / "nope.bin"), "x")


def test_old_models_are_moved_verified_and_merged(ollama, monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    data = b"weights"
    name = "sha256-" + hashlib.sha256(data).hexdigest()
    (old / "blobs").mkdir(parents=True)
    (old / "blobs" / name).write_bytes(data)
    (old / "manifests" / "registry.ollama.ai" / "library" / "qwen3").mkdir(parents=True)
    (old / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "1.7b").write_text("{}")
    (new / "manifests" / "registry.ollama.ai" / "library" / "keep").mkdir(parents=True)
    (new / "manifests" / "registry.ollama.ai" / "library" / "keep" / "latest").write_text("mine")
    monkeypatch.setattr(harness, "storage", lambda settings=None: {"models_dir": str(new)})
    out = harness.move_models(str(old))
    assert out["blobs_copied"] == 1 and out["manifests"] == 1
    assert (new / "blobs" / name).read_bytes() == data and not (old / "blobs" / name).exists()
    assert (new / "manifests" / "registry.ollama.ai" / "library" / "keep" / "latest").read_text() == "mine", "nothing overwritten"


def test_a_corrupt_copy_removes_nothing(ollama, monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "blobs").mkdir(parents=True)
    (old / "manifests").mkdir()
    bad = old / "blobs" / ("sha256-" + "0" * 64)
    bad.write_bytes(b"not what the name says")
    monkeypatch.setattr(harness, "storage", lambda settings=None: {"models_dir": str(new)})
    with pytest.raises(client.OllamaError, match="did not match"):
        harness.move_models(str(old))
    assert bad.exists() and not list((new / "blobs").iterdir())


# ---- a bot on an Ollama model ---------------------------------------------------------------------------------------------------
def test_a_bot_on_ollama_is_served_by_the_abp_model(ollama, monkeypatch, tmp_path):
    from bot import bot_instances
    from bot.backends import native_backend
    from bot.backends.custom_model_backend import CustomModelBackend

    native_backend._ollama_names.clear()
    sent = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append(json["model"])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", Client)
    iid = bot_instances.create_instance(name="ollama-bot", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[])
    backend = CustomModelBackend(provider_name="ollama", model_id="qwen3.5:9b", base_url=BASE + "/v1")
    assert asyncio.run(backend.ask("hello", context={"cwd": str(tmp_path / "ws"), "instance_id": iid})).text == "hi"
    assert sent == ["qwen3.5-abp:9b"]
    from bot.router_brain import learn

    assert learn.stats("ollama/qwen3.5:9b")["ok"] == pytest.approx(1), "the router learns under the name the bot uses"


# ---- the agent's tools ------------------------------------------------------------------------------------------------------------
def test_the_tools(ollama):
    from bot.agent_runtime import tools, toolspec

    names = {n for n in toolspec.registered_names() if n.startswith("ollama")}
    assert names == {"ollama_status", "ollama_models", "ollama_pull", "ollama_load", "ollama_manage", "ollama_operations", "ollama_call"}
    for name in ("ollama_pull", "ollama_load", "ollama_manage", "ollama_call"):
        assert tools.is_dangerous(name), name
    assert not tools.is_dangerous("ollama_status") and not tools.is_dangerous("ollama_models")
    out = asyncio.run(tools.execute_tool("ollama_operations", {"operation": "web_search", "call_read": True, "args": {"query": "x"}},
                                         workspace=None, instance_id=1))
    assert '"url": "u"' in out
    refused = asyncio.run(tools.execute_tool("ollama_operations", {"operation": "delete", "call_read": True}, workspace=None, instance_id=1))
    assert "use ollama_call" in refused


# ---- the dashboard ---------------------------------------------------------------------------------------------------------------
def test_the_api(ollama, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/ollama/status").status_code == 401
    assert c.get("/api/ollama/status", headers=h).json()["summary"]["version"] == "0.34.4"
    assert c.get("/api/ollama/models", headers=h).json()["models"][0]["model"] == "qwen3.5:9b"
    assert c.get("/api/ollama/show?model=qwen3.5:9b", headers=h).json()["capabilities"] == ["completion", "tools"]
    assert c.get("/api/ollama/show?model=nope", headers=h).status_code == 404
    assert c.post("/api/ollama/pull", json={"model": "a:1b"}).status_code == 401
    job = c.post("/api/ollama/pull", headers=h, json={"model": "a:1b"}).json()
    assert _wait_job(job["id"])["state"] == "done"
    assert c.get("/api/ollama/jobs", headers=h).json()["jobs"][0]["model"] == "a:1b"
    assert c.post("/api/ollama/load", headers=h, json={"model": "a:1b", "context": 4096}).json()["context_length"] == 4096
    assert c.post("/api/ollama/copy", headers=h, json={"source": "a:1b", "destination": "b:1b"}).json()["destination"] == "b:1b"
    assert c.post("/api/ollama/delete", headers=h, json={"model": "b:1b"}).json()["deleted"]
    assert c.post("/api/ollama/delete", headers=h, json={}).status_code == 400
    assert c.get("/api/ollama/operations", headers=h).json()["count"] >= 35
    assert c.post("/api/ollama/call", headers=h, json={"operation": "me"}).json()["result"]["plan"] == "free"
    assert c.post("/api/ollama/call", headers=h, json={"operation": "nope"}).status_code == 404


def test_the_api_says_when_ollama_is_not_running(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setattr(client, "find", lambda **_k: None)
    monkeypatch.setattr(client, "find_cached", lambda: None)
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/ollama/status", headers=h).json()["summary"]["running"] is False
    r = c.get("/api/ollama/models", headers=h)
    assert r.status_code == 503 and "not running" in r.json()["detail"]


def test_the_page_is_identical_in_both_uis_and_mounted():
    import re

    dash = (ROOT / "bot/dashboard/static/ollama-panel.js").read_text(encoding="utf-8")
    assert dash == (ROOT / "desktop-app/ui/ollama-panel.js").read_text(encoding="utf-8")
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert '<section id="ollama">' in text and 'id="ol-root"' in text and 'href="#ollama"' in text
        assert re.search(r'<script src="(/static/)?ollama-panel\.js"></script>', text)
    from bot.dashboard import ollama_api

    src = Path(ollama_api.__file__).read_text(encoding="utf-8")
    for path in set(re.findall(r"['`](/api/ollama/[a-z/-]+)", dash)):
        assert f'"{path.rstrip("/")}' in src, path
