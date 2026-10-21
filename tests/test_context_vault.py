"""The context vault (bot/context_vault): tiers, placement, promotion, demotion, crash recovery, dedup, redaction,
passive capture, retrieval, injection and the surfaces. Everything runs in tmp dirs with fake models and embedders."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from bot import context_vault
from bot.agent_runtime import compression
from bot.agent_runtime.transports.base import NormalizedResponse
from bot.config import config
from bot.context_vault import capture, settings, tools as vtools
from bot.context_vault.store import Vault, est_tokens
from bot import db


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def cfg_for(tmp_path, tiers=None, **over):
    base = {
        "enabled": True,
        "tiers": tiers if tiers is not None else [
            {"name": "ram", "kind": "ram", "max_bytes": 100_000},
            {"name": "nvme", "kind": "dir", "path": str(tmp_path / "nvme"), "max_bytes": 0},
            {"name": "ssd", "kind": "dir", "path": str(tmp_path / "ssd"), "max_bytes": 0},
            {"name": "hdd", "kind": "dir", "path": str(tmp_path / "hdd"), "max_bytes": 0},
        ],
        "min_residency_s": 0,
    }
    base.update(over)
    return settings.load(base)


def make(tmp_path, tiers=None, clock=None, embedder=None, **over) -> Vault:
    return Vault(cfg_for(tmp_path, tiers, **over), tmp_path / "idx" / "index.db", embedder=embedder,
                 clock=clock or Clock())


def tier_files(tmp_path, name):
    return [p for p in (tmp_path / name).glob("*/*") if p.is_file()]


def unique(i, words=12):
    return " ".join(f"w{i}x{j}" for j in range(words))


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """An enabled vault installed as the process's vault, config switched on."""
    monkeypatch.setitem(config._data, "context_vault", {"enabled": True})
    v = make(tmp_path)
    context_vault.set_vault(v)
    yield v
    context_vault.set_vault(None)
    v.close()


class FakeTransport:
    def __init__(self, provider_key, digest="a compact summary"):
        self.provider_key = provider_key
        self.digest = digest

    def user_message(self, text, *, images=None):
        return {"role": "user", "content": text}

    async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
        return NormalizedResponse(text=self.digest, assistant_message={"role": "assistant", "content": self.digest})


# ---- disabled by default ---------------------------------------------------------------------------------------------
def test_off_by_default_and_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", lambda: tmp_path / "data")
    assert settings.load()["enabled"] is False
    assert context_vault.get_vault() is None
    capture.on_compaction("s", FakeTransport("anthropic"), "m", "We decided to use sqlite for the index.")
    capture.on_turn_end([], FakeTransport("anthropic"), "m", "We decided to use sqlite for the index.")
    capture.on_board_event("created", {"title": "t", "detail": "d"})
    capture.on_step_result("goal", "ok", "result text")
    assert asyncio.run(vtools.injection_for_turn("anything", FakeTransport("ollama"))) == ""
    assert context_vault.get_vault() is None
    assert not (tmp_path / "data").exists()


# ---- placement, promotion, demotion ----------------------------------------------------------------------------------
def test_new_items_go_to_the_top_storage_tier(tmp_path):
    v = make(tmp_path)
    r = v.add("We decided to keep the index in SQLite.", "decision")
    assert r["status"] == "stored" and r["tier"] == "nvme"
    assert len(tier_files(tmp_path, "nvme")) == 1 and not tier_files(tmp_path, "ssd")
    assert v.stats()["tiers"][0]["items"] == 0          # RAM is a cache of read items, empty until something is read
    v.close()


def test_a_read_item_is_copied_up_to_ram_and_moved_up_a_tier(tmp_path):
    v = make(tmp_path)
    iid = v.add("Always run the linter before committing.", "rule")["id"]
    assert v.demote(iid) and v.get(iid, touch=False)["tier"] == "ssd"
    assert v.demote(iid) and v.get(iid, touch=False)["tier"] == "hdd"
    got = v.recall("linter committing", k=3)
    assert [g["id"] for g in got] == [iid]
    v.drain()
    item = v.get(iid, touch=False)
    assert item["tier"] == "ssd" and item["hot"] is True        # one tier up, and cached in RAM
    v.recall("linter committing")
    v.drain()
    assert v.get(iid, touch=False)["tier"] == "nvme"
    assert len(tier_files(tmp_path, "nvme")) == 1 and not tier_files(tmp_path, "ssd") and not tier_files(tmp_path, "hdd")
    c = v.stats()["counters"]
    assert c["promoted"] == 2 and c["reads_cold"] >= 1
    v.recall("linter committing")
    assert v.stats()["counters"]["reads_hot"] >= 1
    v.close()


