"""Long Local Chat sessions (PLAN-local-chat.md 4b, S4): a long session compacts visibly and keeps working within the coder's
32k context; the agent's task list and next-step box are parsed out of its reply; chapters, export and auto-archive."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from bot.backends.base import BackendResult
from bot.localchat import chat, housekeeping, store

CODER = "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL"
H = {"X-Dashboard-Token": "test-token"}


@pytest.fixture
def agent(temp_db, monkeypatch):
    from bot.localchat import catalog
    from bot.router import router

    calls, box = [], {"reply": "ok", "fail_summary": False}

    async def ask(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        if str(kw.get("chat_id", "")).endswith(":summary"):
            if box["fail_summary"]:
                raise RuntimeError("hub went away")
            assert kw["context"]["allowed_tools"] == frozenset()          # a summary runs no tools
            return BackendResult(text="SUMMARY: refactoring the parser; tests pass; docs left.", tokens=50)
        return BackendResult(text=box["reply"], tokens=100)

    monkeypatch.setattr(router, "ask", ask)
    monkeypatch.setattr(catalog, "get", lambda **k: {"device": "cpu", "models": [{"id": CODER, "plan": {"n_ctx_per_slot": 32768}}]})
    monkeypatch.setattr(catalog, "loaded_ids", lambda *a: {CODER})        # warm: never a load on whatever hub runs here
    return calls, box


def _turn(sid, text):
    frames = []

    async def go():
        await chat.send(sid, text, _bc(frames))
        await asyncio.wait_for(chat._running[sid], 10)
    asyncio.run(go())
    return frames


def _bc(frames):
    async def broadcast(frame):
        frames.append(frame)
    return broadcast


def _fill(sid, turns, chars=4000):
    for n in range(turns):
        store.add_message(sid, "user", f"step {n}: " + "u" * chars)
        store.add_message(sid, "assistant", f"done {n}: " + "a" * chars, model=CODER)


def test_a_long_session_compacts_visibly_and_keeps_working_on_a_32k_context(agent):
    calls, _ = agent
    s = store.create(model=CODER, mode="chat")
    _fill(s["id"], 12)                                          # ~24k tokens: past 0.7 x 32768
    before = len(store.messages(s["id"]))
    assert housekeeping.needs_compaction(s["id"], 32768)
    frames = _turn(s["id"], "next step please")
    marker = [m for m in store.messages(s["id"]) if (m["meta"] or {}).get("compaction")]
    assert len(marker) == 1 and "Compacted" in marker[0]["text"] and marker[0]["meta"]["compaction"]["source"] == "model"
    assert any(f.get("type") == "local_chat_compacted" and f["generation"] == 1 for f in frames)
    assert len(store.messages(s["id"])) == before + 3              # nothing removed: marker + the new turn's two messages
    turn = calls[-1]
    assert turn["chat_id"] == f"localchat:{s['id']}:g1"            # a fresh agent history, generation 1
    assert turn["prompt"].startswith("Summary of our conversation so far:\nSUMMARY") and turn["prompt"].endswith("next step please")
    assert housekeeping.estimate(s["id"]) < 0.7 * 32768 and not housekeeping.needs_compaction(s["id"], 32768)
    _turn(s["id"], "and the next")                                  # keeps working, no repeated summary
    assert calls[-1]["prompt"] == "and the next" and calls[-1]["chat_id"].endswith(":g1")
    assert store.messages(s["id"])[-1]["text"] == "ok"


def test_without_a_model_the_summary_is_an_extract_and_says_so(agent):
    calls, box = agent
    box["fail_summary"] = True
    s = store.create(model=CODER, mode="chat")
    _fill(s["id"], 12)
    _turn(s["id"], "go on")
    c = [m for m in store.messages(s["id"]) if (m["meta"] or {}).get("compaction")][0]["meta"]["compaction"]
    assert c["source"] == "extract" and c["summary"].startswith("(Summary made without a model")
    assert "step 0" in c["summary"]


def test_compaction_can_be_asked_for_and_is_off_at_zero(agent, client, monkeypatch):
    s = store.create(model=CODER, mode="chat")
    _fill(s["id"], 3, chars=50)
    r = client.post(f"/api/local-chat/sessions/{s['id']}/compact", headers=H).json()
    assert r["compacted"] is True and r["messages"] == 2
    from bot import localchat

    monkeypatch.setattr(localchat, "settings", lambda: {**localchat.DEFAULTS, "compact_at": 0})
    _fill(s["id"], 20)
    assert not housekeeping.needs_compaction(s["id"], 32768)


def test_the_task_list_and_next_step_box_come_out_of_the_reply(agent):
    calls, box = agent
    box["reply"] = ("Renamed it.\n\n```abp-steps\n[x] find the callers\n[x] rename\n[ ] update the docs\n```\n"
                    '```abp-next\n{"recap": "foo is bar now; tests pass.", "ask": "Update the docs too?", "options": ["Yes", "Not now", "x"]}\n```')
    s = store.create(model=CODER, mode="build")
    _turn(s["id"], "rename foo")
    assert housekeeping.NEXT_STEP_INSTRUCTIONS in calls[-1]["prompt"]          # asked for in Build
    last = store.messages(s["id"])[-1]
    assert last["text"] == "Renamed it."
    assert last["meta"]["steps"] == [{"done": True, "text": "find the callers"}, {"done": True, "text": "rename"},
                                     {"done": False, "text": "update the docs"}]
    assert last["meta"]["next"] == {"recap": "foo is bar now; tests pass.", "ask": "Update the docs too?", "options": ["Yes", "Not now"]}
    plain = store.create(model=CODER, mode="chat")
    _turn(plain["id"], "hi")
    assert housekeeping.NEXT_STEP_INSTRUCTIONS not in calls[-1]["prompt"]       # not in plain chat
    assert housekeeping.steps_and_next("no blocks") == ("no blocks", {})


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    return TestClient(build_app())


def test_chapters_and_export(client):
    s = store.create(title="Parser work", model=CODER)
    store.add_message(s["id"], "user", "hello")
    assert client.post(f"/api/local-chat/sessions/{s['id']}/chapters", json={"title": "Phase 2"}, headers=H).status_code == 200
    assert client.post(f"/api/local-chat/sessions/{s['id']}/chapters", json={"title": " "}, headers=H).status_code == 400
    md = client.get(f"/api/local-chat/sessions/{s['id']}/export?format=md", headers=H)
    assert md.status_code == 200 and "# Parser work" in md.text and "## Phase 2" in md.text and "**You**:" in md.text
    assert 'filename="Parser_work.md"' in md.headers["content-disposition"]
    js = client.get(f"/api/local-chat/sessions/{s['id']}/export?format=json", headers=H).json()
    assert js["session"]["id"] == s["id"] and js["messages"][-1]["meta"]["chapter"] == "Phase 2"


def test_auto_archive_leaves_pinned_and_waiting_sessions(temp_db, monkeypatch):
    old, pinned, waiting, fresh = (store.create(title=t) for t in ("old", "pinned", "waiting", "fresh"))
    store.update(pinned["id"], pinned=True)
    store.set_status(waiting["id"], "needs_you")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 40 * 86400)
    store.add_message(fresh["id"], "user", "still here")            # touched "today"
    monkeypatch.setattr(time, "time", lambda: real() + 40 * 86400 + 1)
    assert housekeeping.auto_archive(0) == []
    assert housekeeping.auto_archive(30) == [old["id"]]
    assert store.get(old["id"])["archived"] and not store.get(pinned["id"])["archived"] and not store.get(waiting["id"])["archived"]
