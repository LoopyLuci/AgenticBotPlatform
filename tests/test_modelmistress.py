"""ModelMistress from ABP: finding the hub (env, config, the module's control file, a linked peer), its operations and OpenAI
routes, the agent's tools, the dashboard API, the preference over Ollama / Unsloth, the cluster's inference job and the
fallbacks when no hub runs. Everything runs against a stand-in hub (a small FastAPI app on a free port, answering per
ModelMistress's docs: owner token for every route, a peer key for the OpenAI routes only); one test starts the real
binary when a build exists on this machine."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bot.modelmistress import client, harness
from bot.modelmistress.client import MMError

OWNER = "owner-token-" + "o" * 20
PEER_KEY = "mmk_" + "p" * 24


class StandInHub:
    """The routes ABP uses, answering the way ModelMistress's docs say."""

    def __init__(self):
        self.calls: list[tuple[str, str, object, str]] = []
        self.models = {"llama3.2:1b": {"source": "ollama", "size_gb": 1.3}, "qwen3:8b": {"source": "huggingface", "size_gb": 5.0}}
        self.loaded: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.chat_status = 200
        self.open_chat = False          # an Ollama needs no key
        app = FastAPI()
        self.app = app

        def who(request: Request) -> str:
            h = request.headers.get("authorization", "")
            if h == f"Bearer {OWNER}":
                return "owner"
            if h == f"Bearer {PEER_KEY}":
                return "peer"
            return ""

        @app.get("/v1/health")
        def health():
            return {"ok": True, "pid": os.getpid(), "version": "0.1.0", "uptime_s": 1}

        @app.get("/v1/models")
        def v1_models(request: Request):
            if not who(request):
                return JSONResponse({"error": {"code": "unauthorized", "message": "token"}}, 401)
            self.calls.append(("GET", "/v1/models", None, who(request)))
            return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": v["source"]} for m, v in self.models.items()]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            w = who(request)
            if not w and not self.open_chat:
                return JSONResponse({"error": {"code": "unauthorized", "message": "token"}}, 401)
            body = await request.json()
            self.calls.append(("POST", "/v1/chat/completions", body, w))
            if self.chat_status != 200:
                return JSONResponse({"error": {"code": "backend_error", "message": "no such model"}}, self.chat_status)
            return {"id": "c1", "object": "chat.completion", "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi from the hub"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}

        @app.post("/v1/call/{op}")
        async def call(op: str, request: Request):
            w = who(request)
            if w != "owner":
                code = 401 if not w else 403
                return JSONResponse({"error": {"code": "unauthorized" if code == 401 else "forbidden", "message": "owner only"}}, code)
            body = await request.json()
            self.calls.append(("POST", f"/v1/call/{op}", body, w))
            if op == "service.status":
                return {"result": {"version": "0.1.0", "uptime_s": 5, "loaded": list(self.loaded)}}
            if op == "backend.list":
                return {"result": [{"id": "llama.cpp", "available": True}, {"id": "ollama", "available": False}]}
            if op == "model.list":
                return {"result": [{"id": m, "source": v["source"], "loaded": m in self.loaded} for m, v in self.models.items()]}
            if op == "model.loaded":
                return {"result": list(self.loaded.values())}
            if op == "model.load":
                if body["model"] not in self.models:
                    return JSONResponse({"error": {"code": "not_found", "message": f"no model {body['model']}"}}, 404)
                self.loaded[body["model"]] = {"model": body["model"], "ctx_size": body.get("ctx_size", 4096)}
                return {"result": {"loaded": self.loaded[body["model"]], "load_ms": 12}}
            if op == "model.unload":
                n = 1 if self.loaded.pop(body["model"], None) else 0
                if body["model"] == "*":
                    n, _ = len(self.loaded), self.loaded.clear()
                return {"result": {"unloaded": n, "model": body["model"]}}
            if op == "model.pull":
                job = {"job": f"j{len(self.jobs) + 1}", "model": body["model"], "status": "running", "percent": 0.0}
                self.jobs[job["job"]] = job
                return {"result": job}
            if op == "job.list":
                return {"result": list(self.jobs.values())}
            if op == "job.get":
                return {"result": self.jobs[body["job"]]}
            return JSONResponse({"error": {"code": "not_found", "message": f"no operation {op}"}}, 404)

        port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        end = time.time() + 10
        while time.time() < end:
            try:
                if httpx.get(self.url + "/v1/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("the stand-in hub did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for name in ("ABP_MODELMISTRESS_URL", "ABP_MODELMISTRESS_TOKEN", "ABP_NO_MODELMISTRESS"):
        monkeypatch.delenv(name, raising=False)
    client.forget()
    yield
    client.forget()


@pytest.fixture
def hub(monkeypatch):
    h = StandInHub()
    monkeypatch.setenv("ABP_MODELMISTRESS_URL", h.url)
    monkeypatch.setenv("ABP_MODELMISTRESS_TOKEN", OWNER)
    yield h
    h.stop()


def _config(monkeypatch, value):
    monkeypatch.setattr(client, "_cfg", lambda: value)


# ---- finding the hub ---------------------------------------------------------------------------------------------------------------
def test_no_hub_means_none_and_a_clear_error():
    assert client.find() is None
    with pytest.raises(MMError) as e:
        harness.models()
    assert e.value.code == "unavailable" and "not running" in str(e.value)
    assert not client.provider_running()


def test_found_from_the_environment(hub):
    found = client.find()
    assert found.url == hub.url and found.token == OWNER and found.version == "0.1.0" and not found.remote


def test_a_dead_url_is_no_hub(monkeypatch):
    monkeypatch.setenv("ABP_MODELMISTRESS_URL", f"http://127.0.0.1:{_free_port()}")
    assert client.find() is None


def test_found_from_the_config_with_the_token_from_an_env_variable(hub, monkeypatch):
    monkeypatch.delenv("ABP_MODELMISTRESS_URL")
    monkeypatch.setenv("MM_TEST_TOKEN", OWNER)
    _config(monkeypatch, {"url": hub.url, "token_env": "MM_TEST_TOKEN"})
    assert client.find().token == OWNER
    assert harness.models()[0]["id"] == "llama3.2:1b"


def test_found_from_the_module_control_file(hub, tmp_path, monkeypatch):
    from bot.modules import registry

    monkeypatch.delenv("ABP_MODELMISTRESS_URL")
    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / m.id)
    (tmp_path / "modelmistress").mkdir()
    (tmp_path / "modelmistress" / "control.json").write_text(
        json.dumps({"url": hub.url, "token": OWNER, "pid": os.getpid(), "version": "0.1.0", "api": 1}), encoding="utf-8")
    assert client.find().token == OWNER
    assert harness.status()["service"]["version"] == "0.1.0"


