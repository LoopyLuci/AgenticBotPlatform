"""A nexusfoundry memory source over real Knowledge Modules (bot/memoryfabric/sources.py).

The KMs are written in NexusFoundry's own on-disk format - `<root>/<km_id>/meta.json` (its
KMMetadata, core/km.py) plus `chunks.json`, its fact bank - inside the test's tmp dir, so nothing here
reads or touches the owner's checkout. A source may point at a checkout (its store is
storage/knowledge_modules) or at a folder of KMs; both are covered, with the awkward cases the
registry tolerates too: a folder without meta.json, an unreadable meta.json, a LoRA-only module with no
chunks, and a KM deleted between syncs.
"""
from __future__ import annotations

import json
import time

import pytest

RDNA = {
    "km_id": "sample_rdna3_quantization",
    "name": "Rdna3 Quantization",
    "domain": "hardware",
    "subdomain": "amd_gpu",
    "description": "AMD RDNA3 quantization constraints and workarounds",
    "base_model": "",
    "embed_model_name": "",
    "tags": ["rdna3", "amd", "quantization", "rocm"],
    "source": "sample",
    "n_chunks": 3,
}
RDNA_CHUNKS = [
    "bitsandbytes has no ROCm support on Windows, so the paged eight-bit AdamW optimizer is unavailable "
    "on RDNA3. Fused AdamW with fp32 state is the correct default instead.",
    "HQQ targets inference on RDNA3, while Quanto 4-bit int4 targets training. The training path is load "
    "4-bit, prepare_model_for_kbit_training, then LoRA on the attention projections.",
    "On Windows ROCm, torch.compile does not work. Inference and training run in eager mode, which is "
    "noticeably slower than on Linux.",
]
MOE = {"km_id": "sample_moe_moa", "name": "Moe Moa", "domain": "nexus", "subdomain": "orchestration",
       "description": "sparse MoE/MoA bases seeded from KM energy", "base_model": "", "tags": ["moe", "moa"],
       "source": "sample", "n_chunks": 1}


