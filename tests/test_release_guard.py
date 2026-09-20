"""scripts/release_guard.py — the release/pipeline safety net: pre-flight checks,
lock healing, the step journal + rollback, retry classification, the bundle
smoke test and the pre-push handshake. Git-facing checks run against throwaway
repositories, never the real one."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import release_guard as g  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    (r / "README.md").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _release_files(repo: Path) -> None:
    for rel, text in (("desktop-app/src-tauri/Cargo.toml", "v"), ("desktop-app/src-tauri/Cargo.lock", "l"),
                      ("desktop-app/src-tauri/tauri.conf.json", "{}"), ("android-app/app/build.gradle.kts", "g")):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "files")


# ---- git state ------------------------------------------------------------ #
def test_clean_tree_ignores_untracked_but_catches_edits(repo):
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    assert g.check_clean_tree(repo).ok
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    c = g.check_clean_tree(repo)
    assert not c.ok and "README.md" in c.detail


def test_clean_tree_heals_a_touched_but_unchanged_file(repo):
    p = repo / "README.md"
    p.write_text("hi\n", encoding="utf-8")  # same bytes, new mtime
    time.sleep(0.05)
    assert g.check_clean_tree(repo).ok


def test_git_state_flags_wrong_branch_and_operations_in_progress(repo):
    _git(repo, "switch", "-q", "-c", "feature")
    (repo / ".git" / "MERGE_HEAD").write_text("x", encoding="utf-8")
    by_name = {c.name: c for c in g.check_git_state(repo, fetch=False)}
    assert not by_name["on branch main"].ok
    assert not by_name["no git operation in progress"].ok


def test_git_state_detects_being_behind_origin(tmp_path, repo):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "t")
    (other / "new.txt").write_text("n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "ahead")
    _git(other, "push", "-q", "origin", "main")
    behind = next(c for c in g.check_git_state(repo, fetch=True) if c.name == "not behind origin")
    assert not behind.ok and "1 commit" in behind.detail


def test_stale_index_lock_is_removed_when_no_git_is_running(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    monkeypatch.setattr(g.psutil, "process_iter", lambda attrs=None: iter(()))
    c = g.check_index_lock(repo)
    assert c.ok and c.fixed and not lock.exists()


def test_index_lock_is_left_alone_while_git_is_running(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    fake = SimpleNamespace(pid=4242, info={"name": "git.exe"})
    monkeypatch.setattr(g.psutil, "process_iter", lambda attrs=None: iter([fake]))
    c = g.check_index_lock(repo)
    assert not c.ok and lock.exists()


# ---- gitignore guard -------------------------------------------------------- #
def test_an_unanchored_ignore_rule_hiding_source_is_caught(repo):
    (repo / ".gitignore").write_text("data/\n", encoding="utf-8")
    hidden = repo / "android-app" / "app" / "src" / "main" / "data"
    hidden.mkdir(parents=True)
    (hidden / "Thing.kt").write_text("x", encoding="utf-8")
    c = g.check_ignored_source(repo)
    assert not c.ok and "Thing.kt" in c.detail


def test_an_anchored_rule_and_build_noise_pass(repo):
    (repo / ".gitignore").write_text("/data/\n__pycache__/\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "x.db").write_text("x", encoding="utf-8")
    cache = repo / "bot" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.pyc").write_text("x", encoding="utf-8")
    assert g.check_ignored_source(repo).ok


# ---- version ---------------------------------------------------------------- #
def test_version_must_be_semver_new_and_above_the_latest_tag(repo):
    _git(repo, "tag", "v0.7.5")
    _git(repo, "tag", "v0.7.10")

    def ok(v, **kw):
        return all(c.ok for c in g.check_version(v, repo, check_remote=False, **kw))

    assert ok("0.7.11")
    assert not ok("0.7.10")          # exists
    assert not ok("0.7.9")           # older than the newest tag
    assert not ok("0.7")             # not X.Y.Z
    # with --resume the release's own existing tag is not counted as "newer"
    assert ok("0.7.10", allow_existing_tag=True)


def test_resume_may_reuse_its_own_tag(repo):
    _git(repo, "tag", "v0.7.5")
    assert all(c.ok for c in g.check_version("0.7.5", repo, check_remote=False, allow_existing_tag=True))


def test_remote_tag_already_present_blocks(tmp_path, repo):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "tag", "v0.7.3")
    _git(repo, "push", "-q", "origin", "main", "v0.7.3")
    _git(repo, "tag", "-d", "v0.7.3")
    c = [c for c in g.check_version("0.7.3", repo) if c.name == "tag not already on origin"][0]
    assert not c.ok


# ---- Cargo.lock ------------------------------------------------------------- #
def _cargo(repo: Path, lock_version: str, eol: str) -> Path:
    d = repo / "desktop-app" / "src-tauri"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Cargo.toml").write_text('[package]\nname = "my-app"\nversion = "0.7.28"\n', encoding="utf-8")
    body = ('[[package]]\nname = "other"\nversion = "1.0.0"\n\n'
            f'[[package]]\nname = "my-app"\nversion = "{lock_version}"\n').replace("\n", eol)
    (d / "Cargo.lock").write_bytes(body.encode("utf-8"))
    return d / "Cargo.lock"


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_cargo_lock_is_synced_and_line_endings_are_preserved(repo, eol):
    lock = _cargo(repo, "0.7.26", eol)
    c = g.sync_cargo_lock("0.7.28", repo)
    assert c.ok and c.fixed
    data = lock.read_bytes().decode()
    assert 'name = "my-app"' + eol + 'version = "0.7.28"' in data
    assert 'name = "other"' + eol + 'version = "1.0.0"' in data  # other packages untouched
    assert not g.sync_cargo_lock("0.7.28", repo).fixed          # idempotent


# ---- lock holders ----------------------------------------------------------- #
def _fake_proc(pid, exe):
    return SimpleNamespace(pid=pid, info={"pid": pid, "name": Path(exe).name, "exe": exe})


def test_only_processes_running_out_of_our_build_outputs_are_holders(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    d = root / "desktop-app" / "src-tauri"
    procs = [
        _fake_proc(101, str(d / "target" / "release" / "app.exe")),
        _fake_proc(102, str(d / "target" / "release" / ".venv" / "Scripts" / "python.exe")),
        _fake_proc(103, str(d / "stage" / ".venv" / "Scripts" / "python.exe")),
        _fake_proc(104, str(d / "target" / "release" / "build" / "pkg-abc" / "build-script-build.exe")),
        _fake_proc(105, str(d / "target" / "release" / "deps" / "thing.exe")),
        _fake_proc(106, r"C:\Program Files\ABP\app.exe"),
        _fake_proc(107, str(root / ".venv" / "Scripts" / "python.exe")),
        _fake_proc(108, str(d / "target" / "release" / "app.exe")),
    ]
    monkeypatch.setattr(g.psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(g, "_ancestor_pids", lambda: {108})
    assert sorted(p.pid for p in g.find_lock_holders(root)) == [101, 102, 103]


def test_locked_files_reports_files_that_refuse_a_write_open(tmp_path, monkeypatch):
    venv = tmp_path / "desktop-app" / "src-tauri" / "stage" / ".venv" / "Lib"
    venv.mkdir(parents=True)
    held, free = venv / "_rust.pyd", venv / "ok.pyd"
    held.write_bytes(b"x")
    free.write_bytes(b"x")

    real_open = open

    def fake_open(path, mode="r", *a, **kw):
        if str(path) == str(held):
            raise PermissionError(13, "in use")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(g, "open", fake_open, raising=False)
    assert g.locked_files(tmp_path) == [str(held)]


def test_heal_waits_until_files_are_free_and_reports_what_stays_locked(monkeypatch):
    seen = iter([["a.pyd"], ["a.pyd"], []])
    monkeypatch.setattr(g, "find_lock_holders", lambda root: [])
    monkeypatch.setattr(g, "locked_files", lambda root: next(seen))
    sleeps = []
    res = g.heal_locks(Path("."), wait_s=30, sleep=sleeps.append, log=lambda s: None)
    assert res.ok and len(sleeps) == 2

    monkeypatch.setattr(g, "locked_files", lambda root: ["stuck.dll"])
    res = g.heal_locks(Path("."), wait_s=0, sleep=lambda s: None, log=lambda s: None)
    assert not res.ok and res.still_locked == ["stuck.dll"]


def test_stop_processes_really_stops_a_process_and_its_children():
    child = subprocess.Popen([sys.executable, "-c",
                              "import subprocess,sys,time;"
                              "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)"])
    time.sleep(1.0)
    names = g.stop_processes([psutil.Process(child.pid)], grace=3)
    assert len(names) >= 2  # the parent and its child
    assert child.wait(timeout=5) is not None
    assert not psutil.pid_exists(child.pid) or psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE


# ---- pipeline lock ---------------------------------------------------------- #
def test_lock_blocks_a_live_foreign_owner_and_replaces_a_stale_one(tmp_path):
    path = tmp_path / ".pipeline.lock"
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        path.write_text(json.dumps({"pid": other.pid, "started": psutil.Process(other.pid).create_time(),
                                    "label": "release"}), encoding="utf-8")
        with pytest.raises(g.Busy):
            with g.PipelineLock(path):
                pass
    finally:
        other.kill()
        other.wait()
    # the owner is gone now -> stale -> replaced
    with g.PipelineLock(path, "release"):
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == psutil.Process().pid
    assert not path.exists()


def test_a_recycled_pid_is_not_treated_as_the_owner(tmp_path):
    path = tmp_path / ".pipeline.lock"
    path.write_text(json.dumps({"pid": psutil.Process().pid, "started": 1.0}), encoding="utf-8")
    with g.PipelineLock(path):  # same pid but a different start time -> stale
        pass


def test_a_descendant_of_the_owner_may_reenter(tmp_path):
    path = tmp_path / ".pipeline.lock"
    with g.PipelineLock(path, "release"):
        with g.PipelineLock(path, "pre-push"):   # we ARE the owner's process tree
            assert path.exists()
        assert path.exists()  # the inner one didn't release the owner's lock
    assert not path.exists()


# ---- journal + rollback ----------------------------------------------------- #
def test_journal_roundtrip_and_corruption(tmp_path):
    p = tmp_path / "j.json"
    j = g.Journal(p, "0.7.28", "abc")
    j.done("bumped")
    j.done("committed", release_commit="def")
    back = g.Journal.load(p)
    assert back.is_done("bumped") and back.data["release_commit"] == "def" and not back.is_done("tagged")
    p.write_text("{not json", encoding="utf-8")
    assert g.Journal.load(p) is None
    with pytest.raises(ValueError):
        j.done("not-a-step")


def _release_commit(repo: Path, version="0.7.28", extra: str | None = None) -> g.Journal:
    _release_files(repo)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "android-app/app/build.gradle.kts").write_text("bumped", encoding="utf-8")
    if extra:
        (repo / extra).write_text("sneaky", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"Release v{version}")
    j = g.Journal(repo / "j.json", version, base)
    j.done("bumped")
    j.done("committed", release_commit=_git(repo, "rev-parse", "HEAD"))
    return j


def test_rollback_drops_an_unpushed_release_commit_and_its_tag(repo):
    j = _release_commit(repo)
    _git(repo, "tag", "v0.7.28")
    j.done("tagged")
    actions = g.rollback(j, repo)
    assert _git(repo, "rev-parse", "HEAD") == j.base_head
    assert _git(repo, "tag", "-l", "v0.7.28") == ""
    assert any("dropped" in a for a in actions) and any("deleted local tag" in a for a in actions)


def test_rollback_refuses_when_more_than_version_bumps_are_in_the_commit(repo):
    j = _release_commit(repo, extra="notes.txt")
    head = _git(repo, "rev-parse", "HEAD")
    actions = g.rollback(j, repo)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert any("NOT rolled back" in a and "notes.txt" in a for a in actions)


def test_rollback_refuses_when_someone_committed_on_top(repo):
    j = _release_commit(repo)
    (repo / "later.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later work")
    head = _git(repo, "rev-parse", "HEAD")
    actions = g.rollback(j, repo)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert any("NOT rolled back" in a for a in actions)


def test_rollback_never_touches_pushed_work_and_points_at_resume(repo):
    j = _release_commit(repo)
    j.done("tagged")
    j.done("pushed")
    head = _git(repo, "rev-parse", "HEAD")
    actions = g.rollback(j, repo)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert any("--resume" in a for a in actions)
    assert _git(repo, "log", "-1", "--format=%s") == "Release v0.7.28"


# ---- retry / classification -------------------------------------------------- #
@pytest.mark.parametrize("text,kind", [
    ("The process cannot access the file because it is being used by another process. (os error 32)", "lock"),
    ("PermissionError: [WinError 5] Access is denied", "lock"),
    ("fatal: unable to access 'https://github.com/x': Could not resolve host: github.com", "network"),
    ("error: RPC failed; HTTP 503 ... The remote end hung up unexpectedly", "network"),
    ("error[E0308]: mismatched types", "fatal"),
    ("FAILED tests/test_x.py::test_y - assert 502 == 200", "fatal"),
])
def test_failure_classification(text, kind):
    assert g.classify_failure(text) == kind


def _runner(results):
    calls = []

    def run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        return results[min(len(calls) - 1, len(results) - 1)]
    return run, calls


def test_a_lock_failure_heals_then_retries_with_backoff():
    run, calls = _runner([g.CmdResult(1, "os error 32"), g.CmdResult(1, "os error 32"), g.CmdResult(0, "ok")])
    healed, sleeps = [], []
    res = g.run_with_retry(["x"], attempts=3, base_delay=2, heal=lambda: healed.append(1),
                           sleep=sleeps.append, runner=run, log=lambda s: None)
    assert res.ok and len(calls) == 3 and len(healed) == 2 and sleeps == [2, 4]


def test_a_real_failure_is_not_retried():
    run, calls = _runner([g.CmdResult(1, "error[E0308]: mismatched types")])
    res = g.run_with_retry(["x"], attempts=3, sleep=lambda s: None, runner=run, log=lambda s: None)
    assert not res.ok and len(calls) == 1


def test_retries_give_up_after_the_attempt_limit():
    run, calls = _runner([g.CmdResult(1, "connection reset by peer")])
    res = g.run_with_retry(["x"], attempts=3, sleep=lambda s: None, runner=run, log=lambda s: None)
    assert not res.ok and len(calls) == 3


def test_run_cmd_captures_output_and_kills_a_hung_command():
    ok = g.run_cmd([sys.executable, "-c", "print('hello')"], echo=False)
    assert ok.ok and "hello" in ok.output
    hung = g.run_cmd([sys.executable, "-c", "import time;time.sleep(30)"], timeout=1, echo=False)
    assert hung.rc == 124 and "killed after" in hung.output
    assert g.run_cmd(["definitely-not-a-real-tool-xyz"], echo=False).rc == 127


# ---- smoke test -------------------------------------------------------------- #
_SERVER = textwrap.dedent('''
    import http.server, json, os
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    http.server.HTTPServer(("127.0.0.1", int(os.environ["DASHBOARD_PORT"])), H).serve_forever()
''')


def _stage(tmp_path, main_py: str) -> Path:
    stage = tmp_path / "stage"
    (stage / "bot").mkdir(parents=True)
    (stage / "bot" / "__init__.py").write_text("", encoding="utf-8")
    (stage / "bot" / "main.py").write_text(main_py, encoding="utf-8")
    return stage


def test_smoke_test_passes_for_a_bundle_that_serves_healthz(tmp_path):
    ok, detail = g.smoke_test_bundle(_stage(tmp_path, _SERVER), timeout=30, python=Path(sys.executable))
    assert ok, detail


def test_smoke_test_reports_a_bundle_that_crashes_on_start(tmp_path):
    ok, detail = g.smoke_test_bundle(_stage(tmp_path, "raise SystemExit('boom: missing module')"),
                                     timeout=30, python=Path(sys.executable))
    assert not ok and "exited with code" in detail and "boom" in detail


def test_smoke_test_times_out_on_a_bundle_that_never_answers(tmp_path):
    ok, detail = g.smoke_test_bundle(_stage(tmp_path, "import time\ntime.sleep(60)"),
                                     timeout=3, python=Path(sys.executable))
    assert not ok and "timed out" in detail


def test_smoke_test_rejects_an_incomplete_bundle(tmp_path):
    ok, detail = g.smoke_test_bundle(tmp_path / "nothing")
    assert not ok and "incomplete" in detail


# ---- pre-push handshake ------------------------------------------------------ #
def test_pipeline_is_skipped_only_for_the_exact_commit_that_passed(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    assert g.pipeline_already_passed(repo, {g.PIPELINE_PASSED_ENV: sha}) == sha
    assert g.pipeline_already_passed(repo, {g.PIPELINE_PASSED_ENV: "0" * 40}) is None
    assert g.pipeline_already_passed(repo, {}) is None
    (repo / "x.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "moved on")
    assert g.pipeline_already_passed(repo, {g.PIPELINE_PASSED_ENV: sha}) is None


def test_rollback_restores_uncommitted_version_bumps(repo):
    _release_files(repo)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "android-app/app/build.gradle.kts").write_text("bumped", encoding="utf-8")
    j = g.Journal(repo / "j.json", "0.7.28", base)
    j.done("bumped")
    actions = g.rollback(j, repo)
    assert (repo / "android-app/app/build.gradle.kts").read_text(encoding="utf-8") == "g"
    assert any("restored" in a for a in actions)
    assert g.check_clean_tree(repo).ok


def test_retry_reevaluates_a_callable_command_each_attempt():
    seen = []

    def command():
        seen.append(len(seen))
        return ["create"] if not seen[1:] else ["upload"]

    calls = []

    def run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        return g.CmdResult(1, "connection reset") if len(calls) == 1 else g.CmdResult(0, "ok")

    res = g.run_with_retry(command, attempts=3, sleep=lambda s: None, runner=run, log=lambda s: None)
    assert res.ok and calls == [["create"], ["upload"]]


def test_cargo_lock_sync_only_reports_in_a_dry_run(repo):
    lock = _cargo(repo, "0.7.26", "\n")
    before = lock.read_bytes()
    c = g.sync_cargo_lock("0.7.28", repo, apply=False)
    assert c.ok and c.warn and "would update" in c.detail
    assert lock.read_bytes() == before
