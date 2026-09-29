"""Unsloth Studio from ABP: finding it, every operation it describes, loading models (and loading one on demand when a
bot's call finds it unloaded), downloads, storage, training and export, the agent's tools, the dashboard API, and the
page. Everything runs against a stand-in Studio; tests/test_unsloth_live.py is not needed for these."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest

from bot.unsloth import client, harness

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8888"

SPEC = {
    "openapi": "3.1.0", "info": {"title": "Unsloth UI Backend", "version": "test"},
    "paths": {
        "/api/health": {"get": {"operationId": "health", "summary": "Health Check"}},
        "/api/models/gguf-variants": {"get": {"operationId": "variants", "summary": "Get Gguf Variants",
                                              "parameters": [{"name": "repo_id", "in": "query", "required": True, "schema": {"type": "string"}}]}},
        "/api/train/runs/{run_id}": {"delete": {"operationId": "delete_run", "summary": "Delete Training Run",
                                                "parameters": [{"name": "run_id", "in": "path", "required": True, "schema": {"type": "string"}}]}},
        "/api/inference/load": {"post": {"operationId": "load", "summary": "Load Model", "requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/LoadRequest"}}}}}},
        "/api/datasets/upload": {"post": {"operationId": "upload", "summary": "Upload Dataset", "requestBody": {"content": {
            "multipart/form-data": {"schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"},
                                                                               "note": {"type": "string"}}}}}}}},
    },
    "components": {"schemas": {"LoadRequest": {"type": "object", "required": ["model_path"],
                                               "properties": {"model_path": {"type": "string"}, "max_seq_length": {"type": "integer", "default": 0}}}}},
}


class FakeStudio:
    """A stand-in Unsloth Studio: answers the calls ABP makes and records them."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict, object]] = []
        self.loaded: list[str] = []
        self.load_delay = 0.0
        self.downloads = {}
        self.cache_home = r"E:\AIModels\Unsloth"
        self.key_ok = "unused"

    def handle(self, method: str, url: str, params=None, json_body=None, headers=None):
        u = httpx.URL(url)
        path = u.path
        params = dict(params or {})
        self.calls.append((method, path, params, json_body))
        if path != "/api/health" and (headers or {}).get("Authorization") != f"Bearer {self.key_ok}":
            return 401, {"detail": "no"}
        if path == "/api/health":
            return 200, {"status": "healthy", "service": client.SERVICE}
        if path == "/openapi.json":
            return 200, SPEC
        if path == "/api/inference/loaded-models":
            return 200, {"data": [{"id": m, "loaded": True} for m in self.loaded]}
        if path == "/v1/models":
            return 200, {"data": [{"id": m, "loaded": True, "owned_by": "unsloth-studio"} for m in self.loaded]
                                 + [{"id": "unsloth/cold", "loaded": False, "owned_by": "unsloth-studio"}]}
        if path == "/api/inference/load":
            time.sleep(self.load_delay)
            self.loaded.append(json_body["model_path"])
            return 200, {"context_length": json_body.get("max_seq_length"), "supports_reasoning": True}
        if path == "/api/inference/unload":
            self.loaded = [m for m in self.loaded if m != json_body["model_path"]]
            return 200, {}
        if path == "/api/models/gguf-variants":
            return 200, {"variants": [{"quant": "Q8_0", "downloaded": False}, {"quant": "Q4_K_M", "downloaded": True, "size_bytes": 5}]}
        if path == "/api/hub/download":
            self.downloads[json_body["repo_id"]] = ["running", "running", "complete"]
            return 200, {"state": "queued"}
        if path == "/api/hub/download-status":
            seq = self.downloads.get(params["repo_id"], ["idle"])
            return 200, {"state": seq.pop(0) if len(seq) > 1 else seq[0], "error": None}
        if path in ("/api/hub/gguf-download-progress", "/api/hub/download-progress"):
            return 200, {"progress": 0.5}
        if path == "/api/settings/hugging-face-cache":
            if method == "PUT":
                self.cache_home = json_body["cache_home"]
            return 200, {"cache_home": self.cache_home, "free_bytes": 10 ** 11, "writable": True, "is_custom": True}
        if path == "/api/models/scan-folders":
            return 200, {"folders": [{"id": 1, "path": ""}]}
        if path == "/api/train/start":
            return 200, {"job_id": "j1"}
        if path == "/api/datasets/upload":
            return 200, {"stored": [name for _f, name, _b in json_body["files"]], "sizes": [len(b) for _f, _n, b in json_body["files"]],
                         "form": json_body["form"]}
        if path.startswith("/api/train/runs/"):
            return 200, {"deleted": path.rsplit("/", 1)[-1]}
        if path == "/api/export/export/gguf":
            return 200, {"output_path": json_body["save_directory"]}
        if path in ("/api/system", "/api/train/hardware", "/api/inference/status", "/api/train/status", "/api/llama/backend", "/api/auth/status"):
            return 200, {"devices": [{"name": "GPU", "vram_used_gb": 1, "vram_total_gb": 24}], "backend": "rocm", "phase": "idle"}
        return 404, {"detail": f"no route {path}"}