def test_a_tier_over_its_cap_sheds_down_and_cascades(tmp_path):
    tiers = [
        {"name": "ram", "kind": "ram", "max_bytes": 1000},
        {"name": "nvme", "kind": "dir", "path": str(tmp_path / "nvme"), "max_bytes": 400},
        {"name": "ssd", "kind": "dir", "path": str(tmp_path / "ssd"), "max_bytes": 800},
        {"name": "hdd", "kind": "dir", "path": str(tmp_path / "hdd"), "max_bytes": 0},
    ]
    clock = Clock()
    v = make(tmp_path, tiers, clock)
    ids = []
    for i in range(20):
        text = unique(i, 12)                                       # about 100 bytes each, all different
        ids.append(v.add(text, "note", importance=0.9 if i == 19 else 0.3)["id"])
        clock.t += 10
    v.drain()
    s = {t["name"]: t for t in v.stats()["tiers"]}
    assert s["nvme"]["bytes"] <= 400 and s["ssd"]["bytes"] <= 800
    assert s["hdd"]["items"] > 0 and s["ssd"]["items"] > 0
    assert s["nvme"]["items"] + s["ssd"]["items"] + s["hdd"]["items"] == 20
    assert v.get(ids[19], touch=False)["tier"] == "nvme"           # the important and newest item stays on top
    assert v.stats()["counters"]["demoted"] >= 10
    # bodies really moved: the files on disk match the index
    assert len(tier_files(tmp_path, "hdd")) == s["hdd"]["items"]
    v.close()


def test_the_last_tier_evicts_the_least_important_oldest_only_at_its_own_cap(tmp_path, caplog):
    tiers = [{"name": "only", "kind": "dir", "path": str(tmp_path / "only"), "max_bytes": 450}]
    clock = Clock()
    v = make(tmp_path, tiers, clock)
    keep = v.add(unique(1), "decision", importance=0.95)["id"]
    for i in range(2, 9):
        clock.t += 5
        v.add(unique(i), "note", importance=0.2 + i / 100)
    with caplog.at_level("INFO", logger="bot.context_vault"):
        v.rebalance()
    assert v.get(keep, touch=False) is not None
    assert v.stats()["tiers"][0]["bytes"] <= 450
    assert v.stats()["counters"]["evicted"] >= 1
    assert "dropping item" in caplog.text
    v.close()
    # without a cap nothing is ever evicted
    v2 = make(tmp_path / "b")
    for i in range(30):
        v2.add(unique(i), "note")
    v2.rebalance()
    assert v2.stats()["counters"]["evicted"] == 0 and v2.stats()["items"] == 30
    v2.close()


# ---- crash safety ----------------------------------------------------------------------------------------------------
class Boom(Exception):
    pass


def _crash_at(stage):
    def hook(name):
        if name == stage:
            raise Boom(name)
    return hook


def test_recovery_after_a_crash_between_copy_and_index_update(tmp_path):
    v = make(tmp_path)
    iid = v.add("Decision: the cold tier never evicts below its cap.", "decision")["id"]
    v._fault = _crash_at("after_copy")
    with pytest.raises(Boom):
        v.demote(iid)
    assert tier_files(tmp_path, "nvme") and tier_files(tmp_path, "ssd")      # both copies exist, row says moving
    assert v._q("SELECT state FROM items")[0]["state"] == "moving"
    v.close()

    v2 = make(tmp_path)                                                      # recovery runs on open
    row = v2._q("SELECT tier, state FROM items")[0]
    assert (row["tier"], row["state"]) == ("ssd", "ok")                      # the verified copy won
    assert not tier_files(tmp_path, "nvme") and len(tier_files(tmp_path, "ssd")) == 1
    assert "cold tier never evicts" in v2.get(iid)["text"]
    v2.close()


def test_recovery_rolls_back_when_the_copy_is_damaged_and_sweeps_leftovers(tmp_path):
    v = make(tmp_path)
    iid = v.add("Plan: migrate the tiers one at a time.", "plan")["id"]
    v._fault = _crash_at("after_copy")
    with pytest.raises(Boom):
        v.demote(iid)
    v.close()
    dest = tier_files(tmp_path, "ssd")[0]
    dest.write_bytes(b"torn")                                                # a half-written destination
    (tmp_path / "ssd" / "zz").mkdir()
    (tmp_path / "ssd" / "zz" / "junk.part").write_bytes(b"x")
    v2 = make(tmp_path)
    assert v2.get(iid, touch=False)["tier"] == "nvme"
    assert not tier_files(tmp_path, "ssd") and not list((tmp_path / "ssd").glob("*/*.part"))
    v2.close()


