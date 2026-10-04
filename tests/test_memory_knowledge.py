"""The knowledge base under the memory fabric (bot/memoryfabric: knowledge, sources, vault, diff) on real files and a
real git repository: chunking, the scoring gate, entities and their graph, summary trees, every retrieval mode, folder
and notes sources with sync, the Obsidian vault read back after a person edits it, and the diff ledger."""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def kb(temp_db, tmp_path, monkeypatch):
    from bot.fileserver.index import hash_vectors
    from bot.memoryfabric import knowledge, store
    monkeypatch.setenv("ABP_MEMORY_DIR", str(tmp_path / "vault"))
    store.reset_cache()
    knowledge._ready_for = None
    store._embed_cache["e"] = ("hash-tfidf", hash_vectors, time.time() + 3600)
    yield knowledge
    store.reset_cache()


MEETING = """Board call with Alice Smith and Bob Jones about the Falcon launch. Alice (alice@example.com) wants the
launch moved to March because the payment provider integration is late. Bob will check with @carol-ops whether the
staging cluster can take the load test. See https://wiki.example.com/falcon for the plan. #falcon #launch"""


def test_chunks_are_bounded_and_canonical(kb):
    text = "<p>Hello <b>there</b></p><div>Second para</div>" + "\n\n" + ("A long sentence about storage. " * 300)
    body = kb.canonicalise(text + "\n> quoted reply\n--\nsignature")
    assert "<p>" not in body and "quoted reply" not in body and "signature" not in body
    pieces = kb.chunk(body, limit=200)
    assert len(pieces) > 3 and all(kb._tokens(p) <= 200 for p in pieces)
    assert kb._cid("s", "i", 0) == kb._cid("s", "i", 0) != kb._cid("s", "i", 1)       # deterministic ids


def test_entities_and_the_scoring_gate(kb):
    ents = {e for e, _, _ in kb.entities(MEETING)}
    assert {"name:alice smith", "name:bob jones", "email:alice@example.com", "handle:carol-ops", "tag:falcon",
            "url:wiki.example.com/falcon"} <= ents
    total, kept, _ = kb.score(MEETING, "email", ["reply"], kb.entities(MEETING))
    assert kept and total > 0.5
    assert kb.score("thanks!", "chat", [], []) [1:] == (False, "tiny and without entities")
    assert kb.score("ok", "github", ["priority_high"], [])[1] in (True, False)        # priority bypasses the tiny guard
    borderline = "word " * 40
    low = kb.score(borderline, "chat", [], [], importance=lambda b: 0.0)
    high = kb.score(borderline, "chat", [], [], importance=lambda b: 1.0)
    assert high[0] > low[0]


def test_ingest_trees_and_every_retrieval_mode(kb):
    t0 = 1_700_000_000.0
    for i in range(14):
        kb.ingest("mail", f"m{i}", f"Falcon update {i}",
                  MEETING.replace("March", ["March", "April", "May"][i % 3]) + f"\n\nUpdate number {i}: " + "detail " * 320,
                  kind="email", ts=t0 + i * 3600, tags=["reply"])
    kb.ingest("mail", "noise", "", "thanks!", kind="chat", ts=t0)
    st = kb.stats()["sources"]["mail"]
    assert st["kept"] == 14 and st["chunks"] == 15 and st["summaries"] >= 2
    ents = kb.query("search_entities", name="alice")["entities"]
    assert ents[0]["entity"] in ("name:alice smith", "email:alice@example.com") and ents[0]["mentions"] >= 14
    edges = {e["object"] for e in kb.query("neighbors", entity="name:alice smith")["edges"]}
    assert "name:bob jones" in edges
    src = kb.query("query_source", source_id="mail", query="payment provider late")
    assert src["hits"] and src["hits"][0]["node_kind"] in ("summary", "leaf") and src["total"] >= 1
    top = next(h for h in src["hits"] if h["node_kind"] == "summary")
    kids = kb.query("drill_down", node_id=top["node_id"])
    assert kids["hits"] and all(h["node_id"] in top["child_ids"] for h in kids["hits"])
    win = kb.query("cover_window", since=t0, until=t0 + 14 * 3600)
    covered = set()
    for h in win["hits"]:
        covered |= kb._leaves_of(h["node_id"])
    assert len(covered) == 14 and len(win["hits"]) < 14                              # fewer nodes than leaves
    leaves = kb.query("fetch_leaves", ids=[kb._cid("mail", "m3", 0), "nope"])
    assert [h["source_ref"]["item_id"] for h in leaves["hits"]] == ["m3"]
    w = kb.query("walk", query="What did Alice Smith and Bob Jones decide?")
    assert w["route"] == "local" and w["hits"] and "name:alice smith" in w["query_entities"]
    g = kb.query("walk", query="storage throughput benchmarks")
    assert g["route"] == "global"
    doc = kb.query("ingest_document", title="Note", text="Carol Smith owns the staging cluster upgrade plan for Falcon.")
    assert doc["kept"] == 1
    kb.ingest("mail", "m0", "changed", "Completely different text about Delta Force operations and Eve Adams.", kind="email", ts=t0)
    assert any("Eve Adams" in h["content"] for h in kb.query("query_source", source_id="mail", limit=50)["hits"])
    assert kb.remove_item("mail", "m0") >= 1
    with pytest.raises(ValueError, match="mode is"):
        kb.query("teleport")