def write_km(root, km_id: str, meta: dict, chunks: list[str] | None = None) -> None:
    """A Knowledge Module exactly as NexusFoundry's KnowledgeModule.save() lays one out."""
    d = root / km_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if chunks is not None:
        (d / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")


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


def _items(source):
    from bot.memoryfabric import sources
    return list(sources.read(source))


def test_a_km_folder_reads_as_one_item_per_module(kb, tmp_path):
    from bot.memoryfabric import sources

    store = tmp_path / "storage" / "knowledge_modules"
    write_km(store, RDNA["km_id"], RDNA, RDNA_CHUNKS)
    write_km(store, MOE["km_id"], MOE, ["The router picks the experts a KM's LoRA energy says are worth it."])
    src = sources.add("nexusfoundry", "Foundry", path=str(tmp_path))          # the checkout, not the store
    assert sources.km_root(tmp_path) == store
    items = {i[0]: i for i in _items(src)}
    assert set(items) == {RDNA["km_id"], MOE["km_id"]}                        # km_id is the item id
    item_id, title, text, ts, tags = items[RDNA["km_id"]]
    assert title == "Rdna3 Quantization"
    assert "AMD RDNA3 quantization constraints" in text and RDNA_CHUNKS[0] in text
    assert f"Knowledge Module {item_id}" in text and "Domain: hardware. Subdomain: amd_gpu." in text
    assert tags == ["nexusfoundry", "km", "hardware", "amd_gpu", "rdna3", "amd", "quantization", "rocm"]
    assert ts == pytest.approx(RDNA.get("created_at", ts))
    # the KMs that the registry itself skips, and one with no fact bank at all
    (store / "not-a-km").mkdir()
    (store / "not-a-km" / "chunks.json").write_text("[]", encoding="utf-8")
    (store / "broken").mkdir()
    (store / "broken" / "meta.json").write_text("{not json", encoding="utf-8")
    write_km(store, "km_lora_only", {"km_id": "km_lora_only", "name": "Behaviour only", "domain": "behaviour",
                                     "subdomain": "", "description": "", "base_model": "qwen", "tags": []})
    items = {i[0]: i for i in _items(src)}
    assert set(items) == {RDNA["km_id"], MOE["km_id"], "km_lora_only"}
    assert "Knowledge Module km_lora_only" in items["km_lora_only"][2]
    assert items["km_lora_only"][4] == ["nexusfoundry", "km", "behaviour"]
    # pointing at the KM folder itself reads the same KMs
    assert {i[0] for i in _items({"kind": "nexusfoundry", "path": str(store)})} == set(items)


def test_the_km_folder_is_capped_per_module_when_asked(kb, tmp_path):
    store = tmp_path / "kms"
    write_km(store, RDNA["km_id"], RDNA, RDNA_CHUNKS)
    assert RDNA_CHUNKS[2] in _items({"kind": "nexusfoundry", "path": str(store)})[0][2]
    capped = _items({"kind": "nexusfoundry", "path": str(store), "max_chunks": 1})[0][2]
    assert RDNA_CHUNKS[0] in capped and RDNA_CHUNKS[1] not in capped


def test_a_knowledge_module_syncs_into_the_base_and_freshness_moves(kb, tmp_path):
    from bot.memoryfabric import knowledge, sources, vault

    store = tmp_path / "storage" / "knowledge_modules"
    write_km(store, RDNA["km_id"], dict(RDNA, created_at=time.time()), RDNA_CHUNKS)
    write_km(store, MOE["km_id"], MOE, ["The router picks the experts a KM's LoRA energy says are worth it."])
    src = sources.add("nexusfoundry", path=str(tmp_path))
    r = sources.sync(src["id"])
    assert (r["stage"], r["added"], r["items"]) == ("completed", 2, 2)
    kept = knowledge.stats()["sources"][src["id"]]
    assert kept["kept"] == 2
    row = knowledge.query("query_source", source_id=src["id"], query="eight-bit AdamW optimizer on RDNA3", limit=10)
    assert any("bitsandbytes" in h["content"] for h in row["hits"])
    assert all(h["title"] == "Rdna3 Quantization" for h in row["hits"] if "bitsandbytes" in h["content"])
    # the module's own vocabulary reaches the model as the chunk's tags, so a question about it finds it
    tags = json.loads(knowledge._conn().execute("SELECT tags FROM kb_chunks WHERE source_id=? LIMIT 1",
                                                (src["id"],)).fetchone()[0])
    assert {"nexusfoundry", "km", "hardware", "amd_gpu", "rocm"} <= set(tags)
    walk = knowledge.query("walk", query="What does the RDNA3 quantization Knowledge Module say?")
    assert walk["hits"] and any(h["source_id"] == src["id"] for h in walk["hits"])
    assert sources.sync(src["id"])["added"] == 0                                # unchanged: nothing re-ingested
    assert sources.freshness(src["id"]) in ("active", "recent")
    # a KM edited in place is re-ingested; one deleted from the store is dropped
    write_km(store, MOE["km_id"], dict(MOE, description="sparse MoE seeded from LoRA energy (edited)"),
             ["The router picks the experts a KM's LoRA energy says are worth it."])
    (store / MOE["km_id"] / "meta.json").touch()
    assert sources.sync(src["id"])["changed"] == 1
    import shutil

    shutil.rmtree(store / MOE["km_id"])
    r = sources.sync(src["id"])
    assert (r["added"], r["changed"], r["removed"]) == (0, 0, 1)
    assert knowledge.stats()["sources"][src["id"]]["chunks"] == 1
    assert knowledge.seal(src["id"], force=True) == 1
    assert vault.write_source(src["id"]) == 1                                 # the vault takes them too
    assert list((vault.root() / "summaries").rglob("*.md"))
    assert next(s for s in sources.status_list() if s["id"] == src["id"])["freshness"] in ("active", "recent")


def test_a_knowledge_module_source_is_added_from_the_api_and_the_cli(kb, tmp_path):
    from bot.memoryfabric import sources

    store = tmp_path / "storage" / "knowledge_modules"
    write_km(store, RDNA["km_id"], RDNA, RDNA_CHUNKS)
    assert "nexusfoundry" in sources.KINDS and sources.KIND_OF["nexusfoundry"] == "note"
    with pytest.raises(ValueError, match="not a folder"):
        sources.add("nexusfoundry", path=str(tmp_path / "nope"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import memory_api
    app = FastAPI()
    memory_api.register(app, lambda: None)
    c = TestClient(app)
    src = c.post("/api/memory/sources", json={"kind": "nexusfoundry", "label": "Foundry", "path": str(tmp_path)}).json()
    assert src["kind"] == "nexusfoundry" and src["label"] == "Foundry"
    assert c.post(f"/api/memory/sources/{src['id']}/sync").json()["added"] == 1
    listed = {s["id"]: s for s in c.get("/api/memory/sources").json()}
    assert listed[src["id"]]["kept"] == 1 and listed[src["id"]]["chunks"] == 1
    assert c.delete(f"/api/memory/sources/{src['id']}").json() == {"removed": True}

    # and the CLI offers the kind, so `abp memory source-add nexusfoundry <label> --path <checkout>` parses
    from abp_cli.__main__ import _parser

    args = _parser().parse_args(["memory", "source-add", "nexusfoundry", "Foundry", "--path", str(tmp_path)])
    assert (args.kind, args.path) == ("nexusfoundry", str(tmp_path))
    assert c.post("/api/memory/sources", json={"kind": "nexusfoundry", "label": "No path"}).status_code == 400