def test_a_second_vault_sharing_the_tier_folders_never_sweeps_the_first_ones_items(tmp_path):
    # 2026-10-20: tier folders are absolute paths from the config, so a second ABP (its own data folder, the same config)
    # opened a vault on them, and its start-up sweep deleted the live vault's items as "unknown here".
    live = make(tmp_path)
    ids = [live.add(f"Decision {i}: keep the tiers apart from the index. " + unique(i), "decision")["id"] for i in range(3)]
    live.close()
    other = Vault(cfg_for(tmp_path), tmp_path / "other-home" / "index.db", clock=Clock())
    other.close()
    assert len(tier_files(tmp_path, "nvme")) == 3                            # nothing of the live vault's was swept
    live = make(tmp_path)
    assert all(live.get(i, touch=False) for i in ids)
    live.close()


def test_an_unmarked_tier_with_foreign_items_is_left_alone_and_a_clean_one_is_claimed(tmp_path):
    v = make(tmp_path)
    v.add("Note: the owner mark is written when the folder is clean.", "note")
    v.close()
    marker = tmp_path / "nvme" / Vault.OWNER_FILE
    assert marker.read_text(encoding="utf-8") == str((tmp_path / "idx" / "index.db").resolve())
    marker.unlink()                                                          # a folder from before the mark existed
    Vault(cfg_for(tmp_path), tmp_path / "fresh" / "index.db", clock=Clock()).close()
    assert len(tier_files(tmp_path, "nvme")) == 1 and not marker.exists()    # foreign item: kept, folder not claimed


def test_recovery_after_the_index_update_removes_the_stale_source(tmp_path):
    v = make(tmp_path)
    iid = v.add("Rule: never store credentials in the vault.", "rule")["id"]
    v._fault = _crash_at("after_index")
    with pytest.raises(Boom):
        v.demote(iid)
    assert tier_files(tmp_path, "nvme") and tier_files(tmp_path, "ssd")
    v.close()
    v2 = make(tmp_path)
    assert not tier_files(tmp_path, "nvme") and v2.get(iid, touch=False)["tier"] == "ssd"
    v2.close()


def test_a_missing_body_is_dropped_not_served(tmp_path):
    v = make(tmp_path)
    iid = v.add("Preference: short commit messages.", "preference")["id"]
    tier_files(tmp_path, "nvme")[0].unlink()
    assert v.get(iid) is None and v.stats()["items"] == 0 and v.stats()["counters"]["lost"] == 1
    v.close()


# ---- dedup -----------------------------------------------------------------------------------------------------------
def test_exact_and_near_duplicates_are_not_stored_twice(tmp_path):
    v = make(tmp_path)
    a = v.add("We decided to use SQLite for the index.", "decision")
    b = v.add("  we decided to USE sqlite   for the index. ", "decision")
    assert a["id"] == b["id"] and b["status"] == "deduped"
    c = v.add("We decided to use SQLite for the index!", "decision")             # same words, other punctuation
    assert c["status"] == "deduped"
    d = v.add("Prefer tabs over spaces in the Makefile.", "preference")
    assert d["status"] == "stored"
    assert v.stats()["items"] == 2 and v.stats()["counters"]["deduped"] == 2
    v.close()


def test_embedding_near_duplicates_are_deduped_when_embeddings_are_on(tmp_path):
    vecs = {"alpha beta gamma delta epsilon one": [1.0, 0.0, 0.1], "completely other wording here two": [0.99, 0.02, 0.1],
            "unrelated text about something else three": [0.0, 1.0, 0.0]}
    v = make(tmp_path, embedder=lambda texts: [vecs[t] for t in texts])
    assert v.add("alpha beta gamma delta epsilon one", "note")["status"] == "stored"
    near = v.add("completely other wording here two", "note")
    assert near["status"] == "deduped" and near.get("near")
    assert v.add("unrelated text about something else three", "note")["status"] == "stored"
    assert v.stats()["items"] == 2
    v.close()