def _resp(status, data, method, url):
    return httpx.Response(status, json=data, request=httpx.Request(method, url))


@pytest.fixture
def studio(monkeypatch, temp_db):
    from bot import providers

    fake = FakeStudio()

    class Shim:
        HTTPError = httpx.HTTPError
        TimeoutException = httpx.TimeoutException
        URL = httpx.URL

        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            return _resp(*fake.handle("GET", url, params, None, headers), "GET", url)

        @staticmethod
        def request(method, url, params=None, json=None, headers=None, timeout=None, files=None, data=None):
            if files is not None:
                json = {"files": [(field, item[0], item[1]) for field, item in files], "form": data}
            return _resp(*fake.handle(method, url, params, json, headers), method, url)

    monkeypatch.setattr(client, "httpx", Shim)
    monkeypatch.setattr(httpx, "get", Shim.get)   # the router's own check of local servers
    providers.set_provider("unsloth", base_url=BASE + "/v1", api_key="unused")
    client._reset_for_tests()
    client.find()                 # the tools' per-turn check reads this cache
    yield fake
    client._reset_for_tests()


# ---- finding Studio, and every operation it describes -------------------------------------------------------------------
def test_studio_is_found_from_the_provider_and_uses_its_key(studio):
    s = client.find()
    assert s.root == BASE and s.provider == "unsloth" and s.key == "unused" and s.openai_base == BASE + "/v1"


def test_a_wrong_key_says_what_to_do(studio):
    studio.key_ok = "other"
    with pytest.raises(client.StudioError, match="refused the key"):
        client.request("GET", "/api/inference/loaded-models")


def test_every_described_operation_is_listed_and_callable(studio):
    ops = client.operations()
    assert {o["id"] for o in ops} == {"health", "variants", "delete_run", "load", "upload"}
    load = client.find_operation("POST /api/inference/load")
    assert load["mutating"] and load["body"]["title"] == "LoadRequest" and load["body"]["properties"]["model_path"]
    assert not client.find_operation("variants")["mutating"]
    assert client.call("variants", {"repo_id": "a/b"})["variants"][1]["quant"] == "Q4_K_M"
    assert studio.calls[-1][2] == {"repo_id": "a/b"}
    assert client.call("delete_run", {"run_id": "r 1"}) == {"deleted": "r 1"}
    client.call("load", {"model_path": "m"})
    assert studio.calls[-1][3] == {"model_path": "m"}, "remaining arguments become the body"
    with pytest.raises(client.StudioError, match="needs the parameter 'repo_id'"):
        client.call("variants", {})
    with pytest.raises(client.StudioError, match="uploads a file"):
        client.call("upload", {})
    up = client.find_operation("upload")
    assert up["multipart"] and up["file_fields"] == [{"name": "file", "many": False}]
    out = client.call("upload", {"files": {"file": [("d.jsonl", b"{}\n")]}, "note": "x"})
    assert out == {"stored": ["d.jsonl"], "sizes": [3], "form": {"note": "x"}}
    with pytest.raises(client.StudioError, match="no operation"):
        client.call("nope")


# ---- loading, on demand, downloads, storage -----------------------------------------------------------------------------
def test_load_asks_for_an_agent_sized_context(studio, monkeypatch):
    harness.load("unsloth/m")
    assert studio.calls[-1][3]["max_seq_length"] == harness.DEFAULT_CONTEXT, "Studio's own default (2048) is too small for an agent"
    from bot.config import config

    monkeypatch.setattr(config, "_data", {**config._data, "unsloth": {"context_length": 8192}})
    harness.load("unsloth/m2", options={"gpu_layers": 20})
    assert studio.calls[-1][3]["max_seq_length"] == 8192 and studio.calls[-1][3]["gpu_layers"] == 20


