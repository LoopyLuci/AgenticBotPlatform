"""abp_cicd core: the allow-listed event schema, the tamper-evident store, the
recorder, the read models and the CLI. Uses temp databases only."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from abp_cicd import events, queries, recorder
from abp_cicd.cli import main as cli_main
from abp_cicd.service import CAPABILITIES, Service
from abp_cicd.store import EventStore, default_db_path


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "cicd" / "events.db")


# ---- schema: allow-list + redaction ------------------------------------------ #
def test_unknown_fields_are_dropped_not_recorded():
    clean = events.sanitize("step.end", {"status": "ok", "duration_ms": "42", "environment": {"SECRET": "x"},
                                         "argv": ["--flag"], "extra": "nope"})
    assert clean == {"status": "ok", "duration_ms": 42}


def test_an_unknown_event_kind_is_a_programming_error():
    with pytest.raises(ValueError):
        events.sanitize("made.up", {})


def test_secret_shaped_text_is_redacted_and_long_text_is_capped():
    fake_key = "sk" + "-" + "a1b2c3d4e5f6g7h8"          # built at runtime: no literal secret in the repo
    text = f"failed calling {fake_key} with password=hunter2 and Authorization: Bearer abcdefghijkl"
    clean = events.sanitize("note", {"message": text * 40, "level": "error"})
    assert fake_key not in clean["message"] and "hunter2" not in clean["message"] and "abcdefghijkl" not in clean["message"]
    assert len(clean["message"]) <= events.MAX_STR
    assert "[redacted]" in clean["message"]


def test_the_home_directory_is_shortened():
    home = Path.home().as_posix()
    assert events.sanitize("note", {"message": f"file {home}/projects/x.py"})["message"] == "file ~/projects/x.py"


def test_decision_inputs_are_flat_scalars_only():
    clean = events.sanitize("decision", {"actor": "rules", "decision": "skip",
                                         "inputs": {"files": 12, "risky": False, "nested": {"a": 1}, "list": [1, 2], "note": "ok"}})
    assert clean["inputs"] == {"files": 12, "risky": False, "note": "ok"}


def test_an_invalid_status_becomes_unknown():
    assert events.sanitize("run.end", {"status": "kaboom"})["status"] == "unknown"


# ---- store ------------------------------------------------------------------- #
def test_events_are_appended_in_order_and_chained(store):
    a = store.append("run.start", {"run_kind": "pipeline"}, run_id="r1")
    b = store.append("step.end", {"status": "ok", "duration_ms": 5}, run_id="r1", step="python")
    assert (a["seq"], b["seq"]) == (1, 2)
    rows = store.events()
    assert [r["kind"] for r in rows] == ["run.start", "step.end"]
    assert store.verify_chain() == {"ok": True, "count": 2, "first_bad_seq": None, "reason": ""}


def test_editing_a_row_is_detected(store):
    store.append("note", {"message": "one"}, run_id="r")
    store.append("note", {"message": "two"}, run_id="r")
    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE events SET data = ? WHERE seq = 1", ('{"message":"tampered"}',))
    conn.commit()
    conn.close()
    res = store.verify_chain()
    assert not res["ok"] and res["first_bad_seq"] == 1 and "hash" in res["reason"]


def test_deleting_a_row_is_detected(store):
    for i in range(3):
        store.append("note", {"message": str(i)}, run_id="r")
    conn = sqlite3.connect(store.path)
    conn.execute("DELETE FROM events WHERE seq = 2")
    conn.commit()
    conn.close()
    res = store.verify_chain()
    assert not res["ok"] and res["first_bad_seq"] == 3


def test_concurrent_writers_keep_a_single_linear_chain(store):
    def worker(n):
        s = EventStore(store.path)
        for i in range(15):
            s.append("note", {"message": f"{n}-{i}"}, run_id=f"r{n}")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert store.count() == 90
    assert store.verify_chain()["ok"]


def test_writers_in_separate_processes_share_one_chain(store):
    code = ("import sys; from abp_cicd.store import EventStore; s = EventStore(sys.argv[1]);"
            "[s.append('note', {'message': str(i)}, run_id='p') for i in range(10)]")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(store.path)], env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
             for _ in range(3)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert store.count() == 30 and store.verify_chain()["ok"]


def test_prune_keeps_the_chain_verifiable(store):
    for i, age in enumerate([40, 35, 30, 1, 0]):
        store.append("note", {"message": str(i)}, run_id="r", ts=time.time() - age * 86400)
    assert store.prune(10) == 3
    assert store.count() == 2 and store.verify_chain()["ok"]
    store.append("note", {"message": "after"}, run_id="r")     # the chain keeps growing from the anchor
    assert store.verify_chain()["ok"]


def test_prune_never_removes_the_newest_event(store):
    store.append("note", {"message": "only"}, run_id="r", ts=time.time() - 400 * 86400)
    assert store.prune(1) == 0 and store.count() == 1


def test_export_is_plain_jsonl(store, tmp_path):
    store.append("run.start", {"run_kind": "release", "version": "1.2.3"}, run_id="r1")
    out = tmp_path / "log.jsonl"
    assert store.export_jsonl(out) == 1
    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "run.start" and row["data"]["version"] == "1.2.3" and row["hash"]


def test_a_failure_to_record_never_raises_from_safe_append(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    s = EventStore(blocker / "sub" / "events.db")           # parent is a file: cannot be created
    assert s.safe_append("note", {"message": "x"}) is None


def test_default_path_honours_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ABP_CICD_DB", str(tmp_path / "custom.db"))
    assert default_db_path() == tmp_path / "custom.db"
    monkeypatch.delenv("ABP_CICD_DB")
    monkeypatch.setenv("ABP_HOME", str(tmp_path / "home"))
    assert default_db_path() == tmp_path / "home" / "data" / "cicd" / "events.db"


# ---- recorder ---------------------------------------------------------------- #
def test_a_run_records_steps_outcomes_and_never_swallows_exceptions(store):
    with pytest.raises(ValueError):
        with recorder.start_run("pipeline", store=store, title="t") as run:
            with run.step("python"):
                pass
            with run.step("rust") as st:
                st.set(status="skipped", skipped_reason="no changes")
            with run.step("android"):
                raise ValueError("boom")
    r = queries.get_run(store, run.id)
    assert r["status"] == "failed" and r["kind"] == "pipeline"
    by = {s["name"]: s for s in r["steps"]}
    assert by["python"]["status"] == "ok" and by["rust"]["status"] == "skipped"
    assert by["rust"]["skipped_reason"] == "no changes"
    assert by["android"]["status"] == "failed" and "ValueError: boom" in by["android"]["error"]


def test_finish_overrides_how_the_block_ended(store):
    with pytest.raises(SystemExit):
        with recorder.start_run("release", store=store, version="1.0.0") as run:
            run.finish("rolled_back", "gate failed")
            raise SystemExit(1)
    r = queries.get_run(store, run.id)
    assert r["status"] == "rolled_back" and r["summary"] == "gate failed"


def test_a_clean_system_exit_is_a_success(store):
    with pytest.raises(SystemExit):
        with recorder.start_run("pipeline", store=store) as run:
            raise SystemExit(0)
    assert queries.get_run(store, run.id)["status"] == "ok"


def test_child_runs_link_to_their_parent_and_the_env_is_restored(store, monkeypatch):
    monkeypatch.delenv(recorder.PARENT_ENV, raising=False)
    with recorder.start_run("release", store=store) as parent:
        assert os.environ[recorder.PARENT_ENV] == parent.id
        with recorder.start_run("pipeline", store=store) as child:
            pass
    assert recorder.PARENT_ENV not in os.environ
    assert queries.get_run(store, child.id)["attrs"]["parent_run"] == parent.id


def test_an_unusable_store_makes_the_recorder_a_no_op(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    bad = EventStore(blocker / "sub" / "events.db")
    with recorder.start_run("pipeline", store=bad) as run:      # must not raise
        with run.step("x"):
            pass
        run.decision(actor="rules", decision="d")


# ---- queries ----------------------------------------------------------------- #
def _seed(store, kind="pipeline", ok=True, durations=(1000, 2000), version=None):
    with recorder.start_run(kind, store=store, **({"version": version} if version else {})) as run:
        for i, ms in enumerate(durations):
            run.record_step(f"step{i}", "ok", ms)
        if not ok:
            run.record_step("bad", "failed", 10, error="Boom: no")
            run.finish("failed", "step bad failed")
    return run


def test_list_runs_is_newest_first_and_filters_by_kind(store):
    a = _seed(store, "pipeline")
    b = _seed(store, "release", version="0.1.0")
    c = _seed(store, "pipeline", ok=False)
    ids = [r["id"] for r in queries.list_runs(store)]
    assert ids == [c.id, b.id, a.id]
    assert [r["id"] for r in queries.list_runs(store, kind="release")] == [b.id]
    assert queries.list_runs(store)[0]["failed_steps"] == 1


def test_a_run_with_no_end_is_running_then_stale(store):
    store.append("run.start", {"run_kind": "pipeline"}, run_id="live", ts=time.time() - 30)
    store.append("step.start", {}, run_id="live", step="python", ts=time.time() - 20)
    assert queries.get_run(store, "live")["status"] == "running"
    assert queries.get_run(store, "live", now=time.time() + 5 * 3600)["status"] == "stale"


def test_step_stats_percentiles_ignore_failures_and_skips(store):
    for ms in (100, 200, 300, 400, 1000):
        with recorder.start_run("pipeline", store=store) as run:
            run.record_step("python", "ok", ms)
    with recorder.start_run("pipeline", store=store) as run:
        run.record_step("python", "failed", 5)
    with recorder.start_run("pipeline", store=store) as run:
        run.record_step("python", "skipped", 0)
    s = queries.step_stats(store, "python")["python"]
    assert (s["n"], s["ok"], s["failed"], s["skipped"]) == (7, 5, 1, 1)
    assert s["p50_ms"] == 300 and s["p95_ms"] == 1000 and s["max_ms"] == 1000
    assert s["last_status"] == "skipped"


def test_step_stats_can_be_limited_to_one_kind_of_run(store):
    _seed(store, "pipeline", durations=(100,))
    _seed(store, "release", durations=(9000,))
    assert queries.step_stats(store, "step0", run_kind="release")["step0"]["max_ms"] == 9000


def test_summary_reports_chain_health_and_per_kind_counts(store):
    _seed(store, "pipeline")
    _seed(store, "pipeline", ok=False)
    s = queries.summary(store)
    assert s["by_kind"]["pipeline"]["runs"] == 2 and s["by_kind"]["pipeline"]["failed"] == 1
    assert s["chain"]["ok"] is True


def test_workers_report_stale_when_silent(store):
    store.append("worker.heartbeat", {"worker": "flake_detector", "state": "serving", "model": "v3", "queue": 2})
    now = time.time()
    fresh = queries.workers(store, now=now + 5)[0]
    assert fresh["state"] == "serving" and fresh["model"] == "v3"
    stale = queries.workers(store, now=now + 600)[0]
    assert stale["state"] == "stale" and stale["reported_state"] == "serving"


def test_explain_is_plain_language_and_deterministic(store):
    with recorder.start_run("release", store=store, version="0.9.0") as run:
        run.record_step("built_desktop", "ok", 240_000)
        run.record_step("smoke", "ok", 20_000)
        run.record_step("rust", "skipped", 0, skipped_reason="no src-tauri changes")
        run.decision(actor="rules", decision="run the full suite", reason="release commit", confidence=1.0)
    text = queries.explain(store, run.id)
    assert "release 0.9.0" in text and "ok after" in text
    assert "slowest was 'built_desktop'" in text
    assert "'rust' was skipped: no src-tauri changes" in text
    assert "rules decided to run the full suite because release commit (confidence 1.00)" in text
    assert queries.explain(store, "nope") is None


def test_every_capability_exists_on_the_service_and_returns_json_safe_data(store):
    _seed(store)
    svc = Service(store)
    for name in CAPABILITIES:
        assert callable(getattr(svc, name)), name
    json.dumps([svc.summary(), svc.runs(), svc.step_stats(), svc.decisions(), svc.workers(), svc.events(), svc.chain()])


# ---- CLI --------------------------------------------------------------------- #
def _cli(tmp_db, *args):
    return cli_main(["--source", "local", "--db", str(tmp_db), *args])


def test_cli_commands_run_against_a_local_store(store, capsys):
    run = _seed(store, "release", version="0.5.0")
    assert _cli(store.path, "status") == 0 and "release" in capsys.readouterr().out
    assert _cli(store.path, "runs") == 0 and run.id in capsys.readouterr().out
    assert _cli(store.path, "run", run.id) == 0 and "step0" in capsys.readouterr().out
    assert _cli(store.path, "explain", run.id) == 0 and "release 0.5.0" in capsys.readouterr().out
    assert _cli(store.path, "steps") == 0 and "p95" in capsys.readouterr().out
    assert _cli(store.path, "verify") == 0 and "chain OK" in capsys.readouterr().out
    assert _cli(store.path, "run", "missing") == 2


def test_cli_json_output_is_exactly_the_service_payload(store, capsys):
    _seed(store)
    _cli(store.path, "--json", "runs")
    assert json.loads(capsys.readouterr().out) == Service(store).runs(limit=20)


def test_cli_verify_exits_nonzero_on_a_broken_chain(store, capsys):
    _seed(store)
    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE events SET data = '{}' WHERE seq = 1")
    conn.commit()
    conn.close()
    assert _cli(store.path, "verify") == 3 and "BROKEN" in capsys.readouterr().out


def test_cli_export_and_prune_are_local_control_operations(store, tmp_path, capsys):
    _seed(store)
    out = tmp_path / "x.jsonl"
    assert _cli(store.path, "export", str(out)) == 0 and out.exists()
    assert _cli(store.path, "prune", "365") == 0


def test_cli_events_lists_the_log(store, capsys):
    _seed(store)
    assert _cli(store.path, "events", "--limit", "3") == 0
    assert capsys.readouterr().out.count("\n") == 3


def test_cli_reports_an_unreachable_server_cleanly(capsys):
    code = cli_main(["--source", "http", "--url", "http://127.0.0.1:1", "status"])
    assert code == 1 and "can't reach" in capsys.readouterr().err


def test_explain_names_why_a_step_failed_even_without_an_exception(store):
    with recorder.start_run("release", store=store, version="1.0.0") as run:
        with run.step("preflight") as st:
            st.set(status="failed", detail="tracked files are committed")
    assert "'preflight' FAILED: tracked files are committed." in queries.explain(store, run.id)
