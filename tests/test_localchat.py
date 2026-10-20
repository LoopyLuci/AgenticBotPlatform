"""Local Chat backend (bot/localchat, /api/local-chat): sessions, the model picker's catalog, measured/estimated speed, turns
on the native agent with a per-session model, cancel, recovery - and the two fixes it needed (the "modelmistress" provider found
in the MM home; router.ask's per-turn model)."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from bot.backends.base import BackendResult
from bot.localchat import catalog, chat, speed, store

CODER = "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL"
SMALL = "unsloth/Qwen3.5-9B-MTP-GGUF:Qwen3.5-9B-Q4_K_M"
EMBED = "Qwen/Qwen3-Embedding-0.6B-GGUF:Qwen3-Embedding-0.6B-Q8_0"
HUGE = "someone/Huge-405B-GGUF:Huge-405B-Q4_K_M"
DEVICE = "Vulkan0: AMD Radeon RX 7900 XTX"


@pytest.fixture(autouse=True)
def fresh():
    catalog.forget()
    yield
    catalog.forget()


# ---- sessions ------------------------------------------------------------------------------------------------------------------
def test_sessions_keep_their_choices_and_validate_them():
    s = store.create(model=CODER, mode="review", autonomy=1, workspace="/ws/repo")
    assert (s["model"], s["mode"], s["autonomy"], s["workspace"], s["cloud_assist"]) == (CODER, "review", 1, "/ws/repo", "ask")
    with pytest.raises(ValueError):
        store.update(s["id"], mode="yolo")
    with pytest.raises(ValueError):
        store.update(s["id"], autonomy=9)
    with pytest.raises(ValueError):
        store.update(s["id"], created=0)
    assert store.update(s["id"], pinned=True, mode="plan")["mode"] == "plan"


def test_the_first_message_names_the_session_and_search_finds_it():
    a, b = store.create(), store.create()
    store.add_message(a["id"], "user", "  Refactor   the   capacity governor please ")
    store.add_message(b["id"], "user", "hello")
    store.update(b["id"], pinned=True)
    assert store.get(a["id"])["title"] == "Refactor the capacity governor please"
    assert [s["id"] for s in store.list_sessions()][0] == b["id"]                 # pinned first
    assert [s["id"] for s in store.list_sessions(query="governor")] == [a["id"]]
    assert store.delete(a["id"]) and store.get(a["id"]) is None and store.messages(a["id"]) == []


def test_a_turn_left_running_by_a_dead_process_is_marked_interrupted():
    s = store.create()
    store.start_turn(s["id"])
    assert store.recover() == 1
    assert store.running_turns() == []
    assert "interrupted" in store.messages(s["id"])[-1]["text"]


# ---- speed -----------------------------------------------------------------------------------------------------------------------
def test_active_fraction_for_dense_and_mixture_of_experts():
    assert speed.active_fraction({"size_label": "30B-A3B"}) == pytest.approx(0.1)
    assert speed.active_fraction({"experts": 128, "experts_used": 8}) == pytest.approx(1.5 * 8 / 128)
    assert speed.active_fraction({"size_label": "9B"}) == 1.0


def test_speed_is_measured_then_calibrates_estimates_for_other_models():
    coder = {"id": CODER, "size_gb": 17.7, "size_label": "30B-A3B"}
    small = {"id": SMALL, "size_gb": 5.6, "size_label": "9B"}
    rough = speed.figure(small, DEVICE, "gpu")
    assert rough["source"] == "rough_estimate" and rough["tokens_per_s"] > 0
    speed.record(CODER, DEVICE, 34.0, "reply", size_gb=17.7, active_fraction=0.1)
    m = speed.figure(coder, DEVICE, "gpu")
    assert m == {**m, "source": "measured", "tokens_per_s": 34.0, "samples": 1}
    est = speed.figure(small, DEVICE, "gpu")
    assert est["source"] == "estimated" and est["tokens_per_s"] == pytest.approx(34.0 * 1.77 / 5.6, rel=0.01)
    speed.record(CODER, DEVICE, 44.0, "reply")
    assert speed.figure(coder, DEVICE, "gpu")["tokens_per_s"] == pytest.approx(0.7 * 34 + 0.3 * 44)
    assert speed.figure({"id": "x"}, DEVICE, "gpu")["source"] == "unknown"
    speed.record(CODER, DEVICE, -1)                                            # nonsense is ignored
    assert speed.measured(CODER, DEVICE)["samples"] == 2


# ---- catalog -------------------------------------------------------------------------------------------------------------------
def _hub(loaded=(CODER,), per_slot=32768):
    listed = [
        {"id": CODER, "name": "Qwen3 Coder", "architecture": "qwen3moe", "size_label": "30B-A3B", "size_gb": 17.7,
         "context_length": 262144, "experts": 128, "experts_used": 8, "vision": False, "source": "huggingface"},
        {"id": SMALL, "name": "Qwen3.5 9B", "architecture": "qwen35", "size_label": "9B", "size_gb": 5.6, "context_length": 32768},
        {"id": EMBED, "name": "Qwen3 Embedding", "architecture": "qwen3", "size_label": "0.6B", "size_gb": 0.64},
        {"id": HUGE, "name": "Huge", "size_label": "405B", "size_gb": 230.0},
        {"id": "ollama/qwen3:1.7b", "name": "qwen3:1.7b"},
    ]
    plans = {CODER: "fits", SMALL: "fits_after_unloading", EMBED: "fits", HUGE: "does_not_fit"}
    calls = []

    def call(op, args=None):
        calls.append(op)
        if op == "service.status":
            return {"version": "0.3.0", "llama_server": "/opt/mm/llama-server",
                    "llama_built_in": "7fee178 (vulkan)", "system": {"available_mb": 32 * 1024}}
        if op == "model.list":
            return listed
        if op == "model.loaded":
            return [{"model": m} for m in loaded]
        if op == "model.plan":
            v = plans[args["model"]]
            return {"verdict": v, "devices": [{"name": DEVICE, "free_mib": 21544}], "free_mib": 21544,
                    "estimate": {"total_mib": 20419}, "serving": {"n_ctx_per_slot": per_slot, "slots": 1},
                    "unload": [CODER] if v == "fits_after_unloading" else []}
        raise AssertionError(op)
    return call, calls


REPORTS = [{"kind": "factory", "slug": "qwen3-coder-30b-a3b", "variant": "", "score": 0.55, "passed": 11, "total": 20,
            "when": "2026-10-13T10:00:00", "file": "2026-10-13-factory-qwen3-coder-30b-a3b.json"},
           {"kind": "suite", "slug": "qwen3.5-9b", "variant": "", "score": 0.52, "passed": 16, "total": 31,
            "when": "2026-10-13", "file": "2026-10-13-suite-qwen3.5-9b.json"}]


def test_the_catalog_labels_every_figure_and_groups_the_models():
    call, calls = _hub()
    doc = catalog.build(call, reports=REPORTS, min_context=16384)
    rows = {r["id"]: r for r in doc["models"]}
    assert doc["device"] == DEVICE and doc["hub"]["engine"] is True
    assert rows[CODER]["group"] == "loaded" and rows[CODER]["runs_on"] == "gpu"
    assert rows[SMALL]["group"] == "gpu" and any("unloads" in w for w in rows[SMALL]["warnings"])
    assert rows[HUGE]["group"] == "too_big" and rows[HUGE]["runs_on"] == "none"
    assert rows["ollama/qwen3:1.7b"]["group"] == "other" and "model.plan" in calls
    assert rows[EMBED]["kind"] == "embedding" and rows[EMBED]["strengths"][0]["text"].startswith("embeddings")
    coder = rows[CODER]
    assert coder["metadata"]["quant"] == "UD-Q4_K_XL" and coder["metadata"]["experts"] == 128
    texts = {s["text"]: s["source"] for s in coder["strengths"]}
    assert texts["coding (a code-tuned model)"] == "metadata"
    assert any(src == "measured" and "11/20" in t for t, src in texts.items())
    assert coder["evals"][0]["file"].endswith("qwen3-coder-30b-a3b.json")
    assert rows[SMALL]["evals"][0]["kind"] == "suite"
    assert coder["speed"]["source"] in ("rough_estimate", "estimated", "measured")
    assert doc["groups"] == ["loaded", "gpu", "too_big", "other"]
    assert catalog.default_model(doc) == CODER                       # the loaded chat model


def test_a_model_served_with_too_little_context_is_flagged():
    call, _ = _hub(per_slot=4096)                                    # the live hub on 2026-10-19 before its fix
    rows = {r["id"]: r for r in catalog.build(call, reports=[], min_context=16384)["models"]}
    assert any("4096 tokens per request" in w for w in rows[CODER]["warnings"])
    assert not rows[EMBED]["warnings"]                               # not a chat model: no claim


def test_without_a_loaded_model_the_best_measured_coder_that_fits_is_the_default():
    call, _ = _hub(loaded=())
    doc = catalog.build(call, reports=REPORTS, min_context=0)
    assert catalog.default_model(doc) == CODER


def test_cpu_when_it_only_fits_in_memory():
    row = {"id": "a/b:c", "size_gb": 10.0}
    assert catalog._runs_on({"verdict": "does_not_fit"}, row, ram_free_gb=32) == "cpu"
    assert catalog._runs_on({"verdict": "does_not_fit"}, row, ram_free_gb=8) == "none"
    assert catalog._runs_on(None, row, ram_free_gb=32) == "cpu"


def test_eval_reports_are_read_from_the_folder(tmp_path):
    (tmp_path / "2026-10-14-factory-qwen3-coder-30b-a3b-run2.json").write_text(json.dumps({"passed": 7, "total": 20, "when": "2026-10-14"}))
    (tmp_path / "2026-10-14-suite-limited.json").write_text(json.dumps({"passed": 1, "total": 2, "limited": True}))
    (tmp_path / "notes.json").write_text("{}")
    reps = catalog.eval_reports(tmp_path)
    assert [(r["slug"], r["variant"], r["score"]) for r in reps] == [("qwen3-coder-30b-a3b", "run2", 0.35)]
    assert catalog.evals_for(CODER, reps) and not catalog.evals_for(SMALL, reps)


# ---- turns ------------------------------------------------------------------------------------------------------------------------
def test_modes_offer_the_right_tools():
    from bot.agent_runtime import toolspec

    assert chat.tools_for("chat") == frozenset()
    assert chat.tools_for("build") is None and chat.tools_for("plan") is None
    review = chat.tools_for("review")
    assert review and all(toolspec.is_read_only(n) for n in review) and "write_file" not in review
    assert chat.model_ref(CODER) == "modelmistress/" + CODER and chat.model_ref("modelmistress/x") == "modelmistress/x"


@pytest.fixture
def turns(temp_db, monkeypatch):
    frames, seen = [], {}

    async def fake_ask(prompt, **kw):
        from bot.agent_runtime.transports.base import report_speed

        seen.update(kw, prompt=prompt)
        if seen.get("slow"):
            await asyncio.sleep(30)
        report_speed(CODER, 30.0)
        report_speed(CODER, 40.0)
        report_speed("another-model", 999.0)
        return BackendResult(text="Done: renamed it.", tokens=1234)

    from bot.router import router

    monkeypatch.setattr(router, "ask", fake_ask)
    monkeypatch.setattr(catalog, "get", lambda **k: {"device": DEVICE, "models": []})
    return frames, seen


def test_a_turn_runs_on_the_sessions_model_and_workspace_and_keeps_the_reply(turns, tmp_path):
    frames, seen = turns
    s = store.create(model=CODER, mode="chat", workspace=str(tmp_path))

    async def go():
        started = await chat.send(s["id"], "rename foo to bar", _bc(frames))
        await asyncio.wait_for(chat._running[s["id"]], 10)
        return started
    started = asyncio.run(go())
    assert started["model"] == CODER and started["chat_id"] == f"localchat:{s['id']}"
    assert seen["model_override"] == "modelmistress/" + CODER and seen["backend_override"] == "native_agent"
    assert seen["context"]["allowed_tools"] == frozenset() and seen["context"]["cwd"] == str(tmp_path)
    assert seen["context"]["approval_notify"] and seen["context"]["stream_notify"]
    msgs = store.messages(s["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["text"] == "Done: renamed it." and msgs[1]["tokens"] == 1234 and msgs[1]["tokens_per_s"] == 35.0
    assert speed.measured(CODER, DEVICE)["tokens_per_s"] == 35.0                       # only this model's calls count
    end = [f for f in frames if f.get("type") == "local_chat_turn"]
    assert end and end[0]["state"] == "done" and end[0]["session_id"] == s["id"]
    from bot import bot_instances

    inst = bot_instances.get_instance(seen["instance_id"])
    assert inst["platform"] == "app" and inst["name"] == "Local Chat"
    assert chat.ensure_instance() == seen["instance_id"]                                # one instance, reused


def _bc(frames):
    async def broadcast(frame):
        frames.append(frame)
    return broadcast


def test_cancel_stops_a_turn_and_says_so(turns):
    frames, seen = turns
    seen["slow"] = True
    s = store.create(model=CODER)

    async def go():
        await chat.send(s["id"], "a long job", _bc(frames))
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError):
            await chat.send(s["id"], "again", _bc(frames))                          # one turn at a time per session
        assert await chat.cancel(s["id"]) is True
        assert await chat.cancel(s["id"]) is False
    asyncio.run(go())
    assert store.messages(s["id"])[-1]["text"] == "Stopped."
    assert [f["state"] for f in frames if f.get("type") == "local_chat_turn"] == ["cancelled"]
    assert store.running_turns() == []


def test_a_failed_turn_shows_the_error_instead_of_hanging(temp_db, monkeypatch):
    from bot.router import router

    async def boom(prompt, **kw):
        raise RuntimeError("hub went away")

    monkeypatch.setattr(router, "ask", boom)
    s = store.create(model=CODER)
    frames = []

    async def go():
        await chat.send(s["id"], "hi", _bc(frames))
        await asyncio.wait_for(chat._running[s["id"]], 10)
    asyncio.run(go())
    last = store.messages(s["id"])[-1]
    assert last["role"] == "error" and "hub went away" in last["text"]


# ---- the transport reports llama.cpp's own speed -----------------------------------------------------------------------------
def test_the_openai_transport_reads_llamacpp_timings_and_reports_them():
    from bot.agent_runtime.transports import base, openai_compatible

    r = openai_compatible._normalize({"choices": [{"message": {"content": "hi"}}], "timings": {"predicted_per_second": 33.5}}, "u")
    assert r.tokens_per_s == 33.5
    assert openai_compatible._normalize({"choices": [{"message": {"content": "hi"}}]}, "u").tokens_per_s is None
    got = []
    token = base.speed_observer.set(lambda m, t: got.append((m, t)))
    try:
        base.report_speed("m", 33.5)
        base.report_speed("m", None)
    finally:
        base.speed_observer.reset(token)
    base.report_speed("m", 1.0)                                               # nobody listening: nothing happens
    assert got == [("m", 33.5)]


# ---- the two fixes Local Chat needed ------------------------------------------------------------------------------------------
def test_the_modelmistress_provider_is_found_in_the_mm_home(monkeypatch, tmp_path):
    from bot import providers
    from bot.modules import registry

    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / "empty")
    home = tmp_path / "mm"
    home.mkdir()
    monkeypatch.setenv("MM_HOME", str(home))
    (home / "control.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "t", "pid": os.getpid()}))
    monkeypatch.setattr(providers, "_module_cache", (0.0, {}))
    got = providers.module_providers()
    assert got["modelmistress"]["base_url"] == "http://127.0.0.1:9/v1" and got["modelmistress"]["api_key"] == "t"


def test_router_ask_uses_the_turns_model(temp_db, monkeypatch):
    from bot import router as router_mod
    from bot import setup_wizard

    r = router_mod.Router()
    built = {}

    class Fake:
        model = None

        async def ask(self, prompt, context=None, timeout_s=None):
            return BackendResult(text="ok", tokens=1)

    def fake_get(name, cfg, model_override=None, hermes_home=None):
        built.update(name=name, model=model_override)
        return Fake()

    monkeypatch.setattr(r, "_get_backend", fake_get)
    monkeypatch.setattr(setup_wizard, "check_backend_ready", lambda name: (True, ""))
    asyncio.run(r.ask("hi", backend_override="native_agent", model_override="modelmistress/" + CODER))
    assert built == {"name": "native_agent", "model": "modelmistress/" + CODER}


# ---- the API ---------------------------------------------------------------------------------------------------------------------
def test_the_api(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    call, _ = _hub()
    monkeypatch.setattr(catalog, "get", lambda refresh=False, ttl_s=None: catalog.build(call, reports=REPORTS, min_context=16384))
    c = TestClient(build_app())
    h = {"X-Dashboard-Token": "test-token"}
    assert c.post("/api/local-chat/sessions", json={}).status_code in (401, 403)
    s = c.post("/api/local-chat/sessions", json={"mode": "chat"}, headers=h).json()
    assert s["mode"] == "chat" and s["cloud_assist"] == "ask" and s["autonomy"] == 2
    assert c.patch(f"/api/local-chat/sessions/{s['id']}", json={"mode": "bogus"}, headers=h).status_code == 400
    assert c.patch(f"/api/local-chat/sessions/{s['id']}", json={"title": "T"}, headers=h).json()["title"] == "T"
    assert [x["id"] for x in c.get("/api/local-chat/sessions", headers=h).json()["sessions"]] == [s["id"]]
    got = c.get(f"/api/local-chat/sessions/{s['id']}", headers=h).json()
    assert got["running"] is False and got["messages"] == [] and got["chat_id"] == f"localchat:{s['id']}"
    models = c.get("/api/local-chat/models", headers=h).json()
    assert models["default_model"] == CODER and models["device"] == DEVICE
    assert c.post(f"/api/local-chat/sessions/{s['id']}/send", json={"text": "  "}, headers=h).status_code == 400
    assert c.get("/api/local-chat/sessions/nope", headers=h).status_code == 404
    assert c.post(f"/api/local-chat/sessions/{s['id']}/cancel", headers=h).json() == {"cancelled": False}
    assert c.delete(f"/api/local-chat/sessions/{s['id']}", headers=h).json() == {"deleted": True}
