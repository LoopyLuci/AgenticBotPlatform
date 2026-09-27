"""The Sentinel (ADR-0011): backups, repair, schema versions, CVE scanning,
security posture, bug hunting, boot guard, guardian, watchdog."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from bot import db
from bot import tasks as bg
from bot.sentinel import backup, bootguard, bug_hunter, cve, journal, repair, security
from bot.sentinel.watchdog import Watchdog


def _seed(n: int = 50) -> None:
    for i in range(n):
        db.log_audit(actor="test", action="seed", detail=f"row {i}")


def _audit_count() -> int:
    return db.get_conn().execute("SELECT COUNT(*) FROM audit_log WHERE action='seed'").fetchone()[0]


# ------------------------------------------------------------ journal --

def test_alerts_are_deduplicated_until_cleared(monkeypatch):
    pushes = []
    monkeypatch.setattr(journal, "_push", pushes.append)
    assert journal.alert("k", "first", level="warning") is True
    assert journal.alert("k", "again", level="warning") is False
    journal.clear("k")
    assert journal.alert("k", "back", level="warning") is True
    assert pushes == ["first", "back"]
    kinds = [e["kind"] for e in journal.recent(10)]
    assert "resolved" in kinds and kinds.count("alert") == 2
    assert journal.journal_path().exists()


# ------------------------------------------------------------ schema versions --

def test_init_db_stamps_the_schema_version(temp_db):
    assert db.schema_version() == db.SCHEMA_VERSION


def test_a_database_from_a_newer_build_is_refused(temp_db):
    conn = db.get_conn()
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.commit()
    with pytest.raises(db.SchemaTooNewError):
        db.init_db()


# ------------------------------------------------------------ per-thread connections --

def test_other_threads_get_their_own_connection_and_see_committed_writes(temp_db):
    main_conn = db.get_conn()
    _seed(3)
    seen = {}

    def worker():
        seen["conn"] = db.get_conn()
        seen["count"] = _audit_count()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["conn"] is not main_conn
    assert seen["count"] == 3


def test_close_conn_invalidates_thread_connections(temp_db):
    got = []

    def worker():
        got.append(db.get_conn())
        barrier.wait()
        barrier2.wait()
        got.append(db.get_conn())

    barrier, barrier2 = threading.Barrier(2), threading.Barrier(2)
    t = threading.Thread(target=worker)
    t.start()
    barrier.wait()
    db.close_conn()
    db.get_conn()
    barrier2.wait()
    t.join()
    assert got[0] is not got[1]


# ------------------------------------------------------------ backups --

def test_backup_is_verified_and_restorable(temp_db):
    _seed(20)
    m = backup.create_backup("test")
    assert m["verified"], m["problems"]
    assert m["files"]["bot.db"]["integrity"] == "ok"
    assert m["files"]["bot.db"]["schema_version"] == db.SCHEMA_VERSION
    _seed(5)
    assert _audit_count() == 25
    result = backup.restore_backup(m["name"], parts=("db",))
    assert _audit_count() == 20
    assert result["previous_files"] and Path(result["previous_files"]).exists()


def test_bit_rot_in_a_backup_is_detected(temp_db):
    _seed(5)
    m = backup.create_backup("test")
    f = backup.BACKUPS_ROOT / m["name"] / "bot.db"
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 0xFF
    f.write_bytes(bytes(data))
    assert any("hash mismatch" in p for p in backup.verify_backup(f.parent))
    with pytest.raises(ValueError):
        backup.restore_backup(m["name"])


def test_prune_keeps_recent_and_the_newest_verified(temp_db):
    root = backup.BACKUPS_ROOT
    for day in range(1, 11):
        d = root / f"202601{day:02d}T000000Z"
        d.mkdir(parents=True)
        (d / backup.MANIFEST).write_text(json.dumps({"name": d.name, "verified": day == 2, "files": {}}), encoding="utf-8")
    removed = backup.prune(keep_recent=3, keep_daily=0, keep_weekly=0)
    left = sorted(p.name for p in root.iterdir())
    assert "20260102T000000Z" in left  # the newest verified, kept regardless
    assert left[-3:] == ["20260108T000000Z", "20260109T000000Z", "20260110T000000Z"]
    assert len(removed) == 6


def test_latest_verified_for_schema_skips_newer_databases(temp_db):
    _seed(2)
    m = backup.create_backup("test")
    assert backup.latest_verified_for_schema(db.SCHEMA_VERSION)["name"] == m["name"]
    assert backup.latest_verified_for_schema(db.SCHEMA_VERSION - 1) is None


def test_mirror_copies_and_verifies(temp_db, tmp_path):
    _seed(2)
    m = backup.create_backup("test")
    assert backup.mirror(m, tmp_path / "mirror") == []
    assert (tmp_path / "mirror" / m["name"] / "bot.db").exists()


# ------------------------------------------------------------ repair --

def _corrupt_pages(path: Path) -> None:
    data = bytearray(path.read_bytes())
    page = 4096
    for off in range(page * 2, len(data) - page, page * 3):
        data[off + 16: off + 400] = b"\x00\xde\xad" * 128
    path.write_bytes(bytes(data))


def test_repair_restores_a_corrupted_database(temp_db):
    _seed(300)
    good = backup.create_backup("before")
    assert good["verified"]
    db.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close_conn()
    _corrupt_pages(Path(db.DB_PATH))
    assert backup.check_sqlite(Path(db.DB_PATH)) != "ok"
    outcome = repair.repair_database()
    assert outcome["ok"], outcome["steps"]
    assert backup.check_sqlite(Path(db.DB_PATH)) == "ok"
    assert Path(outcome["quarantine"]).exists()
    assert _audit_count() > 0


def test_check_database_reports_ok_on_a_healthy_file(temp_db):
    r = repair.check_database()
    assert r["quick_check"] == "ok" and r["ok"]


# ------------------------------------------------------------ CVE --

@pytest.mark.parametrize("vector,score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 7.5),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
    ("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N", 1.8),
    ("garbage", None),
])
def test_cvss3_scores(vector, score):
    assert cve.cvss3_score(vector) == score


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeOSV:
    VULNS = {
        "PYSEC-1": {"id": "PYSEC-1", "aliases": ["CVE-2099-1", "GHSA-x"], "summary": "bad thing",
                    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                    "affected": [{"package": {"name": "demo-pkg", "ecosystem": "PyPI"},
                                  "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.0"}]},
                                             {"type": "GIT", "events": [{"fixed": "a" * 40}]}]}]},
        "GHSA-x": {"id": "GHSA-x", "aliases": ["PYSEC-1"], "summary": "bad thing", "affected": []},
        "RUSTSEC-1": {"id": "RUSTSEC-1", "summary": "crate is unmaintained",
                      "affected": [{"package": {"name": "old", "ecosystem": "crates.io"},
                                    "database_specific": {"informational": "unmaintained"}}]},
    }

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json):
        results = []
        for q in json["queries"]:
            name = q["package"]["name"]
            ids = {"demo-pkg": ["PYSEC-1", "GHSA-x"], "old": ["RUSTSEC-1"]}.get(name, [])
            results.append({"vulns": [{"id": i, "modified": "t"} for i in ids]})
        return _Resp({"results": results})

    def get(self, url):
        return _Resp(self.VULNS[url.rsplit("/", 1)[-1]])


def test_scan_dedupes_aliases_scores_and_finds_fixes(monkeypatch):
    monkeypatch.setattr(cve.httpx, "Client", _FakeOSV)
    items = [{"ecosystem": "PyPI", "name": "demo-pkg", "version": "1.0", "source": "python-env"},
             {"ecosystem": "crates.io", "name": "old", "version": "0.1", "source": "Cargo.lock"},
             {"ecosystem": "PyPI", "name": "fine", "version": "1.0", "source": "python-env"}]
    result = cve.scan(items)
    by_name = {f["name"]: f for f in result["findings"]}
    assert set(by_name) == {"demo-pkg", "old"}
    demo = by_name["demo-pkg"]
    assert demo["id"] == "CVE-2099-1" and demo["score"] == 9.8 and demo["severity"] == "CRITICAL"
    assert demo["fixed"] == ["2.0"]  # the git commit is not a version
    assert by_name["old"]["severity"] == "INFO"
    assert cve.last_result()["findings"]


def test_report_alerts_real_findings_only(monkeypatch):
    sent = []
    monkeypatch.setattr(journal, "_push", sent.append)
    cve.report({"packages": 2, "findings": [
        {"ecosystem": "PyPI", "name": "a", "version": "1", "source": "python-env", "id": "CVE-1", "score": 9.1,
         "severity": "CRITICAL", "summary": "x", "fixed": ["2"]},
        {"ecosystem": "crates.io", "name": "b", "version": "1", "source": "Cargo.lock", "id": "RUSTSEC-9", "score": None,
         "severity": "INFO", "summary": "unmaintained", "fixed": []}]})
    assert len(sent) == 1 and "CVE-1" in sent[0]


def test_inventories_parse_lockfiles(tmp_path):
    (tmp_path / "Cargo.lock").write_text(textwrap.dedent('''
        [[package]]
        name = "rustls"
        version = "0.23.45"
        source = "registry+https://github.com/rust-lang/crates.io-index"

        [[package]]
        name = "app"
        version = "0.1.0"
    '''), encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {
        "": {"name": "root"}, "node_modules/a": {"version": "1.0.0"}, "node_modules/b/node_modules/@s/c": {"version": "2.0.0"},
        "node_modules/linked": {"link": True}}}), encoding="utf-8")
    (tmp_path / "libs.versions.toml").write_text(textwrap.dedent('''
        [versions]
        okhttp = "4.12.0"
        [libraries]
        okhttp = { module = "com.squareup.okhttp3:okhttp", version.ref = "okhttp" }
        direct = { group = "g", name = "n", version = "1.2" }
    '''), encoding="utf-8")
    assert [i["name"] for i in cve.inventory_cargo_lock(tmp_path / "Cargo.lock")] == ["rustls"]
    assert sorted(i["name"] for i in cve.inventory_npm_lock(tmp_path / "package-lock.json")) == ["@s/c", "a"]
    maven = {i["name"]: i["version"] for i in cve.inventory_gradle_catalog(tmp_path / "libs.versions.toml")}
    assert maven == {"com.squareup.okhttp3:okhttp": "4.12.0", "g:n": "1.2"}
    assert any(i["source"] == "python-env" and i["name"].lower() == "pytest" for i in cve.inventory_python_env())


def test_fix_python_rolls_back_when_abp_stops_importing(monkeypatch):
    calls = []

    class P:
        def __init__(self, rc=0):
            self.returncode, self.stdout, self.stderr = rc, "", ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["-c", "import bot.main, bot.dashboard.server"]:
            return P(1)
        return P(0)

    monkeypatch.setattr(cve.subprocess, "run", fake_run)
    out = cve.fix_python([{"source": "python-env", "name": "demo", "version": "1.0", "fixed": ["0.5", "1.2", "2.0"]}],
                         python="py")
    assert out[0]["target"] == "1.2" and out[0]["ok"] is False and "rolled back" in out[0]["detail"]
    assert calls[-1][-1] == "demo==1.0"


# ------------------------------------------------------------ security --

def test_exposure_and_weak_token_are_flagged(monkeypatch):
    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("DASHBOARD_TOKEN", "short")
    monkeypatch.delenv("ABP_EXPOSE_DASHBOARD", raising=False)
    keys = {f["key"] for f in security.check_exposure()}
    assert keys == {"security.exposure", "security.token"}
    monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("DASHBOARD_TOKEN", "x" * 48)
    assert security.check_exposure() == []


def test_leaked_secrets_in_logs_are_found_and_redacted(tmp_path):
    log = tmp_path / "bot.log"
    secret = "sk-ant-" + "a" * 40
    log.write_text(f"12:00:00 INFO x: calling with key {secret}\n", encoding="utf-8")
    assert security.check_log_leaks(log)[0]["level"] == "critical"
    assert security.redact_log(log) == 1
    assert secret not in log.read_text(encoding="utf-8")
    assert security.check_log_leaks(log) == []


def test_code_tampering_is_detected_in_an_installed_copy(monkeypatch, tmp_path):
    code = tmp_path / "install"
    (code / "bot").mkdir(parents=True)
    (code / "bot" / "x.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setattr(security, "CODE_ROOT", code)
    assert security.check_code_integrity() == []  # first run records the baseline
    assert security.check_code_integrity() == []
    (code / "bot" / "x.py").write_text("print('evil')\n", encoding="utf-8")
    assert security.check_code_integrity()[0]["key"] == "security.code-integrity"
    (code / ".git").mkdir()
    assert security.check_code_integrity() == []  # a checkout is expected to change


# ------------------------------------------------------------ bug hunter --

def _record(msg, exc=None, name="bot.x", level=logging.ERROR):
    rec = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    if exc is not None:
        try:
            raise exc
        except type(exc):
            rec.exc_info = sys.exc_info()
    return rec


def test_errors_collapse_into_one_issue_regardless_of_ids():
    bug_hunter.observe(_record("job 1234 failed for user 'alice' at C:/x/y.py"))
    bug_hunter.observe(_record("job 99 failed for user 'bob' at /tmp/z.py"))
    issues = bug_hunter.issues()
    assert len(issues) == 1 and issues[0]["count"] == 2
    out = bug_hunter.review()
    assert len(out["new"]) == 1 and bug_hunter.ISSUES_PATH.exists()
    assert bug_hunter.review()["new"] == []  # not new twice


def test_exceptions_are_fingerprinted_by_type_and_our_frame():
    bug_hunter.observe(_record("boom", ValueError("x1")))
    bug_hunter.observe(_record("boom again", ValueError("x2")))
    [issue] = bug_hunter.issues()
    assert issue["signature"].startswith("ValueError @ tests/test_sentinel.py:_record")
    assert "ValueError" in issue["sample"]


def test_resolved_issue_that_returns_is_regressed():
    bug_hunter.observe(_record("thing broke"))
    bug_hunter.review()
    sig = bug_hunter.issues()[0]["signature"]
    assert bug_hunter.set_status(sig, "resolved")
    bug_hunter.observe(_record("thing broke"))
    assert bug_hunter.issues()[0]["status"] == "regressed"
    assert bug_hunter.review()["new"] == [sig]


def test_handler_ignores_plain_warnings_and_sentinel_logs():
    h = bug_hunter.BugHunterHandler()
    h.emit(_record("slow", level=logging.WARNING))
    h.emit(_record("alert", name="bot.sentinel"))
    assert bug_hunter.issues() == []


# ------------------------------------------------------------ boot guard --

def test_crash_loop_enters_safe_mode_and_restores_last_known_good(monkeypatch, tmp_path):
    cfg = tmp_path / "root" / "config"
    cfg.mkdir(parents=True)
    good = cfg / "backends.yaml"
    good.write_text("good: true\n", encoding="utf-8")
    monkeypatch.setattr(bootguard, "_lkg_sources", lambda: {"config/backends.yaml": good})
    monkeypatch.setattr(journal, "_push", lambda m: None)
    bootguard.begin_boot()
    bootguard.mark_healthy()  # saves last-known-good
    good.write_text("broken: [\n", encoding="utf-8")
    for _ in range(bootguard.CRASH_LOOP_BOOTS - 1):
        bootguard._reset_for_tests()
        assert not bootguard.begin_boot()["safe_mode"]
    bootguard._reset_for_tests()
    boot = bootguard.begin_boot()
    assert boot["safe_mode"] and bootguard.is_safe_mode()
    assert good.read_text(encoding="utf-8") == "good: true\n"
    assert list(cfg.glob("backends.yaml.pre-safe-mode-*"))


# ------------------------------------------------------------ guardian --

def test_guardian_restarts_crashes_and_stops_on_clean_exit(tmp_path, monkeypatch):
    pkg = tmp_path / "fakeapp"
    pkg.mkdir()
    counter = tmp_path / "n"
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(textwrap.dedent(f'''
        import os, sys
        p = {str(counter)!r}
        n = int(open(p).read()) if os.path.exists(p) else 0
        open(p, "w").write(str(n + 1))
        assert os.environ["ABP_SUPERVISED"] == "1"
        sys.exit(75 if n < 2 else 0)
    '''), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from bot.sentinel import guardian

    monkeypatch.setattr(guardian, "_log", lambda m: None)
    assert guardian.supervise(module="fakeapp", backoff=(0.01,)) == 0
    assert counter.read_text() == "3"


def test_guardian_gives_up_on_a_hopeless_crash_loop(tmp_path, monkeypatch):
    pkg = tmp_path / "crashy"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from bot.sentinel import guardian

    monkeypatch.setattr(guardian, "_log", lambda m: None)
    monkeypatch.setattr(guardian, "MAX_CRASHES", 2)
    assert guardian.supervise(module="crashy", backoff=(0.01,)) == 3


# ------------------------------------------------------------ watchdog --

def test_watchdog_names_the_code_that_blocks_the_loop(caplog):
    async def main():
        # Generous margins: under a loaded parallel run the watchdog thread itself gets scheduled late, and the stack
        # it captures must still be inside the blocking call.
        w = Watchdog(lag_warn_s=0.3, hang_after_s=60)
        w.start(asyncio.get_running_loop())
        await asyncio.sleep(1.2)

        def this_blocks_the_loop():
            time.sleep(4.0)

        this_blocks_the_loop()
        await asyncio.sleep(1.0)
        w.stop()
        return w

    with caplog.at_level(logging.ERROR, logger="bot.sentinel.watchdog"):
        w = asyncio.run(main())
    assert w.stalls >= 1
    assert any("this_blocks_the_loop" in r.getMessage() for r in caplog.records)


def test_leak_detector_needs_sustained_growth():
    w = Watchdog()
    for i in range(360):
        w.samples.append((i, 100 + i, 10))
    assert "possible leak" in w.leak_suspected()
    w.samples.clear()
    for i in range(360):
        w.samples.append((i, 100 + (i % 60), 10))
    assert w.leak_suspected() is None


# ------------------------------------------------------------ background tasks --

def test_spawn_keeps_tasks_and_logs_failures(caplog):
    async def boom():
        raise RuntimeError("kaboom")

    async def main():
        bg.spawn(boom(), name="boomer")
        await asyncio.sleep(0.05)

    before = bg.failures()
    with caplog.at_level(logging.ERROR, logger="bot.tasks"):
        asyncio.run(main())
    assert bg.failures() == before + 1
    assert any("boomer" in r.getMessage() for r in caplog.records)


def test_spawn_soon_reaches_the_loop_from_a_worker_thread():
    async def main():
        bg.bind_loop(asyncio.get_running_loop())
        done = asyncio.Event()

        async def mark():
            done.set()

        await asyncio.to_thread(bg.spawn_soon, lambda: mark())
        await asyncio.wait_for(done.wait(), 2)
        hit = asyncio.Event()

        async def mark2():
            hit.set()

        await asyncio.to_thread(lambda: bg.spawn(mark2()))
        await asyncio.wait_for(hit.wait(), 2)

    asyncio.run(main())


# ------------------------------------------------------------ orchestrator + API --

def test_sentinel_tick_runs_duties_and_status_reports(temp_db, monkeypatch):
    from bot import sentinel as sentinel_pkg

    s = sentinel_pkg.Sentinel()
    s.started_at -= 10_000  # everything is due
    monkeypatch.setattr(sentinel_pkg.Sentinel, "cve_duty", lambda self, cfg: {"skipped": True})
    monkeypatch.setattr(journal, "_push", lambda m: None)
    asyncio.run(s.tick())
    assert {"integrity", "backup", "security", "bugs", "healthy"} <= set(s.last_result)
    assert all(r["ok"] for r in s.last_result.values()), s.last_result
    st = s.status()
    assert st["latest_backup"]["verified"] and st["backups"] == 1


def test_sentinel_api(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    monkeypatch.setattr(journal, "_push", lambda m: None)
    client = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert client.get("/api/sentinel/status").status_code == 401
    assert client.post("/api/sentinel/run/backup").status_code == 401
    r = client.post("/api/sentinel/run/backup", headers=h)
    assert r.status_code == 200 and r.json()["result"]["ok"]
    backups = client.get("/api/sentinel/backups", headers=h).json()["backups"]
    assert len(backups) == 1
    name = backups[0]["name"]
    assert client.post(f"/api/sentinel/backups/{name}/verify", headers=h).json()["sound"] is True
    assert client.post("/api/sentinel/backups/..%2Fetc/verify", headers=h).status_code in (400, 404)
    assert client.post("/api/sentinel/run/nope", headers=h).status_code == 404
    st = client.get("/api/sentinel/status", headers=h).json()
    assert st["latest_backup"]["name"] == name
    assert client.get("/api/sentinel/issues", headers=h).status_code == 200


def test_watchdog_shuts_down_when_its_supervisor_disappears(monkeypatch):
    import subprocess

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    monkeypatch.setenv("ABP_SUPERVISOR_PID", str(dead.pid))

    async def main():
        stop = asyncio.Event()
        w = Watchdog()
        w.on_orphaned = lambda: loop.call_soon_threadsafe(stop.set)
        loop = asyncio.get_running_loop()
        w.start(loop)
        await asyncio.wait_for(stop.wait(), 10)
        w.stop()

    asyncio.run(main())
