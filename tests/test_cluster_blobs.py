"""Cluster job data (bot/cluster/blobs.py): the content-addressed store, peer transfer over the cluster API, inputs and
outputs of jobs, and data-local placement. Two in-process nodes with their own folders talk through the real API app
(ASGI test client); no sockets, no real peers."""
from __future__ import annotations

import base64
import sys
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bot.cluster import blobs, executor, membership, offer, scheduler, store
from bot.cluster.blobs import BlobError, BlobStore
from bot.cluster.executor import JobError
from bot.cluster.offer import Budget

PY = sys.executable
CHUNK = 4096
DATA = bytes((i * 7 + i // 251) % 256 for i in range(CHUNK * 5 + 123))      # six chunks, the last one short


def _app(kind: str) -> FastAPI:
    from bot.dashboard import cluster_api
    app = FastAPI()

    def identify():
        if kind == "none":
            raise HTTPException(status_code=401, detail="no")
        return kind
    cluster_api.register(app, identify)
    return app


class Net:
    """Node A (the one that holds data) behind its API; node B is whatever blobs.get_store() returns by default."""

    def __init__(self, a: BlobStore, b: BlobStore, monkeypatch) -> None:
        from bot.dashboard import cluster_api
        monkeypatch.setattr(cluster_api, "_peer_name", lambda key: "NodeB")
        self.a, self.b, self.calls, self.current = a, b, [], b
        self.client = TestClient(_app("peer"))
        self.tamper = None                                         # fn(path, doc) -> doc | raises
        monkeypatch.setattr(blobs, "get_store", lambda: self.current)
        monkeypatch.setattr(blobs, "peer_remote", self.remote)
        monkeypatch.setattr(blobs, "MIN_CHUNK_BYTES", 16)

    def remote(self, node, method, path):
        self.calls.append(path)
        self.current = self.a                                       # the request is served by node A
        try:
            r = self.client.request(method, "/api/cluster" + path)
        finally:
            self.current = self.b
        if r.status_code >= 400:
            raise RuntimeError(f"{node}: HTTP {r.status_code}")
        doc = r.json()
        return self.tamper(path, doc) if self.tamper else doc

    def chunk_calls(self):
        return [c for c in self.calls if "/chunks/" in c]


@pytest.fixture
def net(tmp_path, monkeypatch):
    from bot import cache
    monkeypatch.setattr(cache, "available", lambda: False)
    a, b = BlobStore(tmp_path / "a"), BlobStore(tmp_path / "b")
    return Net(a, b, monkeypatch)


def test_put_get_round_trip_and_dedup(net):
    ref = net.a.put_bytes(DATA, chunk_bytes=CHUNK)
    assert ref["size"] == len(DATA) and len(ref["hash"]) == 64
    assert net.a.put_bytes(DATA, chunk_bytes=CHUNK) == ref and net.a.summary()["count"] == 1
    net.b.ensure(ref["hash"], ["NodeA"])
    assert net.b.path(ref["hash"]).read_bytes() == DATA
    m = net.b.manifest(ref["hash"])
    assert m == net.a.manifest(ref["hash"]) and len(m["chunks"]) == 6
    out = net.b.materialize(ref["hash"], net.b.root / "job" / "in" / "x.bin")
    assert out.read_bytes() == DATA
    assert BlobStore(net.b.root).has(ref["hash"])                  # survives a restart


def test_a_corrupt_chunk_is_refetched_and_a_lasting_one_refused(net):
    ref = net.a.put_bytes(DATA, chunk_bytes=CHUNK)
    seen = {"n": 0}

    def flip_once(path, doc):
        if path.endswith("/chunks/2") and seen["n"] == 0:
            seen["n"] += 1
            raw = bytearray(base64.b64decode(doc["data"]))
            raw[0] ^= 1
            doc["data"] = base64.b64encode(bytes(raw)).decode()
        return doc
    net.tamper = flip_once
    net.b.ensure(ref["hash"], ["NodeA"])
    assert net.b.path(ref["hash"]).read_bytes() == DATA
    assert net.b.stats["chunks_refetched"] == 1

    other = net.b.root.parent / "c"
    c = BlobStore(other)
    net.tamper = lambda path, doc: {**doc, "data": base64.b64encode(b"x" * CHUNK).decode()} if "/chunks/1" in path else doc
    net.current = net.b
    with pytest.raises(BlobError, match="chunk 1"):
        c.ensure(ref["hash"], ["NodeA"], blobs.peer_remote)
    assert not c.has(ref["hash"])


def test_a_manifest_that_lies_is_caught_by_the_whole_hash(net):
    ref = net.a.put_bytes(DATA, chunk_bytes=CHUNK)
    evil = bytearray(DATA)
    evil[10] ^= 0xFF
    evil = bytes(evil)
    bad_chunks = [blobs._sha(evil[i:i + CHUNK]) for i in range(0, len(evil), CHUNK)]

    def fake(node, method, path):
        if path.endswith(ref["hash"]):
            return {"hash": ref["hash"], "size": len(evil), "chunk_bytes": CHUNK, "chunks": bad_chunks}
        i = int(path.rsplit("/", 1)[1])
        piece = evil[i * CHUNK:(i + 1) * CHUNK]
        return {"data": base64.b64encode(piece).decode()}
    with pytest.raises(BlobError, match="does not match its hash"):
        net.b.ensure(ref["hash"], ["NodeA"], fake)
    assert not net.b.has(ref["hash"]) and not list((net.b.root / "partial").glob("*.part"))


def test_a_fetch_resumes_without_asking_for_verified_chunks_again(net):
    ref = net.a.put_bytes(DATA, chunk_bytes=CHUNK)

    def cut(path, doc):
        if path.endswith("/chunks/3"):
            raise RuntimeError("connection dropped")
        return doc
    net.tamper = cut
    with pytest.raises(BlobError, match="chunk 3"):
        net.b.ensure(ref["hash"], ["NodeA"])
    assert not net.b.has(ref["hash"])
    net.calls.clear()
    net.tamper = None
    net.b.ensure(ref["hash"], ["NodeA"])
    asked = sorted(c.rsplit("/", 1)[1] for c in net.chunk_calls())
    assert asked == ["3", "4", "5"]
    assert net.b.path(ref["hash"]).read_bytes() == DATA


def test_only_authenticated_peers_read_blobs_and_only_by_hash(net, monkeypatch):
    ref = net.a.put_bytes(DATA, chunk_bytes=CHUNK)
    net.current = net.a
    h = ref["hash"]
    assert TestClient(_app("none")).get(f"/api/cluster/blobs/{h}").status_code == 401
    assert TestClient(_app("mobile")).get(f"/api/cluster/blobs/{h}").status_code == 403
    assert TestClient(_app("bridge")).get(f"/api/cluster/blobs/{h}").status_code == 403
    from bot.dashboard import cluster_api
    monkeypatch.setattr(cluster_api, "_peer_name", lambda key: None)
    assert TestClient(_app("peer")).get(f"/api/cluster/blobs/{h}").status_code == 403
    monkeypatch.setattr(cluster_api, "_peer_name", lambda key: "NodeB")
    peer = TestClient(_app("peer"))
    assert peer.get(f"/api/cluster/blobs/{h}").json()["size"] == len(DATA)
    assert peer.head(f"/api/cluster/blobs/{h}").status_code == 200
    assert peer.get(f"/api/cluster/blobs/{h}/chunks/0").json()["chunk_hash"] == net.a.manifest(h)["chunks"][0]
    for bad in ("nothex", "../../etc/passwd", "g" * 64, "a" * 63, "a" * 65):
        assert peer.get(f"/api/cluster/blobs/{bad}").status_code in (400, 404)
    assert peer.get("/api/cluster/blobs/" + "a" * 64).status_code == 404
    assert peer.get("/api/cluster/blobs/" + "a" * 63).status_code == 400
    assert peer.get(f"/api/cluster/blobs/{h}/chunks/99").status_code == 404
    assert peer.get(f"/api/cluster/blobs/{h}/chunks/-1").status_code == 404
    assert peer.put(f"/api/cluster/blobs/{h}", json={}).status_code == 405      # nothing is written through the API
    with pytest.raises(BlobError):
        net.a.path("../x")


def test_lru_eviction_never_drops_a_pinned_blob(tmp_path):
    s = BlobStore(tmp_path / "s", cap_bytes=300)
    refs = []
    for i in range(3):
        refs.append(s.put_bytes(bytes([i]) * 100)["hash"])
        time.sleep(0.03)
    assert s.summary()["count"] == 3
    s.pin(refs[0], "job-1")                                          # the oldest, but a running job uses it
    d = s.put_bytes(b"d" * 100)["hash"]
    assert s.has(refs[0]) and not s.has(refs[1]) and s.has(refs[2]) and s.has(d)
    assert s.stats["evictions"] == 1
    time.sleep(0.03)
    s.path(refs[2])                                                   # a read refreshes recency
    time.sleep(0.03)
    e = s.put_bytes(b"e" * 100)["hash"]
    assert s.has(refs[0]) and not s.has(d) and s.has(refs[2]) and s.has(e)
    s.unpin_owner("job-1")
    s.put_bytes(b"f" * 100)
    assert not s.has(refs[0])
    with pytest.raises(BlobError, match="more than"):
        s.put_bytes(b"z" * 301)
    full = BlobStore(tmp_path / "t", cap_bytes=200)                   # everything pinned: over the cap, nothing dropped
    x, y, z = (full.put_bytes(bytes([i]) * 100)["hash"] for i in (1, 2, 3))
    assert not full.has(x)
    full.pin(y, "j")
    full.pin(z, "j")
    full.put_bytes(b"w" * 100)
    assert full.has(y) and full.has(z)


def test_cacheit_absent_works_and_present_is_a_mirror(net, monkeypatch):
    from bot import cache
    ref = net.a.put_bytes(b"small blob")
    net.b.ensure(ref["hash"], ["NodeA"])
    assert net.b.path(ref["hash"]).read_bytes() == b"small blob"
    kv = {}
    monkeypatch.setattr(cache, "available", lambda: True)
    monkeypatch.setattr(cache, "put", lambda ns, k, v, **kw: kv.__setitem__((ns, k), v) or True)
    monkeypatch.setattr(cache, "get", lambda ns, k: kv.get((ns, k)))
    c = BlobStore(net.b.root.parent / "c")
    r = c.put_bytes(b"mirrored")
    assert kv[("cluster-blob", r["hash"])] == b"mirrored"
    d = BlobStore(net.b.root.parent / "d")
    net.calls.clear()
    d.ensure(r["hash"], ["NodeA"])                                    # served by the cache, no peer asked
    assert d.path(r["hash"]).read_bytes() == b"mirrored" and net.calls == [] and d.stats["cache_hits"] == 1


# ---- jobs --------------------------------------------------------------------------------------------------------------
@pytest.fixture
def cluster(tmp_path, monkeypatch):
    cfg = {"enabled": True, "cpu_percent": 100, "ram_gb": 2, "max_jobs": 3, "disk_gb": 5, "work_dir": str(tmp_path / "work")}
    monkeypatch.setattr(offer, "_cfg", lambda: cfg)
    monkeypatch.setattr(offer.Budget, "capacity", lambda self, o=None: {
        "cpu": 2.0, "ram_gb": 2.0, "gpus": [], "vram_gb": {}, "disk_gb": 5.0, "max_jobs": 3})
    monkeypatch.setattr(offer, "budget", Budget())
    monkeypatch.setattr(executor, "budget", offer.budget)
    monkeypatch.setattr(membership, "nodes", lambda include_self=True: [
        {"name": "self", "peer": None, "health": "ok", "latency_ms": 0, "info": membership.this_node()}])
    store.use_path(tmp_path / "cluster.sqlite3")
    yield cfg
    for r in store.list_("runs", states=store.ACTIVE, limit=100):
        executor.cancel(r["id"])
    store.use_path(None)


def _wait_run(rid: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = executor.get(rid)
        if j["state"] not in store.ACTIVE:
            return j
        time.sleep(0.1)
    raise AssertionError(f"{rid} still {executor.get(rid)['state']}")


READER = ("import os, hashlib\n"
          "d = open(os.path.join(os.environ['CLUSTER_IN_DIR'], 'sub', 'data.bin'), 'rb').read()\n"
          "open(os.path.join(os.environ['CLUSTER_OUT_DIR'], 'digest.txt'), 'w').write(hashlib.sha256(d).hexdigest())\n")


def test_a_second_job_reuses_the_input_and_outputs_come_back_as_blobs(net, cluster):
    ref = net.a.put_bytes(DATA, chunk_bytes=CHUNK)
    doc = {"kind": "python", "spec": {"code": READER}, "outputs": "all",
           "inputs": {"sub/data.bin": ref}, "submitter": {"node": "NodeA"}}
    j1 = _wait_run(executor.accept({**doc, "id": "job-1"})["id"])
    assert j1["state"] == "done", j1
    assert len(net.chunk_calls()) == 6                                # the input went across once...
    digest = net.b.path(j1["output_blobs"]["digest.txt"]["hash"]).read_text()
    assert digest == blobs._sha(DATA)
    j2 = _wait_run(executor.accept({**doc, "id": "job-2"})["id"])
    assert j2["state"] == "done"
    assert len(net.chunk_calls()) == 6                                # ...and the second job used the copy it left
    assert net.b.stats["fetches"] == 1 and net.b.stats["hits"] >= 1
    assert "already here" in executor.logs("job-2")["text"]
    assert not net.b._pins                                            # nothing stays pinned after the jobs end


def test_inputs_are_checked_and_an_unfetchable_input_fails_the_job(net, cluster):
    ok = {"hash": "a" * 64}
    for name in ("../x", "/abs", "a/../../b", "C:/x", ""):
        with pytest.raises(JobError):
            executor.accept({"id": "bad", "kind": "python", "spec": {"code": "1"}, "inputs": {name: ok}})
    with pytest.raises(JobError):
        executor.accept({"id": "bad2", "kind": "python", "spec": {"code": "1"}, "inputs": {"x": {"hash": "nothex"}}})
    with pytest.raises(JobError):
        executor.accept({"id": "bad3", "kind": "python", "spec": {"code": "1"}, "outputs": 5})
    j = _wait_run(executor.accept({"id": "missing", "kind": "python", "spec": {"code": "1"}, "inputs": {"x": ok},
                                   "submitter": {"node": "NodeA"}})["id"])
    assert j["state"] == "failed" and "input x" in j["error"]


def test_submit_puts_files_in_the_store_and_returns_output_hashes(net, cluster, tmp_path):
    f = tmp_path / "in.txt"
    f.write_bytes(DATA)
    code = ("import os\nd = open(os.path.join(os.environ['CLUSTER_IN_DIR'], 'in.txt'), 'rb').read()\n"
            "open(os.path.join(os.environ['CLUSTER_OUT_DIR'], 'n.txt'), 'w').write(str(len(d)))\n")
    import asyncio

    async def go():
        p = await scheduler.submit({"kind": "python", "spec": {"code": code}, "outputs": ["n.txt"],
                                    "inputs": {"in.txt": {"path": str(f)}, "note.txt": {"text": "hi"}}})
        assert p["inputs"]["in.txt"]["hash"] == blobs._sha(DATA) and p["data"][blobs._sha(DATA)] == len(DATA)
        for _ in range(100):
            p = await scheduler.refresh(p["id"])
            if p["state"] == "done":
                return p
            await asyncio.sleep(0.1)
        raise AssertionError(p["state"])
    p = asyncio.run(go())
    assert net.b.path(p["output_blobs"]["n.txt"]["hash"]).read_text() == str(len(DATA))
    assert net.b.has(blobs._sha(b"hi")) and not net.b._pins
    assert net.calls == []                                            # the submitter already held it


# ---- placement and status ---------------------------------------------------------------------------------------------
def _node(name, held, cpu_free):
    info = {"available": True, "offer": {"kinds": ["python"]}, "static": {"os": "linux"},
            "free": {"slots": 2, "cpu": cpu_free, "ram_gb": 8, "disk_gb": 50}, "capacity": {"cpu": 8, "ram_gb": 8},
            "blobs": {"held": held}}
    return {"name": name, "peer": name, "health": "ok", "latency_ms": 1, "route": None, "info": info}


def test_the_scheduler_prefers_the_node_holding_the_data(monkeypatch):
    h1, h2 = "1" * 64, "2" * 64
    emptier = _node("emptier", [], 8)
    holder = _node("holder", [h1[:12]], 2)
    monkeypatch.setattr(membership, "nodes", lambda include_self=True: [emptier, holder])
    req = offer.Request.from_dict({"cpu": 1})
    assert [n["name"] for n in scheduler.candidates(req, "python")[0]] == ["emptier", "holder"]
    assert [n["name"] for n in scheduler.candidates(req, "python", data={h1: 1000})[0]] == ["holder", "emptier"]
    assert [n["name"] for n in scheduler.candidates(req, "python", data={h2: 1000})[0]] == ["emptier", "holder"]
    both = _node("both", [h1[:12], h2[:12]], 1)
    monkeypatch.setattr(membership, "nodes", lambda include_self=True: [emptier, holder, both])
    order = [n["name"] for n in scheduler.candidates(req, "python", data={h1: 1000, h2: 5000})[0]]
    assert order[0] == "both"                                         # most input bytes held wins
    assert [n["name"] for n in scheduler.candidates(req, "python", data={})[0]][0] == "emptier"   # ties unchanged


def test_status_reports_the_blob_store(net, cluster):
    ref = net.b.put_bytes(DATA, chunk_bytes=CHUNK)
    info = membership.this_node()["blobs"]
    assert info["count"] == 1 and info["bytes"] == len(DATA) and info["cap_bytes"] > 0
    assert ref["hash"][:12] in info["held"] and blobs.holds(info["held"], ref["hash"])
    assert not blobs.holds(info["held"], "f" * 64)
    from bot.cluster import tools
    assert tools._node_summary({"name": "n", "peer": None, "health": "ok", "info": membership.this_node()})["blobs"]["count"] == 1