def test_concurrent_on_demand_loads_share_one_load(studio):
    studio.load_delay = 0.3
    results = []
    threads = [threading.Thread(target=lambda: results.append(harness.ensure_loaded("unsloth/x-GGUF"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    loads = [c for c in studio.calls if c[1] == "/api/inference/load"]
    assert len(loads) == 1 and results.count(True) == 1
    assert loads[0][3]["gguf_variant"] == "Q4_K_M", "a GGUF repo loads the quant already on disk"
    assert harness.ensure_loaded("unsloth/x-GGUF") is False


def test_the_not_loaded_answer_is_recognised():
    assert harness.not_loaded_error("returned 400: No model loaded. Call POST /inference/load first.")
    assert not harness.not_loaded_error("returned 429: slow down")


def test_download_waits_until_studio_says_complete(studio, monkeypatch):
    monkeypatch.setattr(harness.time, "sleep", lambda _s: None)
    seen = []
    out = harness.download("unsloth/x-GGUF", variant="Q4_K_M", wait=True, on_progress=seen.append)
    assert out["done"] and [s["state"] for s in seen] == ["running", "running", "complete"]
    assert any(c[1] == "/api/hub/gguf-download-progress" and c[2].get("variant") == "Q4_K_M" for c in studio.calls)


def test_storage_is_read_and_set(studio, monkeypatch):
    assert harness.storage()["models_dir"] == r"E:\AIModels\Unsloth"
    assert harness.set_models_dir(r"E:\Other")["models_dir"] == r"E:\Other"
    from bot.config import config

    monkeypatch.setattr(config, "_data", {**config._data, "unsloth": {"models_dir": r"E:\AIModels\Unsloth"}})
    assert harness.ensure_models_dir()["models_dir"] == r"E:\AIModels\Unsloth" == studio.cache_home
    before = len(studio.calls)
    harness.ensure_models_dir()
    assert not any(c[0] == "PUT" for c in studio.calls[before:]), "already there: nothing is changed"


def test_training_and_export_check_what_they_need(studio):
    with pytest.raises(client.StudioError, match="format_type"):
        harness.train_start({"model_name": "m", "training_type": "lora"})
    assert harness.train_start({"model_name": "m", "training_type": "lora", "format_type": "chatml", "hf_dataset": "d"}) == {"job_id": "j1"}
    with pytest.raises(client.StudioError, match="export kind"):
        harness.export("onnx", {"save_directory": "x"})
    with pytest.raises(client.StudioError, match="save_directory"):
        harness.export("gguf", {})
    assert harness.export("gguf", {"save_directory": "E:/out"}) == {"output_path": "E:/out"}


def test_status_summary(studio):
    studio.loaded = ["unsloth/a"]
    s = harness.summary()
    assert s["running"] and s["loaded"] == ["unsloth/a"] and s["gpus"][0]["vram_total_gb"] == 24
    assert s["storage"]["models_dir"] == r"E:\AIModels\Unsloth"


# ---- a bot on an Unsloth model --------------------------------------------------------------------------------------------
def test_a_bot_on_an_unloaded_studio_model_gets_it_loaded_and_answered(studio, monkeypatch, tmp_path):
    from bot import bot_instances
    from bot.backends.custom_model_backend import CustomModelBackend

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
            if json["model"] not in studio.loaded:
                body = {"error": {"message": "No model loaded. Call POST /inference/load first."}}
                return httpx.Response(400, json=body, request=httpx.Request("POST", url))
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi from studio"}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", Client)
    iid = bot_instances.create_instance(name="studio-bot", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[])
    backend = CustomModelBackend(provider_name="unsloth", model_id="unsloth/qwen-GGUF", base_url=BASE + "/v1", api_key="unused")
    result = asyncio.run(backend.ask("hello", context={"cwd": str(tmp_path / "ws"), "instance_id": iid}))
    assert result.text == "hi from studio" and sent == ["unsloth/qwen-GGUF", "unsloth/qwen-GGUF"]
    assert studio.loaded == ["unsloth/qwen-GGUF"]


def test_the_router_offers_only_what_local_servers_really_have(studio, monkeypatch):
    from bot import model_router, providers

    providers.set_provider("my_ollama", base_url="http://127.0.0.1:1/v1")   # nothing listens there
    monkeypatch.setattr(model_router, "_cfg", lambda: {})
    model_router._local_cache.clear()
    studio.loaded = ["unsloth/hot"]
    cands = model_router.candidate_models()
    assert "unsloth/unsloth/hot" in cands and "unsloth/unsloth/cold" not in cands, "only a loaded Studio model: loading takes minutes"
    assert not any(c.startswith("my_ollama/") for c in cands), "a local server that is down offers nothing, not the catalog's guesses"


# ---- the agent's tools ------------------------------------------------------------------------------------------------------
def test_the_tools_are_offered_while_studio_runs_and_changes_ask_first(studio):
    from bot.agent_runtime import tools, toolspec

    names = {n for n in toolspec.registered_names() if n.startswith("unsloth")}
    assert names == {"unsloth_status", "unsloth_models", "unsloth_load", "unsloth_download", "unsloth_train", "unsloth_export",
                     "unsloth_operations", "unsloth_call"}
    for name in ("unsloth_load", "unsloth_download", "unsloth_train", "unsloth_export", "unsloth_call"):
        assert tools.is_dangerous(name), name
    assert not tools.is_dangerous("unsloth_status") and not tools.is_dangerous("unsloth_operations")
    out = asyncio.run(tools.execute_tool("unsloth_operations", {"operation": "variants", "call_read": True, "args": {"repo_id": "a/b"}},
                                         workspace=None, instance_id=1))
    assert "Q4_K_M" in out
    refused = asyncio.run(tools.execute_tool("unsloth_operations", {"operation": "load", "call_read": True}, workspace=None, instance_id=1))
    assert "use unsloth_call" in refused


def test_the_tools_are_hidden_when_studio_is_not_running(monkeypatch):
    from bot.unsloth import tools as us_tools

    monkeypatch.setattr(client, "find", lambda **_k: None)
    monkeypatch.setattr(client, "find_cached", lambda: None)
    assert us_tools._enabled() is False


# ---- the dashboard -----------------------------------------------------------------------------------------------------------
def test_the_api(studio, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/unsloth/status").status_code == 401
    s = c.get("/api/unsloth/status", headers=h).json()
    assert s["summary"]["running"] and s["storage"]["models_dir"] == r"E:\AIModels\Unsloth"
    assert c.post("/api/unsloth/load", headers=h, json={}).status_code == 400
    assert c.post("/api/unsloth/load", headers=h, json={"model": "unsloth/a", "context": 4096}).json()["context_length"] == 4096
    assert c.get("/api/unsloth/models", headers=h).json()["available"][0] == {"id": "unsloth/a", "name": "unsloth/a", "loaded": True,
                                                                              "context_length": None}
    assert c.post("/api/unsloth/unload", headers=h, json={"model": "unsloth/a"}).json()["unloaded"]
    assert c.get("/api/unsloth/variants?repo_id=x", headers=h).json()["variants"][1]["downloaded"]
    assert c.put("/api/unsloth/storage", headers=h, json={"models_dir": "E:/m"}).json()["models_dir"] == "E:/m"
    ops = c.get("/api/unsloth/operations", headers=h).json()
    assert ops["count"] == 5
    assert c.post("/api/unsloth/call", headers=h, json={"operation": "variants", "args": {"repo_id": "a"}}).json()["operation"] == "variants"
    assert c.post("/api/unsloth/call", headers=h, json={"operation": "nope"}).status_code in (400, 404, 502)
    assert c.get("/api/unsloth/train/schema", headers=h).status_code == 502, "this stand-in describes no training"


def test_the_api_says_when_studio_is_not_running(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setattr(client, "find", lambda **_k: None)
    monkeypatch.setattr(client, "find_cached", lambda: None)
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/unsloth/status", headers=h).json()["summary"]["running"] is False
    r = c.get("/api/unsloth/models", headers=h)
    assert r.status_code == 503 and "not running" in r.json()["detail"]


def test_the_page_is_identical_in_both_uis_and_mounted():
    import re

    dash = (ROOT / "bot/dashboard/static/unsloth-panel.js").read_text(encoding="utf-8")
    assert dash == (ROOT / "desktop-app/ui/unsloth-panel.js").read_text(encoding="utf-8")
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert '<section id="unsloth">' in text and 'id="us-root"' in text and 'href="#unsloth"' in text
        assert re.search(r'<script src="(/static/)?unsloth-panel\.js"></script>', text)
    called = set(re.findall(r"['`](/api/unsloth/[a-z/-]+)", dash))
    from bot.dashboard import unsloth_api

    src = Path(unsloth_api.__file__).read_text(encoding="utf-8")
    for path in called:
        assert f'"{path.rstrip("/")}' in src, path


def test_the_agent_uploads_only_files_from_its_workspace(studio, tmp_path):
    from bot.agent_runtime import tools

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    ok = asyncio.run(tools.execute_tool("unsloth_call", {"operation": "upload", "files": {"file": "data.jsonl"}}, workspace=str(ws), instance_id=1))
    assert '"d' in ok and "data.jsonl" in ok
    bad = asyncio.run(tools.execute_tool("unsloth_call", {"operation": "upload", "files": {"file": "../secret.txt"}}, workspace=str(ws), instance_id=1))
    assert "outside the workspace" in bad


def test_the_page_uploads_through_the_api(studio, monkeypatch):
    import base64

    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    body = {"operation": "upload", "files": {"file": [{"name": "x.jsonl", "data": base64.b64encode(b"abc").decode()}]}}
    assert c.post("/api/unsloth/upload", json=body).status_code == 401
    assert c.post("/api/unsloth/upload", headers=h, json=body).json()["result"]["sizes"] == [3]
    assert c.post("/api/unsloth/upload", headers=h, json={"operation": "upload", "files": {"file": [{"name": "x", "data": "!!"}]}}).status_code == 400
    assert c.post("/api/unsloth/upload", headers=h, json={"operation": "upload", "files": {}}).status_code == 400