# ---- redaction -------------------------------------------------------------------------------------------------------
def test_a_fake_api_key_is_never_stored(tmp_path, monkeypatch):
    key = "sk-" + "Zq9" * 12
    env_secret = "hunter2-hunter2-hunter2-9981"
    monkeypatch.setenv("MY_SERVICE_API_KEY", env_secret)
    v = make(tmp_path)
    r = v.add(f"We decided to call the API with {key} and also {env_secret} for the deploy.", "decision")
    assert r["status"] == "stored"
    got = v.get(r["id"])["text"]
    assert key not in got and env_secret not in got
    r2 = v.add("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "note")
    assert r2["status"] == "skipped"
    r3 = v.add(f"keys: sk-{'a1' * 20} sk-{'b2' * 20} sk-{'c3' * 20} sk-{'d4' * 20}", "note")
    assert r3["status"] == "skipped"
    v.close()
    blob = b"".join(p.read_bytes() for p in (tmp_path).rglob("*") if p.is_file())
    assert key.encode() not in blob and env_secret.encode() not in blob and b"PRIVATE KEY" not in blob


# ---- passive capture -------------------------------------------------------------------------------------------------
DIGEST = (
    "We decided to use SQLite for the vault index.\n"
    "- Never store credentials in the vault.\n"
    "- bot/context_vault/store.py holds the index and moves items between tiers.\n"
    "Next steps: wire the capture hooks, then write the docs.\n"
    "The build failed with a missing import. Fixed by adding the import to the package init."
)


def test_capture_from_a_real_compaction_with_a_fake_api_model(temp_db, installed, monkeypatch):
    monkeypatch.setattr(compression, "threshold_chars", lambda: 100)
    for i in range(20):
        db.append_agent_message("sess", "user" if i % 2 == 0 else "assistant", f"message {i} " + "x" * 20)
    api = FakeTransport("anthropic", digest=DIGEST)
    assert asyncio.run(compression.maybe_compress("sess", api, model="claude-x", instance_id=7))
    assert installed.stats()["items"] == 0                      # nothing happened on the caller's thread
    installed.drain()                                           # the worker's job
    kinds = {i["kind"] for i in installed.list_items(50)}
    assert {"decision", "rule", "file_summary", "plan", "error_fix", "task_state"} <= kinds
    hit = installed.recall("sqlite index", k=3)
    assert hit and "SQLite" in hit[0]["text"]
    assert "instance:7" in installed.list_items(1)[0]["source"]


def test_a_local_model_compaction_is_not_captured_by_default(temp_db, installed):
    capture.on_compaction("s", FakeTransport("ollama"), "llama", DIGEST)
    installed.drain()
    assert installed.stats()["items"] == 0
    capture.on_compaction("s", FakeTransport("anthropic"), "claude", DIGEST)
    installed.drain()
    assert installed.stats()["items"] > 0


def test_turn_end_captures_error_fix_pairs_and_stated_preferences(installed):
    history = [
        {"role": "user", "content": "Please remember that the tests live in tests/unit. I prefer short answers."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "pytest"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "ImportError: cannot import name widget from helpers"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "The root cause was a renamed helper; I fixed the import."}]},
    ]
    capture.on_turn_end(history, FakeTransport("openai"), "gpt", "Done. We will use the renamed helper from now on.",
                        session_key="s", instance_id=3, cwd="/work/my-project")
    installed.drain()
    items = installed.list_items(20)
    assert any(i["kind"] == "error_fix" for i in items)
    assert any(i["kind"] == "preference" for i in items)
    assert all(i["project"] == "my-project" for i in items)


def test_board_briefs_reviews_and_results_are_captured(installed, tmp_path):
    from bot.coordination import LocalBoard

    board = LocalBoard(tmp_path / "board.db", min_lease_s=0.01)
    try:
        task, _ = board.create_task("lf", "Build the context vault", detail="Tiers RAM NVMe SSD HDD with promotion.", created_by="cond")
        claim = board.claim(task["id"], "worker-1")
        fence = claim.get("fence") or claim["task"]["fence"] if "task" in claim else claim["fence"]
        board.set_state(task["id"], "worker-1", fence, "review", result={"summary": "tiers and capture done"}, note="ready for review")
        board.set_state(task["id"], "worker-1", fence, "done", result={"summary": "merged the vault"})
    finally:
        board.close()
    installed.drain()
    kinds = {i["kind"] for i in installed.list_items(20)}
    assert {"brief", "review", "task_state"} <= kinds
    assert installed.recall("context vault tiers", k=5)


def test_swarm_step_results_are_captured(installed):
    capture.on_step_result("audit the config loader", "ok", "Found two unused keys in loader.py", model="m", index=1)
    installed.drain()
    got = installed.recall("config loader unused keys")
    assert got and got[0]["kind"] == "task_state"


