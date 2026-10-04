"""The memory fabric (bot/memoryfabric): memories shared by every bot and model, recall by meaning, one conversation
whichever backend answers, and the handoff a model gets after another one answered."""
from __future__ import annotations

import asyncio
import time

import pytest

from bot.backends.base import Backend, BackendResult


@pytest.fixture
def mf(temp_db):
    from bot.fileserver.index import hash_vectors
    from bot.memoryfabric import store
    store.reset_cache()
    store._embed_cache["e"] = ("hash-tfidf", hash_vectors, time.time() + 3600)   # the always-present embedder
    yield store
    store.reset_cache()


def _bot(name: str) -> int:
    from bot import bot_instances
    return bot_instances.create_instance(name=name, platform="app", backend="api", credentials={}, allowed_user_ids=[], actor="test")


def test_shared_memories_reach_every_bot_and_own_ones_stay_with_their_bot(mf):
    from bot import memory
    a, b = _bot("alpha"), _bot("beta")
    memory.set_approval_required(a, False, "test")
    mf.set_settings({"shared_approval": False})
    s = mf.remember("The home server is called Server and runs on the LAN.", shared=True)
    own = mf.remember("Alpha's reports go to the ops channel.", instance_id=a)
    assert s["shared"] and s["approved"] and not own["shared"] and own["approved"]
    seen_a = {r["content"] for r in mf.approved(a)}
    seen_b = {r["content"] for r in mf.approved(b)}
    assert "The home server is called Server and runs on the LAN." in seen_a & seen_b
    assert "Alpha's reports go to the ops channel." in seen_a and "Alpha's reports go to the ops channel." not in seen_b
    again = mf.remember("the home server is called Server and runs on the LAN", shared=True)
    assert again["duplicate"] and again["id"] == s["id"]
    mf.set_settings({"shared_approval": True})
    gated = mf.remember("Backups run nightly at 3 am.", shared=True)
    assert not gated["approved"] and "Backups run nightly at 3 am." not in {r["content"] for r in mf.approved(b)}
    with pytest.raises(ValueError, match="unknown memory setting"):
        mf.set_settings({"colour": 1})


def test_recall_puts_related_memories_first_within_the_budget(mf):
    mf.set_settings({"shared_approval": False})
    for text in ("The person's dog is named Biscuit.", "Deploys go through the staging cluster first.",
                 "The GPU is an RX 7900 XTX with 24 GB.", "Invoices are due on the 5th of each month."):
        mf.remember(text, shared=True)
    hits = mf.recall("which GPU does this machine have?", None)
    assert hits and hits[0]["content"] == "The GPU is an RX 7900 XTX with 24 GB." and hits[0]["similarity"] > 0
    block = mf.memory_block(None, "how much memory does the GPU have", budget_chars=60)
    assert "RX 7900 XTX" in block and "more memories are not shown" in block
    assert "Biscuit" in mf.memory_block(None)                          # no question: the most confirmed, all fitting
    assert mf.related_block(None, "zzz qqq") == ""
    assert mf.memory_block(_bot("empty")) != ""                       # shared ones reach a bot with none of its own


def test_a_model_after_a_switch_gets_the_turns_it_missed(mf):
    t = mf.thread_key(7, 100, None)
    mf.record(t, "user", "Let's plan the garden: tomatoes and basil.", instance_id=7)
    mf.record(t, "assistant", "Tomatoes need full sun; basil likes the same spot.", instance_id=7, backend="cli", model="sonnet")
    mf.record(t, "user", "And how often should I water them?", instance_id=7)
    # the native loop never answered here: it gets the whole conversation, not the message being answered now
    h = mf.handoff(t, "api", "qwen3")
    assert "tomatoes and basil" in h and "full sun" in h and "Assistant (sonnet)" in h and "water them" not in h
    mf.record(t, "assistant", "Every two days in summer.", instance_id=7, backend="custom_model", model="qwen3")
    mf.record(t, "user", "Thanks. What about pests?", instance_id=7)
    assert mf.handoff(t, "native_agent", "qwen3") == ""               # the native family saw everything up to now
    back = mf.handoff(t, "cli", "sonnet")                              # the CLI missed only the native answer
    assert "Every two days" in back and "full sun" not in back
    assert "full sun" in mf.handoff(t, "opencode")                     # a one-shot backend gets it all, every time
    assert [x["thread"] for x in mf.threads(7)] == [t]