def test_a_machine_without_the_module_hub_is_not_an_error(monkeypatch, tmp_path):
    from bot.modules import registry

    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / m.id)
    assert client.find() is None


# ---- operations --------------------------------------------------------------------------------------------------------------------
def test_status_models_load_unload_pull(hub):
    st = harness.status()
    assert st["service"]["version"] == "0.1.0" and st["backends"][0]["id"] == "llama.cpp" and st["loaded"] == []
    assert [m["id"] for m in harness.models()] == ["llama3.2:1b", "qwen3:8b"]
    assert harness.load("llama3.2:1b", ctx_size=8192, gpu_layers=None)["loaded"]["ctx_size"] == 8192
    sent = [c for c in hub.calls if c[1] == "/v1/call/model.load"][0]
    assert sent[2] == {"model": "llama3.2:1b", "ctx_size": 8192} and sent[3] == "owner"      # None options are left out
    assert harness.models()[0]["loaded"] is True
    assert harness.unload("llama3.2:1b")["unloaded"] == 1
    job = harness.pull("hf.co/org/repo:Q4_K_M")
    assert job["status"] == "running" and harness.jobs()[0]["model"] == "hf.co/org/repo:Q4_K_M"
    assert harness.status()["jobs"][0]["job"] == job["job"]
    assert harness.job(job["job"])["job"] == job["job"]


def test_the_hubs_errors_come_through(hub):
    with pytest.raises(MMError) as e:
        harness.load("nope")
    assert e.value.status == 404 and e.value.code == "not_found" and "no model nope" in str(e.value)