def test_capture_runs_on_the_worker_thread_not_the_caller(installed):
    installed.start()
    try:
        done = threading.Event()
        names = []
        installed.submit(lambda: (names.append(threading.current_thread().name), done.set()))
        assert done.wait(5) and names == ["context-vault"]
    finally:
        installed.stop()


# ---- retrieval -------------------------------------------------------------------------------------------------------
def test_keyword_recall_respects_the_token_budget_and_filters(tmp_path):
    v = make(tmp_path)
    for i in range(8):
        v.add(f"Decision {i}: the scheduler uses lease number {i}. " + " ".join(f"pad{i}x{j}" for j in range(30)), "decision", project="abp")
    v.add("Decision: the scheduler lives elsewhere.", "decision", project="other")
    v.add("Rule: the scheduler must not block the loop. " + "pad " * 20, "rule", project="abp")
    all_hits = v.recall("scheduler lease", k=20)
    assert len(all_hits) >= 8
    budget = 150
    hits = v.recall("scheduler lease", k=20, budget_tokens=budget)
    assert hits and sum(est_tokens(h["text"]) for h in hits) <= budget
    only_rules = v.recall("scheduler", kinds=["rule"], project="abp")
    assert [h["kind"] for h in only_rules] == ["rule"]
    assert all(h["project"] in ("abp", "") for h in v.recall("scheduler", project="abp", k=30))
    assert v.recall("zzzz nothing matches this", k=5) == []
    v.close()


def test_recall_truncates_one_huge_item_to_fit_a_tiny_budget(tmp_path):
    v = make(tmp_path)
    v.add("summary " + "long text body " * 200, "task_state")
    got = v.recall("summary", budget_tokens=50)
    assert len(got) == 1 and est_tokens(got[0]["text"]) <= 51
    v.close()


def test_embeddings_find_meaning_that_keywords_cannot_and_fall_back_when_off(tmp_path):
    vecs = {"The deployment pipeline restarts the service on failure": [1.0, 0.1, 0.0],
            "Bananas are yellow fruit": [0.0, 0.1, 1.0],
            "how do we recover from outages": [0.95, 0.15, 0.0]}
    on = make(tmp_path / "on", embedder=lambda texts: [vecs[t] for t in texts])
    on.add("The deployment pipeline restarts the service on failure", "decision")
    on.add("Bananas are yellow fruit", "note")
    got = on.recall("how do we recover from outages", k=1)
    assert got and "deployment pipeline" in got[0]["text"]
    on.close()

    off = make(tmp_path / "off")
    off.add("The deployment pipeline restarts the service on failure", "decision")
    assert off.recall("how do we recover from outages", k=1) == []             # no shared words, no embeddings
    assert off.recall("deployment pipeline", k=1)                              # keywords still work
    off.close()

    def broken(texts):
        raise RuntimeError("hub down")

    flaky = make(tmp_path / "flaky", embedder=broken)
    flaky.add("The deployment pipeline restarts the service on failure", "decision")
    assert flaky.recall("deployment restarts", k=1)                            # embedder failure -> keyword scoring
    flaky.close()


def test_hit_rate_and_counters_in_status(tmp_path):
    v = make(tmp_path)
    v.add("Decision: use sqlite for the index.", "decision")
    v.recall("sqlite")
    v.recall("nonexistent topic words")
    s = v.stats()
    assert s["hit_rate"] == 0.5 and s["counters"]["captured"] == 1 and s["counters"]["recalls"] == 2
    v.close()


# ---- injection and the tool ------------------------------------------------------------------------------------------
def test_relevant_context_is_injected_for_local_turns_only(installed, monkeypatch):
    installed.add("Decision: the scheduler uses leases with fencing tokens.", "decision")
    local, api = FakeTransport("modelmistress"), FakeTransport("anthropic")
    block = asyncio.run(vtools.injection_for_turn("how does the scheduler use leases", local))
    assert block.startswith("Relevant context") and "fencing tokens" in block
    assert asyncio.run(vtools.injection_for_turn("how does the scheduler use leases", api)) == ""
    assert asyncio.run(vtools.injection_for_turn("unrelated pancakes", local)) == ""
    monkeypatch.setitem(config._data, "context_vault", {"enabled": True, "inject_for_local": False})
    assert asyncio.run(vtools.injection_for_turn("how does the scheduler use leases", local)) == ""