def test_long_conversations_hand_off_a_summary_and_the_latest_turns(mf):
    t = mf.thread_key(8, 1, None)
    for i in range(40):
        mf.record(t, "user", f"Question {i} about the build " + "x" * 300, instance_id=8)
        mf.record(t, "assistant", f"Answer {i} " + "y" * 300, instance_id=8, backend="cli", model="m")
    mf.set_settings({"handoff_turns": 6, "handoff_chars": 3000})
    h = mf.handoff(t, "api")
    assert "Earlier:" in h and "Answer 39" in h and len(h) < 4000
    assert "Question 0" in mf.summary(t, 10_000) or "earlier turns omitted" in mf.summary(t, 10_000)


class _Recorder(Backend):
    def __init__(self, name: str, reply: str):
        self.name, self.reply, self.model, self.prompts, self.contexts = name, reply, f"{name}-model", [], []

    async def ask(self, prompt, *, context=None, timeout_s=30):
        self.prompts.append(prompt)
        self.contexts.append(dict(context or {}))
        return BackendResult(text=self.reply)


def test_the_router_records_every_turn_and_hands_off_between_backends(mf, monkeypatch):
    asyncio.run(_router_switch(mf, monkeypatch))


async def _router_switch(mf, monkeypatch):
    from bot import memory, router as router_mod, setup_wizard
    iid = _bot("switcher")
    memory.set_approval_required(iid, False, "test")
    mf.set_settings({"shared_approval": False})
    mf.remember("The person likes short answers.", shared=True)
    cli, native = _Recorder("cli", "Paris is the capital."), _Recorder("api", "About 2.1 million people.")
    r = router_mod.Router()
    use = {"name": "cli"}
    monkeypatch.setattr(r, "resolve_chain", lambda *a, **k: [use["name"]])
    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, **k: {"cli": cli, "api": native}[name])
    monkeypatch.setattr(setup_wizard, "check_backend_ready", lambda name: (True, ""))
    await r.ask("What is the capital of France?", instance_id=iid, chat_id=5)
    assert "likes short answers" in cli.prompts[0] and cli.prompts[0].endswith("What is the capital of France?")
    use["name"] = "api"
    await r.ask("How many people live there? Remember that I live in Lyon.", instance_id=iid, chat_id=5)
    ctx = native.contexts[0]
    assert "capital of France" in ctx["memory_handoff"] and "Paris is the capital" in ctx["memory_handoff"]
    assert native.prompts[0] == "How many people live there? Remember that I live in Lyon."   # the native loop adds them itself
    t = mf.thread_key(iid, 5, None)
    assert [(x["role"], x["backend"]) for x in mf.turns(t)] == [("user", None), ("assistant", "cli"), ("user", None), ("assistant", "api")]
    for _ in range(50):                                                # the extraction runs in the background
        if any("Lyon" in m["content"] for m in memory.listing(iid)):
            break
        await asyncio.sleep(0.05)
    assert any(m["content"] == "I live in Lyon." for m in memory.listing(iid))
    use["name"] = "cli"
    await r.ask("And its river?", instance_id=iid, chat_id=5)
    assert "2.1 million" in cli.prompts[1] and "Paris is the capital" not in cli.prompts[1].split("conversation so far")[-1]


def test_statements_become_proposed_memories_and_questions_do_not():
    from bot.memoryfabric import extract
    got = extract.candidates("Hi! My name is Ada Lovelace. Please never push to main without asking. "
                             "I prefer tabs over spaces. Remember that the VPN is down on Sundays for every bot. "
                             "Can you remember the build number?")
    assert [(c["kind"], c["content"], c["shared"]) for c in got] == [
        ("user", "The person's name is Ada Lovelace.", False),
        ("feedback", "Never push to main without asking.", False),
        ("feedback", "The person prefers tabs over spaces.", False),
        ("fact", "The VPN is down on Sundays.", True)]
    assert extract.candidates("") == [] and extract.candidates("Always use ruff before committing.")[0]["kind"] == "feedback"