def test_chat_goes_through_the_openai_route(hub):
    out = client.chat("llama3.2:1b", [{"role": "user", "content": "hi"}], max_tokens=5, temperature=None)
    assert out["choices"][0]["message"]["content"] == "hi from the hub"
    sent = [c for c in hub.calls if c[1] == "/v1/chat/completions"][0]
    assert sent[2]["max_tokens"] == 5 and "temperature" not in sent[2] and sent[2]["stream"] is False


# ---- a linked peer's hub -----------------------------------------------------------------------------------------------------------
def test_a_peers_hub_is_reached_with_its_peer_key(hub, monkeypatch):
    monkeypatch.setenv("MM_BOB_KEY", PEER_KEY)
    _config(monkeypatch, {"peers": {"bob": {"url": hub.url, "key_env": "MM_BOB_KEY"}}})
    bob = client.find("bob")
    assert bob.remote and bob.peer == "bob" and bob.token == PEER_KEY
    assert [m["id"] for m in harness.models("bob")] == ["llama3.2:1b", "qwen3:8b"]
    assert hub.calls[-1][3] == "peer"
    st = harness.status("bob")
    assert st["peer"] == "bob" and st["models"] == ["llama3.2:1b", "qwen3:8b"]
    with pytest.raises(MMError) as e:                 # a peer key does not run operations
        client.call("model.load", {"model": "qwen3:8b"}, hub=bob)
    assert e.value.status == 403
    assert client.chat("qwen3:8b", [{"role": "user", "content": "x"}], hub=bob)["model"] == "qwen3:8b"
    assert client.find("alice") is None


# ---- the agent's tools and the dashboard -------------------------------------------------------------------------------------------
def test_the_tools(hub):
    from bot.agent_runtime import tools, toolspec
    from bot.modelmistress import tools as mm_tools

    mm_tools.register_tools()
    client.find(refresh=True)
    names = {n for n in toolspec.registered_names() if n.startswith("modelmistress")}
    assert names == {"modelmistress_status", "modelmistress_models", "modelmistress_load", "modelmistress_pull"}
    for name in ("modelmistress_pull", "modelmistress_load"):
        assert tools.is_dangerous(name), name
    assert not tools.is_dangerous("modelmistress_status") and not tools.is_dangerous("modelmistress_models")

    def run(name, inp):
        return asyncio.run(tools.execute_tool(name, inp, workspace=None, instance_id=1))
    assert "llama3.2:1b" in run("modelmistress_models", {})
    assert '"version": "0.1.0"' in run("modelmistress_status", {})
    assert "load_ms" in run("modelmistress_load", {"model": "qwen3:8b", "context": 2048})
    assert hub.loaded["qwen3:8b"]["ctx_size"] == 2048
    assert '"unloaded": 1' in run("modelmistress_load", {"model": "qwen3:8b", "unload": True})
    assert '"status": "running"' in run("modelmistress_pull", {"model": "gemma3:1b"})
    assert run("modelmistress_pull", {}).startswith("Error")
    assert run("modelmistress_load", {"model": "nope"}).startswith("Error: no model nope")