def test_folder_and_notes_sources_sync_into_the_vault_and_the_ledger(kb, tmp_path):
    from bot.memoryfabric import diff, sources, vault
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "plan.md").write_text(MEETING)
    (docs / "sub" / "ops.txt").write_text("Bob Jones runs the on-call rota for the staging cluster. " * 30)
    (docs / ".hidden.md").write_text("secret")
    (docs / "image.bin").write_bytes(b"\0" * 10)
    with pytest.raises(ValueError, match="not a folder"):
        sources.add("folder", path=str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="owner/name"):
        sources.add("github", repo="nope")
    src = sources.add("folder", "Docs", path=str(docs))
    assert {s["id"] for s in sources.listing()} == {"notes", src["id"]}
    r = sources.sync(src["id"])
    assert r["stage"] == "completed" and r["added"] == 2 and r["items"] == 2
    assert sources.sync(src["id"])["added"] == 0                                     # unchanged: nothing re-ingested
    first = diff.diff(src["id"])                                                       # nothing read yet: all of it is new
    assert sorted(x["item"] for x in first["added"]) == ["plan.md", "sub_ops.txt"]
    assert diff.diff(src["id"])["added"] == []                                        # the read marker moved
    diff.checkpoint("before-edit")
    (docs / "plan.md").write_text(MEETING + "\n\nUpdate: the launch is confirmed for April with Dave Brown.")
    (docs / "sub" / "ops.txt").unlink()
    (docs / "new.md").write_text("Eve Adams joins the Falcon team as release manager next week. " * 5)
    r = sources.sync(src["id"])
    assert (r["added"], r["changed"], r["removed"]) == (1, 1, 1)
    d = diff.diff(src["id"], include_text=True)
    assert [x["item"] for x in d["added"]] == ["new.md"] and [x["item"] for x in d["removed"]] == ["sub_ops.txt"]
    assert d["modified"][0]["item"] == "plan.md" and "Dave Brown" in d["modified"][0]["diff"]
    assert diff.diff(checkpoint_name="before-edit")["added"][0]["item"] == "new.md"
    assert diff.diff()["sources"] == [{"source_id": src["id"], "snapshots": 2}]
    assert "Added (1): new.md" in diff.summary_text(d)
    with pytest.raises(ValueError, match="no checkpoint"):
        diff.diff(checkpoint_name="never")
    # the vault: summaries and entities written, a hand-written note becomes knowledge
    assert not list((vault.root() / "summaries").rglob("*.md"))                      # too little to fill a bucket yet
    from bot.memoryfabric import knowledge
    assert knowledge.seal(src["id"], force=True) == 1                                  # the daily close seals what is left
    assert vault.write_source(src["id"]) == 1
    files = list((vault.root() / "summaries").rglob("*.md"))
    assert files and "source: " in files[0].read_text()
    assert any((vault.root() / "entities").rglob("name-*.md"))
    (vault.root() / "notes" / "2026-10-03-standup.md").write_text("Frank Ocean will migrate the NAS to the new rack on Friday.")
    assert sources.sync("notes")["added"] == 1
    assert sources.status_list()[0]["freshness"] in ("active", "recent")
    assert sources.remove(src["id"]) and src["id"] not in {s["id"] for s in sources.listing()}
    with pytest.raises(ValueError, match="always a source"):
        sources.remove("notes")
    assert vault.obsidian_link().startswith("obsidian://open?path=")