def test_the_memory_api_end_to_end(mf):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import memory_api
    app = FastAPI()
    memory_api.register(app, lambda: None)
    c = TestClient(app)
    iid = _bot("api-bot")
    assert c.get("/api/memory").json()["embedder"] == "hash-tfidf"
    added = c.post("/api/memory/entries", json={"content": "The NAS is at 192.168.1.20.", "kind": "reference"}).json()
    assert added["shared"] and not added["approved"]                              # shared memories are reviewed first
    assert c.post(f"/api/memory/entries/{added['id']}/approve").json()["status"] == "approved"
    assert c.post(f"/api/memory/entries/{added['id']}/approve").status_code == 404
    own = c.post("/api/memory/entries", json={"content": "This bot answers in French.", "instance_id": iid}).json()
    assert not own["shared"]
    assert c.post("/api/memory/entries", json={"content": " "}).status_code == 400
    assert [e["content"] for e in c.get("/api/memory/entries", params={"scope": "shared"}).json()] == ["The NAS is at 192.168.1.20."]
    assert c.get("/api/memory/entries", params={"scope": "bogus"}).status_code == 400
    assert c.get("/api/memory/search", params={"q": "where is the NAS"}).json()[0]["content"].startswith("The NAS")
    assert "192.168.1.20" in c.get("/api/memory/context", params={"q": "NAS address"}).json()["block"]
    assert c.put("/api/memory/settings", json={"recall_k": 3}).json()["recall_k"] == 3
    assert c.put("/api/memory/settings", json={"nope": 1}).status_code == 400
    t = "ext:openhuman:1"
    assert c.post(f"/api/memory/threads/{t}", json={"role": "user", "text": "hello from another program"}).json()["id"]
    assert c.post(f"/api/memory/threads/{t}", json={"role": "robot", "text": "x"}).status_code == 400
    assert c.get(f"/api/memory/threads/{t}").json()[0]["text"] == "hello from another program"
    assert "hello from another program" not in c.get("/api/memory/context", params={"thread": t}).json()["block"]  # the message itself
    c.post(f"/api/memory/threads/{t}", json={"role": "assistant", "text": "hi there", "backend": "openhuman", "model": "m"})
    c.post(f"/api/memory/threads/{t}", json={"role": "user", "text": "next question"})
    assert "hello from another program" in c.get("/api/memory/context", params={"thread": t, "backend": "cli"}).json()["block"]
    assert c.delete(f"/api/memory/entries/{added['id']}", params={"scope": "shared"}).json() == {"deleted": added["id"]}
    assert c.delete(f"/api/memory/entries/{added['id']}", params={"scope": "shared"}).status_code == 404


def test_the_model_server_adds_memory_when_asked(mf, tmp_path, monkeypatch):
    from starlette.requests import Request

    from bot.localai import server
    mf.set_settings({"shared_approval": False})
    mf.remember("The person's timezone is Europe/Paris.", shared=True)

    def req(headers=None):
        return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]})
    b = {"model": "qwen2.5:0.5b+memory"}
    block = server._memory(req(), b, "what time is it for me")
    assert b["model"] == "qwen2.5:0.5b" and "Europe/Paris" in block
    assert server._memory(req(), {"model": "qwen2.5:0.5b"}, "x") == ""             # not asked: nothing added
    assert "Europe/Paris" in server._memory(req({"X-ABP-Memory": "1"}), {"model": "m"}, "timezone")
    msgs = server._with_memory([{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}], "MEM")
    assert msgs[0]["content"] == "Be brief.\n\nMEM" and len(msgs) == 2
    assert server._with_memory([{"role": "user", "content": "hi"}], "MEM")[0] == {"role": "system", "content": "MEM"}
    assert server._last_user([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]) == "a"


def test_integration_keys_can_be_given_the_memory(mf):
    from bot import integrations
    allowed = {s for s in integrations.SCOPES if s.startswith("memory:")}
    assert allowed == {"memory:read", "memory:write"}
    assert {"memory:read", "memory:write"} <= set(integrations.PRESETS["omnisystem"]["scopes"])