def test_the_api(hub, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/modelmistress/status").status_code == 401
    assert c.get("/api/modelmistress/status", headers=h).json()["service"]["version"] == "0.1.0"
    assert c.get("/api/modelmistress/models", headers=h).json()["models"][0]["id"] == "llama3.2:1b"
    assert c.post("/api/modelmistress/load", json={"model": "qwen3:8b"}).status_code == 401
    assert c.post("/api/modelmistress/load", headers=h, json={"model": "qwen3:8b", "ctx_size": 1024}).json()["load_ms"] == 12
    assert c.post("/api/modelmistress/load", headers=h, json={"model": "nope"}).status_code == 404
    assert c.post("/api/modelmistress/load", headers=h, json={}).status_code == 400
    assert c.post("/api/modelmistress/unload", headers=h, json={"model": "qwen3:8b"}).json()["unloaded"] == 1
    assert c.post("/api/modelmistress/pull", headers=h, json={"model": "gemma3:1b"}).json()["status"] == "running"
    assert c.get("/api/modelmistress/jobs", headers=h).json()["jobs"][0]["model"] == "gemma3:1b"


def test_the_api_says_when_no_hub_runs(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    r = TestClient(build_app()).get("/api/modelmistress/models", headers={"X-Dashboard-Token": "t" * 48})
    assert r.status_code == 503 and "not running" in r.json()["detail"]


# ---- the preference over Ollama and Unsloth, and the fallback -----------------------------------------------------------------------
PROVIDERS = {"ollama": {"base_url": "http://127.0.0.1:11434/v1"}, "studio": {"base_url": "http://127.0.0.1:8888/v1"},
             "unsloth-studio": {"base_url": "http://127.0.0.1:9/v1"}, "lmstudio": {"base_url": "http://127.0.0.1:1234/v1"},
             "modelmistress": {"base_url": "http://127.0.0.1:7/v1"}}


def _router(monkeypatch, providers):
    from bot import model_router
    from bot import providers as registry

    monkeypatch.setattr(model_router, "_cfg", lambda: {})
    monkeypatch.setattr(registry, "list_providers", lambda: dict(providers))
    monkeypatch.setattr(model_router, "_local_models", lambda name, pcfg: [f"{name}-model"])
    return model_router.candidate_models()


def test_candidates_prefer_modelmistress_over_ollama_and_unsloth(monkeypatch):
    found = _router(monkeypatch, PROVIDERS)
    assert found[0] == "modelmistress/modelmistress-model"
    assert "ollama/ollama-model" not in found and "studio/studio-model" not in found and "unsloth-studio/unsloth-studio-model" not in found
    assert "lmstudio/lmstudio-model" in found                       # other local servers are not replaced


def test_candidates_keep_ollama_without_modelmistress(monkeypatch):
    without = {k: v for k, v in PROVIDERS.items() if k != "modelmistress"}
    assert "ollama/ollama-model" in _router(monkeypatch, without)


def test_prefer_false_keeps_ollama_while_modelmistress_runs(monkeypatch):
    _config(monkeypatch, {"prefer": False})
    found = _router(monkeypatch, PROVIDERS)
    assert "ollama/ollama-model" in found and "modelmistress/modelmistress-model" in found


def test_localai_does_not_start_a_second_server_while_modelmistress_serves(hub, monkeypatch):
    from bot.localai import engine, service

    started = []
    monkeypatch.setattr(engine, "settings", lambda: {"autostart": True})
    monkeypatch.setattr(engine, "installed", lambda: ["llama.cpp"])
    monkeypatch.setattr(service, "server_status", lambda: {"running": False})
    monkeypatch.setattr(service, "server_start", lambda: started.append(1))
    monkeypatch.setattr(service.train, "tick", lambda log=None: [])
    client.find(refresh=True)
    service.tick()
    assert started == [] and service.modelmistress_serving()["url"] == hub.url
    monkeypatch.setenv("ABP_NO_MODELMISTRESS", "1")
    client.forget()
    service._last.clear()
    service.tick()
    assert started == [1]


# ---- the cluster's inference job ---------------------------------------------------------------------------------------------------
def _job(**spec):
    return {"id": "inf1", "timeout_s": 30, "spec": {"model": "llama3.2:1b", "messages": [{"role": "user", "content": "hi"}], **spec}}


@pytest.fixture
def cluster(monkeypatch):
    from bot.cluster import executor

    logs = []
    monkeypatch.setattr(executor, "_log", lambda jid, line: logs.append(line))
    return executor, logs


def test_inference_runs_on_modelmistress(hub, cluster):
    executor, logs = cluster
    out = executor._inference(_job(max_tokens=9))
    assert out["choices"][0]["message"]["content"] == "hi from the hub"
    assert hub.calls[-1][2]["max_tokens"] == 9 and hub.calls[-1][3] == "owner"
    assert "through ModelMistress" in logs[0]


def test_inference_falls_back_to_ollama_when_no_hub_answers(cluster, monkeypatch):
    executor, logs = cluster
    ollama = StandInHub()
    ollama.open_chat = True
    try:
        monkeypatch.setattr(executor, "OLLAMA_CHAT", ollama.url + "/v1/chat/completions")
        out = executor._inference(_job())
        assert out["choices"][0]["message"]["content"] == "hi from the hub"
        assert "through Ollama" in logs[0] and ollama.calls[-1][3] == ""
    finally:
        ollama.stop()


def test_inference_falls_back_to_ollama_when_the_hub_is_down(cluster, monkeypatch):
    executor, logs = cluster
    ollama = StandInHub()
    ollama.open_chat = True
    gone = StandInHub()
    try:
        monkeypatch.setenv("ABP_MODELMISTRESS_URL", gone.url)
        monkeypatch.setenv("ABP_MODELMISTRESS_TOKEN", OWNER)
        assert client.find() is not None
        gone.stop()                                                  # found a moment ago, dead now
        monkeypatch.setattr(executor, "OLLAMA_CHAT", ollama.url + "/v1/chat/completions")
        out = executor._inference(_job())
        assert out["model"] == "llama3.2:1b" and "falling back to Ollama" in " ".join(logs)
        assert ollama.calls[-1][3] == ""
    finally:
        ollama.stop()


def test_a_hub_that_answers_with_an_error_is_not_retried_on_ollama(hub, cluster, monkeypatch):
    executor, logs = cluster
    hub.chat_status = 404
    monkeypatch.setattr(executor, "OLLAMA_CHAT", "http://127.0.0.1:1/never")
    with pytest.raises(RuntimeError) as e:
        executor._inference(_job())
    assert "ModelMistress answered 404" in str(e.value) and "no such model" in str(e.value)


def test_inference_on_a_peers_hub_uses_its_peer_key(hub, cluster, monkeypatch):
    executor, _logs = cluster
    monkeypatch.delenv("ABP_MODELMISTRESS_URL")
    monkeypatch.setenv("MM_BOB_KEY", PEER_KEY)
    _config(monkeypatch, {"peers": {"bob": {"url": hub.url, "key_env": "MM_BOB_KEY"}}})
    out = executor._inference(_job(hub="bob"))
    assert out["model"] == "llama3.2:1b" and hub.calls[-1][3] == "peer"
    with pytest.raises(RuntimeError) as e:
        executor._inference(_job(hub="alice"))
    assert "no ModelMistress hub answers for 'alice'" in str(e.value)


def test_inference_job_validation_is_unchanged():
    from bot.cluster import executor

    with pytest.raises(executor.JobError):
        executor._validate({"id": "x1", "kind": "inference", "spec": {"model": "m"}})
    assert executor._validate({"id": "x1", "kind": "inference", "spec": {"model": "m", "messages": []}})["kind"] == "inference"


# ---- the real binary, when one is built here ---------------------------------------------------------------------------------------
_bin = os.environ.get("ABP_TEST_MODELMISTRESS_BIN", "")
BINARY = Path(_bin) if _bin and Path(_bin).is_file() else None


@pytest.mark.skipif(BINARY is None, reason="ABP_TEST_MODELMISTRESS_BIN names no ModelMistress build")
def test_against_the_real_binary(tmp_path, monkeypatch):
    home = tmp_path / "mm-home"
    home.mkdir()
    env = {**os.environ, "MM_HOME": str(home)}
    proc = subprocess.Popen([str(BINARY), "--home", str(home), "serve", "--port", "0"], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=str(tmp_path))
    try:
        control = home / "control.json"
        end = time.time() + 40
        while time.time() < end and not control.is_file() and proc.poll() is None:
            time.sleep(0.2)
        if not control.is_file():
            pytest.skip("the ModelMistress binary did not start a hub here")
        d = json.loads(control.read_text(encoding="utf-8"))
        monkeypatch.setenv("ABP_MODELMISTRESS_URL", d["url"])
        monkeypatch.setenv("ABP_MODELMISTRESS_TOKEN", d["token"])
        found = client.find(refresh=True)
        assert found is not None and found.version
        st = harness.status()
        assert st["service"] and isinstance(st["backends"], list)
        assert isinstance(harness.models(), list)
        assert isinstance(harness.jobs(), list)
        assert isinstance(client.models(), list)
        with pytest.raises(MMError) as e:
            harness.load("no-such-model-abp-test")
        assert e.value.status in (400, 404)
        with pytest.raises(MMError) as e:
            client.request("GET", "/v1/models", hub=client.Hub(found.url, "wrong-token"))
        assert e.value.status == 401
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
        shutil.rmtree(home, ignore_errors=True)