def test_a_person_edits_memories_in_the_vault(kb):
    from bot import db, memory
    from bot.memoryfabric import store, vault
    store.set_settings({"shared_approval": False})
    a = store.remember("The office wifi is called Lattice.", shared=True)
    b = store.remember("Backups run at 3 am.", shared=True)
    path = vault.write_memories(0)
    text = path.read_text()
    assert f"<!-- m{a['id']} -->" in text and "Backups run at 3 am." in text
    assert vault.read_memories(0) == {"changed": 0, "added": 0, "removed": 0}       # not edited: nothing to do
    time.sleep(0.05)
    text = text.replace("Lattice.", "Lattice-5G.").replace(f"- Backups run at 3 am. <!-- m{b['id']} -->\n", "")
    text = text.replace("## Other things to remember\n", "## Other things to remember\n\n- The printer is on floor 2.\n")
    path.write_text(text)
    assert vault.read_memories(0) == {"changed": 1, "added": 1, "removed": 1}
    now = {r["content"] for r in db.list_memory_entries(0, status="approved")}
    assert now == {"The office wifi is called Lattice-5G.", "The printer is on floor 2."}
    assert vault.sync_memories() == {"changed": 0, "added": 0, "removed": 0}
    assert memory.find_duplicate(0, "the printer is on floor 2")


def test_the_knowledge_routes_and_tools(kb, tmp_path):
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.agent_runtime import tools as agent_tools
    from bot.dashboard import memory_api
    app = FastAPI()
    memory_api.register(app, lambda: None)
    c = TestClient(app)
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "a.md").write_text(MEETING)
    src = c.post("/api/memory/sources", json={"kind": "folder", "label": "F", "path": str(folder)}).json()
    assert c.post("/api/memory/sources", json={"kind": "carrier-pigeon"}).status_code == 400
    assert c.post(f"/api/memory/sources/{src['id']}/sync").json()["stage"] == "completed"
    assert c.patch(f"/api/memory/sources/{src['id']}", json={"enabled": False, "label": "Folder"}).json()["label"] == "Folder"
    listed = {s["id"]: s for s in c.get("/api/memory/sources").json()}
    assert listed[src["id"]]["kept"] == 1 and not listed[src["id"]]["enabled"]
    w = c.post("/api/memory/tree", json={"mode": "walk", "query": "What did Alice Smith say?"}).json()
    assert w["hits"] and w["route"] == "local"
    assert c.post("/api/memory/tree", json={"mode": "ingest_document", "text": "x"}).status_code == 400
    assert c.post("/api/memory/tree", json={"mode": "drill_down"}).status_code == 400            # node_id missing
    assert c.post("/api/memory/tree/ingest", json={"title": "T", "text": "Grace Hopper wrote the compiler notes."}).json()["kept"] == 1
    assert "sources" in c.get("/api/memory/tree/stats").json()
    assert c.get("/api/memory/diff", params={"source_id": src["id"]}).json()["added"][0]["item"] == "a.md"
    assert c.post("/api/memory/diff/checkpoint", json={"name": "bad name!"}).status_code == 400
    assert c.post("/api/memory/diff/checkpoint", json={"name": "v1"}).json() == {"checkpoint": "v1"}
    assert c.get("/api/memory/vault").json()["obsidian"].startswith("obsidian://")
    assert c.post("/api/memory/vault/sync").json()["sources"] >= 2
    assert c.delete(f"/api/memory/sources/{src['id']}").json() == {"removed": True}
    # the agents' tools
    async def run(name, inp):
        return await agent_tools.execute_tool(name, inp, workspace=tmp_path)
    out = asyncio.run(run("memory_tree", {"mode": "search_entities", "name": "grace"}))
    assert "name:grace hopper" in out
    assert asyncio.run(run("memory_diff", {"source_id": "documents"})) == "Changes in documents:\nnothing new."
    with pytest.raises(agent_tools.ToolError):
        asyncio.run(run("memory_tree", {"mode": "teleport"}))