def test_the_context_recall_tool_is_registered_and_read_only(installed):
    from bot.agent_runtime import toolspec

    assert "context_recall" in toolspec.registered_names()
    installed.add("Decision: the scheduler uses leases.", "decision")
    spec = toolspec._registered["context_recall"]
    handler = spec[2] if isinstance(spec, tuple) else None
    assert handler is not None
    out = asyncio.run(handler({"query": "scheduler leases"}))
    assert "scheduler uses leases" in out
    assert toolspec.registered_dangerous("context_recall") is False


# ---- surfaces --------------------------------------------------------------------------------------------------------
def test_command_line_status_recall_export_import_rebalance(tmp_path, monkeypatch, capsys):
    from bot.context_vault import __main__ as cli

    monkeypatch.setitem(config._data, "context_vault", {
        "enabled": False, "tiers": [{"name": "ram", "kind": "ram"}, {"name": "d", "kind": "dir", "path": str(tmp_path / "d")}]})
    monkeypatch.setattr(settings, "index_path", lambda: tmp_path / "idx" / "index.db")
    src = tmp_path / "in.jsonl"
    src.write_text(json.dumps({"text": "Decision: ship the vault off by default.", "kind": "decision"}) + "\n", encoding="utf-8")
    assert cli.main(["import", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["stored"] == 1
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == 1
    assert cli.main(["tiers"]) == 0
    assert [t["name"] for t in json.loads(capsys.readouterr().out)] == ["ram", "d"]
    assert cli.main(["recall", "vault default"]) == 0
    assert "ship the vault" in capsys.readouterr().out
    out = tmp_path / "out.jsonl"
    assert cli.main(["export", str(out)]) == 0
    assert "ship the vault" in out.read_text(encoding="utf-8")
    assert cli.main(["rebalance"]) == 0


def test_read_only_api_status(tmp_path, monkeypatch):
    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(settings, "data_dir", lambda: tmp_path / "data")
    client = TestClient(build_app())
    r = client.get("/api/context-vault/status", headers={"X-Dashboard-Token": "test-token"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False and [t["name"] for t in body["tiers"]] == ["ram", "store"]
    assert client.post("/api/context-vault/status", headers={"X-Dashboard-Token": "test-token"}).status_code in (404, 405)
    assert not (tmp_path / "data").exists()


def test_the_ram_tier_can_sit_on_the_cacheit_client_when_it_answers():
    from bot.context_vault.tiers import RamBackend

    class FakeCache:
        def __init__(self):
            self.store = {}

        def available(self):
            return True

        def put(self, ns, key, data, **kw):
            self.store[(ns, key)] = data
            return True

        def delete(self, ns, key):
            return self.store.pop((ns, key), None) is not None

    fake = FakeCache()
    ram = RamBackend("ram", use_cacheit=True, cache_module=fake)
    ram.put("k1", b"body")
    assert ram.get("k1") == b"body" and fake.store == {("ctxvault", "ram:k1"): b"body"}
    ram.delete("k1")
    assert not fake.store and RamBackend("r2", use_cacheit=True, cache_module=type("N", (), {"available": staticmethod(lambda: False)})())._cache is None


def test_the_owner_config_shape_loads_as_four_ordered_tiers(tmp_path):
    import yaml

    text = f"""
context_vault:
  enabled: true
  inject_for_local: true
  tiers:
  - {{name: ram, kind: ram, max_bytes: 1048576}}
  - {{name: nvme, kind: dir, path: "{(tmp_path / 'nv').as_posix()}", max_bytes: 4096}}
  - {{name: ssd,  kind: dir, path: "{(tmp_path / 'ss').as_posix()}", max_bytes: 8192}}
  - {{name: hdd,  kind: dir, path: "{(tmp_path / 'hd').as_posix()}", max_bytes: 0}}
"""
    cfg = settings.load(yaml.safe_load(text)["context_vault"])
    assert cfg["enabled"] and cfg["inject_for_local"]
    assert [t["name"] for t in cfg["tiers"]] == ["ram", "nvme", "ssd", "hdd"]
    v = Vault(cfg, tmp_path / "idx" / "index.db")
    try:
        assert [t.name for t in v.tiers] == ["ram", "nvme", "ssd", "hdd"]
        assert [v.caps[n] for n in ("ram", "nvme", "ssd", "hdd")] == [1048576, 4096, 8192, 0]
        assert v.durable[0].root == tmp_path / "nv" and v.add("x" * 40 + " owner shape works", "note")["tier"] == "nvme"
    finally:
        v.close()
