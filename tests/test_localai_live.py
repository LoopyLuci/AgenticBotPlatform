"""ABP's Ollama-compatible server (bot/localai/server.py) and engine (bot/localai/engine.py) with the real llama.cpp
engine and real models: Qwen2.5 0.5B Instruct (chat, generate, raw, fill-in-the-middle, tools, JSON, the OpenAI routes)
and nomic-embed-text (embeddings), loaded on this machine's GPU, plus the store routes (create, copy, delete, blobs).

It runs where the local AI home (bot/localai/paths.py) has an engine build and those two models (`abp ai engine
install`, `abp ai pull ...`); the store is read in place and the engines folder is linked, so a temporary home holds
everything the test writes."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REAL = Path(os.environ.get("ABP_LOCALAI_REAL_HOME", "E:/ABP-LocalAI"))
CHAT = "hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"
EMBED = "hf.co/nomic-ai/nomic-embed-text-v1.5-GGUF:Q8_0"


def _have() -> bool:
    m = REAL / "models" / "manifests" / "hf.co"
    return (any((REAL / "engines").glob("*/llama-server*")) and (m / "Qwen" / "Qwen2.5-0.5B-Instruct-GGUF" / "Q4_K_M").exists()
            and (m / "nomic-ai" / "nomic-embed-text-v1.5-GGUF" / "Q8_0").exists())


pytestmark = [pytest.mark.skipif(not _have(), reason="no llama.cpp engine and test models in the local AI home"),
              pytest.mark.xdist_group("localai_live")]


def _link_dir(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))      # no privilege needed, unlike a symlink
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    home = tmp_path_factory.mktemp("localai-live")
    mp = pytest.MonkeyPatch()
    mp.setenv("ABP_LOCALAI_HOME", str(home))
    from bot.localai import engine, models, server
    from bot.neurallab import systune
    systune._cache.clear()
    _link_dir(home / "engines", REAL / "engines")
    models.set_extra_stores([{"name": "Main", "path": str(REAL / "models")}])
    engine.set_settings({"keep_alive_s": 120, "parallel": 1, "default_ctx": 2048})
    try:
        with TestClient(server.build_app()) as c:
            yield c
    finally:
        engine.unload()
        os.rmdir(home / "engines") if sys.platform == "win32" else (home / "engines").unlink()   # the link, not the engines
        mp.undo()


def _ndjson(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_engine_is_found_and_described(live):
    from bot.localai import engine
    eng = engine.engine()
    assert Path(eng["server"]).exists() and eng["backend"] in ("hip", "vulkan", "cuda", "cpu", "metal")
    assert Path(engine.tool("llama-quantize")).exists()
    with pytest.raises(Exception):
        engine.tool("llama-no-such-program")
    assert engine.parse_keep_alive("5m") == 300 and engine.parse_keep_alive("1h") == 3600
    assert engine.parse_keep_alive(0) == 0 and engine.parse_keep_alive("250ms") == 0.25 and engine.parse_keep_alive("") is None
    with pytest.raises(Exception):
        engine.parse_keep_alive("soon")
    assert engine._asset_matches("llama-b11312-bin-win-vulkan-x64.zip", "vulkan") == (sys.platform == "win32")
    assert not engine._asset_matches("cudart-llama-bin-win-cuda-12.4-x64.zip", "cuda")
    assert engine._vendor("AMD Radeon RX 7900 XTX") == "amd" and engine._vendor("NVIDIA GeForce RTX 4090") == "nvidia"
    with pytest.raises(Exception):
        engine.set_settings({"no_such_setting": 1})


def test_load_unload_and_ps(live):
    r = live.post("/api/generate", json={"model": CHAT, "keep_alive": "2m"}).json()
    assert r["done_reason"] == "load" and r["done"]
    ps = live.get("/api/ps").json()["models"]
    assert [m["name"] for m in ps] == [CHAT] and ps[0]["context_length"] == 2048 and ps[0]["expires_at"]
    assert live.post("/api/chat", json={"model": CHAT, "keep_alive": 0}).json()["done_reason"] == "unload"
    assert live.get("/api/ps").json()["models"] == []
    assert live.post("/api/chat", json={"model": CHAT}).json()["done_reason"] == "load"


def test_chat_streamed_and_whole(live):
    msgs = [{"role": "user", "content": "What is the capital of France? Answer in one word."}]
    whole = live.post("/api/chat", json={"model": CHAT, "messages": msgs, "stream": False,
                                         "options": {"temperature": 0, "seed": 1, "num_predict": 16}}).json()
    assert "paris" in whole["message"]["content"].lower() and whole["done"] and whole["eval_count"] > 0
    chunks = _ndjson(live.post("/api/chat", json={"model": CHAT, "messages": msgs, "options": {"temperature": 0, "num_predict": 16}}))
    assert chunks[-1]["done"] and chunks[-1]["done_reason"] == "stop" and not any(c["done"] for c in chunks[:-1])
    assert "paris" in "".join(c["message"]["content"] for c in chunks).lower()


def test_chat_json_schema_and_tools(live):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    out = live.post("/api/chat", json={"model": CHAT, "stream": False, "format": schema, "options": {"temperature": 0, "num_predict": 40},
                                       "messages": [{"role": "user", "content": "Which city is the capital of Japan? Reply as JSON."}]}).json()
    assert "city" in json.loads(out["message"]["content"])
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city",
                                               "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    ask = [{"role": "user", "content": "What's the weather in Paris right now? Use the tool."}]
    whole = live.post("/api/chat", json={"model": CHAT, "stream": False, "tools": tools, "messages": ask, "options": {"temperature": 0}}).json()
    calls = whole["message"].get("tool_calls") or []
    assert calls and calls[0]["function"]["name"] == "get_weather" and isinstance(calls[0]["function"]["arguments"], dict)
    streamed = _ndjson(live.post("/api/chat", json={"model": CHAT, "tools": tools, "messages": ask, "options": {"temperature": 0}}))
    assert any(c.get("message", {}).get("tool_calls") for c in streamed)
    # the tool's answer goes back in, as Ollama clients send it
    follow = ask + [{"role": "assistant", "content": "", "tool_calls": calls},
                    {"role": "tool", "tool_name": "get_weather", "content": '{"temp_c": 21, "sky": "clear"}'}]
    again = live.post("/api/chat", json={"model": CHAT, "stream": False, "tools": tools, "messages": follow,
                                         "options": {"temperature": 0, "num_predict": 40}}).json()
    assert "21" in again["message"]["content"]
    bad = live.post("/api/chat", json={"model": CHAT, "messages": ask, "format": 5})
    assert bad.status_code == 400 and "format" in bad.json()["error"]


def test_generate_modes(live):
    g = live.post("/api/generate", json={"model": CHAT, "prompt": "Say just the word hello.", "system": "You answer in one word.",
                                         "stream": False, "think": False, "options": {"temperature": 0, "num_predict": 8}}).json()
    assert "hello" in g["response"].lower() and g["context"] == []
    s = _ndjson(live.post("/api/generate", json={"model": CHAT, "prompt": "Count: one, two,", "options": {"num_predict": 6}}))
    assert s[-1]["done"] and "".join(c["response"] for c in s).strip()
    raw = live.post("/api/generate", json={"model": CHAT, "prompt": "1, 2, 3, 4,", "raw": True, "stream": False,
                                           "options": {"temperature": 0, "num_predict": 4}}).json()
    assert "5" in raw["response"]
    raw_s = _ndjson(live.post("/api/generate", json={"model": CHAT, "prompt": "a b c", "raw": True, "options": {"num_predict": 3}}))
    assert len(raw_s) == 1 and raw_s[0]["done"]
    fim = live.post("/api/generate", json={"model": CHAT, "prompt": "def add(a, b):\n    return ", "suffix": "\n\nprint(add(1, 2))\n",
                                           "stream": False, "options": {"temperature": 0, "num_predict": 8}})
    assert fim.status_code in (200, 400) and ("response" in fim.json() or "engine" in fim.json()["error"])
    assert live.post("/api/generate", json={"model": "no-such-model:1", "prompt": "x"}).status_code == 404
    assert live.post("/api/generate", json={"model": CHAT, "keep_alive": "0"}).json()["done_reason"] == "unload"


def test_embeddings(live):
    e = live.post("/api/embed", json={"model": EMBED, "input": ["search_query: a cat", "search_query: a kitten", "search_query: tax law"]}).json()
    v = e["embeddings"]
    assert len(v) == 3 and abs(sum(x * x for x in v[0]) - 1) < 1e-3 and e["prompt_eval_count"] > 0
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert dot(v[0], v[1]) > dot(v[0], v[2])
    short = live.post("/api/embed", json={"model": EMBED, "input": "hello", "dimensions": 64}).json()["embeddings"][0]
    assert len(short) == 64 and abs(sum(x * x for x in short) - 1) < 1e-3
    assert live.post("/api/embed", json={"model": EMBED, "input": []}).status_code == 400
    legacy = live.post("/api/embeddings", json={"model": EMBED, "prompt": "hello"}).json()["embedding"]
    assert len(legacy) == 768
    oa = live.post("/v1/embeddings", json={"model": EMBED, "input": "hello"}).json()
    assert oa["model"] == EMBED and len(oa["data"][0]["embedding"]) == 768
    assert live.post("/api/embeddings", json={"model": "nope:1", "prompt": "x"}).status_code == 404


def test_openai_routes(live):
    msgs = [{"role": "user", "content": "Reply with the single word yes."}]
    j = live.post("/v1/chat/completions", json={"model": CHAT, "messages": msgs, "temperature": 0, "max_tokens": 4}).json()
    assert j["model"] == CHAT and "yes" in j["choices"][0]["message"]["content"].lower()
    sse = live.post("/v1/chat/completions", json={"model": CHAT, "messages": msgs, "stream": True, "max_tokens": 4}).text
    assert "data: " in sse and "[DONE]" in sse
    c = live.post("/v1/completions", json={"model": CHAT, "prompt": "1, 2, 3,", "max_tokens": 3, "temperature": 0}).json()
    assert c["choices"][0]["text"]
    missing = live.post("/v1/chat/completions", json={"model": "nope:1", "messages": msgs})
    assert missing.status_code == 404 and missing.json()["error"]["type"] == "invalid_request_error"
    assert CHAT in [m["id"] for m in live.get("/v1/models").json()["data"]]


def test_show_create_copy_delete(live):
    sh = live.post("/api/show", json={"model": CHAT}).json()
    assert sh["details"]["family"] == "qwen2" and "completion" in sh["capabilities"] and "tools" in sh["capabilities"]
    assert sh["modelfile"].startswith("# Modelfile") or "FROM" in sh["modelfile"]
    assert live.post("/api/show", json={"model": EMBED}).json()["capabilities"] == ["embedding"]
    made = _ndjson(live.post("/api/create", json={"model": "me/brief:v1", "from": CHAT, "system": "Answer in at most three words.",
                                                  "parameters": {"temperature": 0.2, "stop": ["<|im_end|>"]},
                                                  "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]}))
    assert made[-1].get("status") == "success", made
    brief = live.post("/api/show", json={"model": "me/brief:v1"}).json()
    assert brief["system"] == "Answer in at most three words." and "temperature" in brief["parameters"]
    ans = live.post("/api/chat", json={"model": "me/brief:v1", "stream": False, "options": {"num_predict": 20},
                                       "messages": [{"role": "user", "content": "Name a primary colour."}]}).json()
    assert len(ans["message"]["content"].split()) <= 8
    whole = live.post("/api/create", json={"model": "me/fromfile:v1", "modelfile": f"FROM {CHAT}\nPARAMETER top_k 20", "stream": False})
    assert whole.status_code == 200 and whole.json()["status"] == "success"
    assert live.post("/api/create", json={"model": "me/none:v1", "stream": False}).status_code == 400
    assert live.post("/api/copy", json={"source": "me/brief:v1", "destination": "me/brief2:v1"}).status_code == 200
    assert live.post("/api/copy", json={"source": "nope:1", "destination": "me/x:1"}).status_code == 404
    names = {m["name"] for m in live.get("/api/tags").json()["models"]}
    assert {"me/brief:v1", "me/brief2:v1", "me/fromfile:v1", CHAT, EMBED} <= names
    for n in ("me/brief:v1", "me/brief2:v1", "me/fromfile:v1"):
        assert live.request("DELETE", "/api/delete", json={"model": n}).status_code == 200
    assert live.request("DELETE", "/api/delete", json={"model": "me/brief:v1"}).status_code == 404


def test_blobs_and_create_from_files(live, tmp_path):
    data = (tmp_path / "x.bin")
    data.write_bytes(b"ABP blob test " * 1000)
    digest = "sha256:" + hashlib.sha256(data.read_bytes()).hexdigest()
    assert live.head(f"/api/blobs/{digest}").status_code == 404
    assert live.post(f"/api/blobs/{digest}", content=data.read_bytes()).status_code == 201
    assert live.head(f"/api/blobs/{digest}").status_code == 200
    wrong = "sha256:" + "0" * 64
    assert live.post(f"/api/blobs/{wrong}", content=b"something else").json()["error"] == "digest mismatch"
    assert live.head("/api/blobs/not-a-digest").status_code == 400
    # a GGUF uploaded as a blob, then made a model from `files` (what `ollama create` does with a local file)
    from bot.localai import models
    weights = Path(models.resolve(EMBED)["weights"])
    gd = "sha256:" + models.sha256_file(weights).split(":", 1)[-1]
    with open(weights, "rb") as f:
        assert live.post(f"/api/blobs/{gd}", content=iter(lambda: f.read(1 << 20), b"")).status_code == 201
    made = live.post("/api/create", json={"model": "me/embed-upload:v1", "files": {"model.gguf": gd}, "stream": False}).json()
    assert made["status"] == "success", made
    assert len(live.post("/api/embed", json={"model": "me/embed-upload:v1", "input": "x"}).json()["embeddings"][0]) == 768
    assert live.request("DELETE", "/api/delete", json={"model": "me/embed-upload:v1"}).status_code == 200


def test_pull_reports_errors_and_push_is_not_offered(live):
    r = live.post("/api/pull", json={"model": "hf.co/abp-test-does-not-exist/none-GGUF:Q4_K_M", "stream": False})
    assert r.status_code == 500 and r.json()["error"]
    streamed = _ndjson(live.post("/api/pull", json={"model": "hf.co/abp-test-does-not-exist/none-GGUF:Q4_K_M"}))
    assert "error" in streamed[-1]
    assert live.post("/api/push", json={"model": CHAT}).status_code == 501


def test_reaper_and_limits(live):
    from bot.localai import engine
    live.post("/api/generate", json={"model": CHAT, "keep_alive": 0.5})
    live.post("/api/embed", json={"model": EMBED, "input": "x", "keep_alive": -1})
    import time
    time.sleep(1)
    engine.reap()                                          # the chat model's keep_alive passed; the embedding model never expires
    assert [m["name"] for m in engine.running()] == [EMBED]
    import datetime as dt
    assert dt.datetime.fromisoformat(engine.running()[0]["expires_at"]).year >= 2099      # never (local time)
    assert engine.unload() == 1 and engine.running() == []


def test_the_server_process_and_overview(live):
    """ABP's background task keeps the model server running as a process of its own; pages read its overview."""
    import socket
    from bot.localai import engine, service
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    engine.set_settings({"port": port, "autostart": True})
    assert service.export_store_env() == os.environ["ABP_MODELS_DIR"]
    assert not service.server_status()["running"] and service._running() == []
    service._last.clear()
    try:
        service.tick()                                    # autostart: the server was not running
        import httpx
        import time
        for _ in range(100):
            st = service.server_status()
            if st.get("version"):
                break
            time.sleep(0.2)
        assert st["running"] and st["version"] and st["url"].endswith(str(port))
        assert httpx.post(f"{st['url']}/api/generate", json={"model": CHAT}, timeout=120).json()["done_reason"] == "load"
        ov = service.overview()
        assert [m["name"] for m in ov["running"]] == [CHAT] and ov["engine"] and ov["gpus"] is not None
        assert any(m["name"] == CHAT for m in ov["models"]) and ov["train_env"]["path"]
    finally:
        assert service.server_stop()
    assert not service.server_status()["running"]


def test_the_lab_recorder_and_overview(live):
    from bot.localai import engine
    from bot.neurallab import service, telemetry
    engine.set_settings({"lab_telemetry_interval_s": 0.2, "lab_telemetry": True})
    n0 = telemetry.stats()["samples"]
    service.start_recorder()
    try:
        import time
        for _ in range(50):
            if telemetry.stats()["samples"] >= n0 + 2:
                break
            time.sleep(0.2)
        ov = service.overview()
        assert ov["recording"] and ov["settings"]["telemetry_interval_s"] == 0.2 and ov["ops"]
        assert telemetry.stats()["samples"] >= n0 + 2
    finally:
        service._recorder.stop()
        service._recorder = None
