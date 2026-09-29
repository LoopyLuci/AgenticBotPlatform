"""The cluster (bot/cluster/): offers and budgets, running real jobs under hard caps, scheduling, gangs, arrays,
and the API's rules about who may do what. Runs on this machine as a one-node cluster."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bot.cluster import executor, membership, offer, scheduler, store
from bot.cluster.executor import JobError
from bot.cluster.offer import Budget, Request
from bot.cluster.scheduler import ScheduleError

PY = sys.executable


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    """A one-node cluster (this machine) sharing 2 threads, 2 GB and 3 jobs, with its store and work folder in tmp."""
    cfg = {"enabled": True, "cpu_percent": 100, "ram_gb": 2, "max_jobs": 3, "disk_gb": 5,
           "work_dir": str(tmp_path / "work")}
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


# ---- offers -------------------------------------------------------------------------------------------------------------
def test_sharing_is_off_until_the_owner_turns_it_on(monkeypatch):
    monkeypatch.setattr(offer, "_cfg", lambda: {})
    assert offer.current()["enabled"] is False
    ok, why = offer.available_now()
    assert not ok and "not turned sharing on" in why


@pytest.mark.parametrize("changes, message", [
    ({"cpu_percent": 150}, "0-100"), ({"ram_gb": -1}, "negative"), ({"gpus": "some"}, "GPU indices"),
    ({"kinds": ["shell"]}, "kinds are from"), ({"when": "weekends"}, "always"), ({"nope": 1}, "unknown"),
])
def test_bad_offer_edits_are_refused(changes, message):
    with pytest.raises(ValueError, match=message):
        offer.validate(changes)


def test_the_budget_never_double_books(cluster):
    b = offer.budget
    results = []

    def grab(i):
        results.append(b.try_reserve(f"j{i}", Request(cpu=1, ram_gb=1))[0])
    threads = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 2                      # 2 threads and 2 GB: exactly two 1/1 jobs fit
    assert b.free()["cpu"] == 0 and b.free()["ram_gb"] == 0
    ok, why, _ = b.try_reserve("j9", Request(cpu=0.5, ram_gb=0.5))
    assert not ok and "free" in why
    for i in range(8):
        b.release(f"j{i}")
    assert b.free()["cpu"] == 2


def test_refusals_say_why(cluster):
    cluster["kinds"] = ["command"]
    assert "does not run python jobs" in offer.budget.fits(Request(), kind="python")[1]
    assert "needs 9 CPU threads" in offer.budget.fits(Request(cpu=9))[1]
    assert "lacks nosuchtool" in offer.budget.fits(Request(needs=["nosuchtool"]))[1]
    cluster["peers"] = ["Server"]
    assert "does not share with Laptop" in offer.budget.fits(Request(), peer="Laptop")[1]
    assert offer.budget.fits(Request(), peer="server")[0]


# ---- running jobs ------------------------------------------------------------------------------------------------------
def test_a_command_job_runs_in_its_own_folder_with_a_clean_environment(cluster, monkeypatch):
    monkeypatch.setenv("ABP_TEST_SECRET", "unused")
    code = ("import os, pathlib; print('hello', os.environ.get('CLUSTER_JOB_ID')); "
            "print('secret:', os.environ.get('ABP_TEST_SECRET')); "
            "pathlib.Path(os.environ['CLUSTER_OUT_DIR'], 'result.txt').write_text('42')")
    j = executor.accept({"id": "cmd1", "kind": "command", "spec": {"argv": [PY, "-c", code]}, "req": {"cpu": 1}})
    assert j["state"] in ("starting", "running", "done")
    j = _wait_run("cmd1")
    assert j["state"] == "done" and j["exit_code"] == 0
    log = executor.logs("cmd1")["text"]
    assert "hello cmd1" in log and "secret: None" in log            # ABP's environment never reaches a job
    assert j["files"] == [{"name": "result.txt", "size": 2}]
    assert executor.file_path("cmd1", "result.txt").read_text() == "42"
    with pytest.raises(JobError):
        executor.file_path("cmd1", "../job.log")
    assert offer.budget.free()["slots"] == 3                          # released


def test_a_python_job_and_the_same_job_sent_twice(cluster):
    doc = {"id": "py1", "kind": "python", "spec": {"code": "print(sum(range(10)))"}}
    executor.accept(doc)
    assert executor.accept(doc)["id"] == "py1"                        # idempotent: not run twice
    assert _wait_run("py1")["state"] == "done"
    assert "45" in executor.logs("py1")["text"]


@pytest.mark.skipif(os.name != "nt", reason="the memory cap is a Windows job object here")
def test_the_memory_cap_is_enforced(cluster):
    code = "x = bytearray(600 * 1024 * 1024); print('allocated')"
    executor.accept({"id": "mem1", "kind": "python", "spec": {"code": code}, "req": {"cpu": 1, "ram_gb": 0.25}})
    j = _wait_run("mem1")
    log = executor.logs("mem1")["text"]
    assert j["state"] == "failed" and "MemoryError" in log
    assert "allocated" not in [line.strip() for line in log.splitlines()]      # the print never ran


def test_timeouts_and_cancels_stop_the_whole_job(cluster):
    executor.accept({"id": "slow1", "kind": "python", "spec": {"code": "import time; time.sleep(60)"}, "timeout_s": 1})
    j = _wait_run("slow1", 30)
    assert j["state"] == "failed" and "timed out" in j["error"]
    executor.accept({"id": "slow2", "kind": "python", "spec": {"code": "import time; time.sleep(60)"}})
    time.sleep(0.5)
    executor.cancel("slow2")
    assert _wait_run("slow2", 30)["state"] == "cancelled"


def test_held_jobs_wait_for_start(cluster):
    executor.accept({"id": "held1", "kind": "python", "spec": {"code": "print('go')"}, "hold": True})
    assert executor.get("held1")["state"] == "held"
    assert offer.budget.free()["slots"] == 2                           # reserved while held
    executor.start("held1")
    assert _wait_run("held1")["state"] == "done"


def test_bad_jobs_are_refused_before_anything_runs(cluster):
    for doc, msg in [({"id": "b1", "kind": "shell", "spec": {}}, "unknown job kind"),
                     ({"id": "b2", "kind": "command", "spec": {"argv": "rm -rf /"}}, "list of strings"),
                     ({"id": "../x", "kind": "python", "spec": {"code": "1"}}, "job id"),
                     ({"id": "b3", "kind": "python", "spec": {"code": "1"}, "env": {"BAD KEY": "1"}}, "variable names")]:
        with pytest.raises(JobError, match=msg):
            executor.accept(doc)
    cluster["enabled"] = False
    with pytest.raises(JobError, match="not turned sharing on") as e:
        executor.accept({"id": "b4", "kind": "python", "spec": {"code": "1"}})
    assert e.value.status == 409


# ---- scheduling --------------------------------------------------------------------------------------------------------
def test_submit_places_on_the_node_that_fits_and_says_why_others_cannot(cluster):
    p = asyncio.run(scheduler.submit({"kind": "python", "spec": {"code": "print('placed')"}, "req": {"cpu": 1}}))
    assert p["node"] == "self" and p["run_id"].endswith("-a1")
    p = asyncio.run(scheduler.wait(p["id"], 30, 0.1))
    assert p["state"] == "done"
    with pytest.raises(ScheduleError) as e:
        asyncio.run(scheduler.submit({"kind": "python", "spec": {"code": "1"}, "req": {"gpus": 1}}))
    assert "self" in e.value.reasons and "GPU" in e.value.reasons["self"]


def test_a_gang_gets_rank_world_size_and_a_master(cluster):
    code = "import os; print(os.environ['RANK'], os.environ['WORLD_SIZE'], os.environ['MASTER_PORT'])"
    g = asyncio.run(scheduler.submit_group({"kind": "python", "spec": {"code": code}, "replicas": 1}))
    assert g["state"] == "running" and len(g["members"]) == 1
    p = asyncio.run(scheduler.wait(g["members"][0], 30, 0.1))
    assert p["state"] == "done"
    assert "0 1 29" in executor.logs(p["run_id"])["text"]
    with pytest.raises(ScheduleError, match="2 nodes are needed"):
        asyncio.run(scheduler.submit_group({"kind": "python", "spec": {"code": "1"}, "replicas": 2}))


def test_an_array_spreads_its_tasks_and_gathers_the_results(cluster):
    code = "import os; print('task', os.environ['CLUSTER_TASK_INDEX'], 'of', os.environ['CLUSTER_TASK_COUNT'])"
    g = asyncio.run(scheduler.submit_array({"kind": "python", "spec": {"code": code}, "count": 5, "max_parallel": 2}))
    deadline = time.time() + 60
    while time.time() < deadline:
        asyncio.run(scheduler.supervise_once())
        g = store.get("groups", g["id"])
        if g["state"] != "running":
            break
        time.sleep(0.2)
    res = scheduler.group_results(g["id"])
    assert res["state"] == "done" and len(res["results"]) == 5
    assert all(r["state"] == "done" for r in res["results"])


# ---- the API ------------------------------------------------------------------------------------------------------------
def _app(kind: str):
    from bot.dashboard import cluster_api
    app = FastAPI()

    def identify():
        if kind == "none":
            raise HTTPException(status_code=401, detail="no")
        return kind
    cluster_api.register(app, identify)
    return app


def test_only_the_owner_changes_the_offer_and_peers_see_only_their_own_jobs(cluster, monkeypatch):
    from bot.dashboard import cluster_api
    monkeypatch.setattr(cluster_api, "_peer_name", lambda key: "Laptop")
    peer, owner = TestClient(_app("peer")), TestClient(_app("dashboard"))
    assert peer.get("/api/cluster/node").json()["available"] is True
    assert peer.put("/api/cluster/offer", json={"enabled": False}).status_code == 403
    assert TestClient(_app("mobile")).put("/api/cluster/offer", json={"enabled": False}).status_code == 403
    assert peer.post("/api/cluster/jobs", json={"kind": "python", "spec": {"code": "1"}}).status_code == 403
    r = peer.post("/api/cluster/runs", json={"id": "fromlaptop", "kind": "python", "spec": {"code": "print(1)"}})
    assert r.status_code == 200 and r.json()["peer"] == "Laptop"
    executor.accept({"id": "mine", "kind": "python", "spec": {"code": "print(2)"}})
    assert [x["id"] for x in peer.get("/api/cluster/runs").json()] == ["fromlaptop"]
    assert peer.get("/api/cluster/runs/mine").status_code == 404
    assert {x["id"] for x in owner.get("/api/cluster/runs").json()} == {"fromlaptop", "mine"}
    cluster["enabled"] = False
    r = peer.post("/api/cluster/runs", json={"id": "refused", "kind": "python", "spec": {"code": "1"}})
    assert r.status_code == 409 and "sharing" in r.json()["detail"]["error"]


def test_the_owner_submits_and_follows_a_job(cluster):
    c = TestClient(_app("dashboard"))
    p = c.post("/api/cluster/jobs", json={"kind": "python", "spec": {"code": "print('via api')"}}).json()
    for _ in range(100):
        p = c.get(f"/api/cluster/jobs/{p['id']}").json()
        if p["state"] == "done":
            break
        time.sleep(0.1)
    assert p["state"] == "done"
    assert "via api" in c.get(f"/api/cluster/jobs/{p['id']}/logs").json()["text"]
    assert c.post("/api/cluster/jobs", json={"kind": "python", "spec": {}}).status_code == 400
    r = c.post("/api/cluster/jobs", json={"kind": "python", "spec": {"code": "1"}, "req": {"cpu": 99}})
    assert r.status_code == 409 and "self" in r.json()["detail"]["reasons"]


def test_peer_links_can_reach_the_cluster_routes():
    from bot import peers
    assert "/api/cluster/node".startswith(peers.CLUSTER_PREFIX)


def test_panel_is_identical_in_both_uis_and_wired():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    a = (root / "bot/dashboard/static/cluster-panel.js").read_text(encoding="utf-8")
    b = (root / "desktop-app/ui/cluster-panel.js").read_text(encoding="utf-8")
    assert a == b
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (root / page).read_text(encoding="utf-8")
        assert 'id="cluster"' in html and 'id="clp-root"' in html and "cluster-panel.js" in html
