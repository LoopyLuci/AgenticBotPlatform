"""LF4 phase 2b: the integrator regenerates the SDK and runs a sharded, budgeted, machine-locked suite; the pusher acts only on an
approval. Real git (a bare repository stands in for origin), real pytest, real processes for the lock."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bot import conductor
from bot.conductor import integrator, pusher, run, shards, store

ROOT = Path(__file__).resolve().parents[1]
PASS = [sys.executable, "-c", "pass"]
PYTEST = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rf"]


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
                           *args], check=check, capture_output=True, text=True).stdout


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "pytest.ini", "[pytest]\npythonpath = .\n")
    write(root, "tests/test_base.py", "def test_base():\n    assert True\n")
    write(root, "notes.txt", "keep me\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def origin(repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    return bare


def branch(repo, name, files, start="main"):
    git(repo, "checkout", "-q", "-b", name, start)
    for rel, text in files.items():
        write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    git(repo, "checkout", "-q", "main")
    return name


def integrate(repo, tmp_path, files=None, **kw):
    b = branch(repo, f"feat/x{len(list(tmp_path.glob('wt/*')))}", files or {"extra.py": "Z = 1\n"})
    kw.setdefault("suite", PASS)
    return integrator.Integrator(repo, base="main", workdir=tmp_path / "wt", **kw).integrate([b])


ON = {"enabled": True, "remote": "origin", "target": ""}


# ------------------------------------------------------------ budget and shards
def test_the_budget_follows_idle_cpus_and_free_memory():
    gb = 1024 ** 3
    assert shards.Budget.measure(8, cpus=16, busy_pct=0, free_ram=64 * gb).workers == 8
    b = shards.Budget.measure(8, cpus=16, busy_pct=75, free_ram=64 * gb)
    assert b.workers == 4 and b.limited_by == "cpu"
    b = shards.Budget.measure(8, cpus=16, busy_pct=0, free_ram=3 * gb)
    assert b.workers == 2 and b.limited_by == "memory"
    assert shards.Budget.measure(8, cpus=4, busy_pct=100, free_ram=gb // 4).workers == 1          # never less than one
    assert shards.with_workers(["py", "-m", "pytest", "tests", "-n", "8", "-q"], 3) == ["py", "-m", "pytest", "tests", "-n", "3", "-q"]
    assert shards.with_workers(["pytest", "-n4"], 2) == ["pytest", "-n2"]
    assert shards.with_workers(["pytest", "tests"], 2) == ["pytest", "tests"]                    # no -n: left alone


def test_files_are_split_into_balanced_shards():
    files = [("tests/a.py", 100), ("tests/b.py", 90), ("tests/c.py", 10), ("tests/d.py", 5)]
    groups = shards.split_files(files, 2)
    assert sorted(sum(groups, [])) == sorted(p for p, _ in files) and len(groups) == 2
    assert {tuple(g) for g in groups} == {("tests/a.py", "tests/d.py"), ("tests/b.py", "tests/c.py")}
    assert len(shards.split_files(files[:1], 3)) == 1                                              # no empty shards
    cmds = shards.shard_commands(["py", "-m", "pytest", "tests", "-q"], [["tests/a.py"], ["tests/b.py"]])
    assert cmds == [["py", "-m", "pytest", "tests/a.py", "-q"], ["py", "-m", "pytest", "tests/b.py", "-q"]]


def test_a_sharded_suite_records_each_shard_and_still_tells_a_flake_from_a_failure(repo, tmp_path, monkeypatch):
    mark = tmp_path / "flake.mark"
    monkeypatch.setenv("FLAKE_MARK", str(mark))
    files = {"tests/test_flaky.py": ("import os\nimport pathlib\n\n\ndef test_flaky():\n    m = pathlib.Path(os.environ['FLAKE_MARK'])\n"
                                     "    if not m.exists():\n        m.write_text('x')\n        assert False, 'first run'\n"),
             "tests/test_two.py": "def test_two():\n    assert 1\n", "tests/test_three.py": "def test_three():\n    assert 1\n"}
    res = integrate(repo, tmp_path, files, suite=PYTEST, shards=3, workers=1, selective_first=False)
    assert res.status == "green" and [s["name"] for s in res.shards] == ["shard 1/3", "shard 2/3", "shard 3/3"]
    assert sum(s["files"] for s in res.shards) == 4 and sorted(s["rc"] for s in res.shards) == [0, 0, 1]
    assert [t.split("::")[-1] for t in res.flaky] == ["test_flaky"] and res.failed == []
    assert all(s["tests_run"] for s in res.shards) and "passed" in res.tests_run
    assert Path(res.log).read_text(encoding="utf-8").count("=====") >= 6                              # a log section per shard

    mark.unlink()
    bad = integrate(repo, tmp_path, {"tests/test_broken.py": "def test_broken():\n    assert 1 == 2\n"}, suite=PYTEST, shards=2)
    assert bad.status == "red" and [t.split("::")[-1] for t in bad.failed] == ["test_broken"]
    assert len(store.list_queue()) == 1                                                                 # red is never queued


def test_a_shard_that_crashes_makes_the_run_red(repo, tmp_path):
    res = integrate(repo, tmp_path, {"tests/test_syntax.py": "def broken(:\n"}, suite=PYTEST, shards=2)
    assert res.status == "red" and any(s["rc"] not in (0, 1) or s["rc"] == 1 for s in res.shards)
    assert store.list_queue() == []


def test_the_worker_count_comes_from_the_budget(repo, tmp_path):
    res = integrate(repo, tmp_path, suite=[sys.executable, "-c", "import sys; print(sys.argv)", "-n", "8"], workers=2)
    assert res.budget["workers"] == 2 and res.status == "green"
    res = integrate(repo, tmp_path, suite=PASS, workers="auto", max_workers=3)
    assert 1 <= res.budget["workers"] <= 3


def test_the_final_gate_decides_after_a_green_suite(repo, tmp_path):
    ok = integrate(repo, tmp_path, gate=PASS)
    assert ok.status == "green" and ok.gate["rc"] == 0 and ok.queue_id
    bad = integrate(repo, tmp_path, gate=[sys.executable, "-c", "import sys; print('nope'); sys.exit(3)"])
    assert bad.status == "red" and bad.gate["rc"] == 3 and "final gate" in bad.note and not bad.queue_id


# ---------------------------------------------------------------------- the SDK
EXPORT = ("import pathlib\nroot = pathlib.Path(__file__).resolve().parent.parent\n(root / 'docs' / 'api').mkdir(parents=True, exist_ok=True)\n"
          "n = len(list((root / 'bot' / 'dashboard').glob('*.py'))) if (root / 'bot' / 'dashboard').is_dir() else 0\n"
          "(root / 'docs' / 'api' / 'openapi.json').write_text('{\"routes\": %d}\\n' % n)\n")


def test_routes_changed_regenerates_the_sdk_and_commits_it_on_the_integration_branch(repo, tmp_path):
    write(repo, "scripts/export_openapi.py", EXPORT)
    write(repo, "docs/api/openapi.json", '{"routes": 0}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sdk")
    plain = integrate(repo, tmp_path, {"extra.py": "A = 1\n"})
    assert plain.sdk["note"] == "no route changed" and not plain.sdk["changed"]
    routed = integrate(repo, tmp_path, {"bot/dashboard/thing_api.py": "@app.get('/api/thing')\ndef f():\n    pass\n"})
    assert routed.status == "green" and routed.sdk["changed"] and routed.sdk["files"] == 1
    assert git(repo, "show", f"{routed.branch}:docs/api/openapi.json").strip() == '{"routes": 1}'
    assert git(repo, "log", "-1", "--format=%s", routed.branch).startswith("Regenerate the OpenAPI document")
    assert git(repo, "rev-parse", routed.branch).strip() == routed.head_sha
    assert store.list_queue()[0]["head_sha"] == routed.head_sha                                         # the queued sha includes it
    off = integrate(repo, tmp_path, {"bot/dashboard/other_api.py": "@app.get('/api/o')\ndef g():\n    pass\n"}, regen_sdk=False)
    assert off.sdk["note"] == "off"


def test_a_failing_regeneration_is_red(repo, tmp_path):
    write(repo, "scripts/export_openapi.py", "raise SystemExit('cannot build the app')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sdk")
    res = integrate(repo, tmp_path, {"bot/dashboard/thing_api.py": "@app.get('/api/thing')\n"})
    assert res.status == "red" and "SDK regeneration failed" in res.note and store.list_queue() == []


# -------------------------------------------------------------- the machine lock
def hold_lock(path, seconds):
    code = ("import sys, time\nfrom pathlib import Path\nsys.path.insert(0, %r)\nfrom bot.conductor import shards\n"
            "with shards.machine_lock(5, Path(%r), 'other run'):\n    print('held', flush=True)\n    time.sleep(%d)\n" % (str(ROOT), str(path), seconds))
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_two_runs_never_hold_the_suite_at_once(repo, tmp_path):
    lock = tmp_path / "suite.lock"
    other = hold_lock(lock, 30)
    try:
        t0 = time.time()
        res = integrate(repo, tmp_path, lock_wait_s=1, lock_path=lock)
        assert res.status == "red" and "busy" in res.note and time.time() - t0 < 20 and store.list_queue() == []
    finally:
        other.terminate()
        other.wait(timeout=20)
    ok = integrate(repo, tmp_path, lock_wait_s=10, lock_path=lock)                                        # the holder is gone: stale lock replaced
    assert ok.status == "green" and not lock.exists()


def test_a_run_waits_for_the_lock_and_then_goes(repo, tmp_path):
    lock = tmp_path / "suite.lock"
    other = hold_lock(lock, 3)
    try:
        res = integrate(repo, tmp_path, lock_wait_s=60, lock_path=lock)
    finally:
        other.wait(timeout=30)
    assert res.status == "green"


# ------------------------------------------------------------------- the pusher
def approved(repo, tmp_path, by="owner", files=None):
    res = integrate(repo, tmp_path, files)
    assert res.status == "green", res.note
    run.approve(res.queue_id, by, "ship it")
    return res


def spy_git(monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            seen.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    return seen


def origin_main(origin):
    return git(origin, "rev-parse", "main").strip()


def test_an_approved_fresh_entry_is_pushed_as_exactly_its_sha_and_never_forced(repo, origin, tmp_path, monkeypatch):
    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)
    res = approved(repo, tmp_path)
    seen = spy_git(monkeypatch)
    entry = pusher.push(res.queue_id, "claude", config=ON)
    pushes = [c for c in seen if "push" in c]
    assert [c[c.index("push"):] for c in pushes] == [["push", "origin", f"{res.head_sha}:refs/heads/main"]]
    assert not any(a in ("--force", "-f", "--force-with-lease", "--mirror", "--delete") or a.startswith("+") for c in pushes for a in c)
    assert entry["status"] == "pushed" and origin_main(origin) == res.head_sha
    assert [(a["actor"], a["action"]) for a in store.list_audit(res.queue_id)] == [
        ("owner", "approved"), ("claude", "push_started"), ("claude", "pushed")]
    with pytest.raises(pusher.PushRefused, match="pushed"):                                              # once only
        pusher.push(res.queue_id, "claude", config=ON)


def test_an_unapproved_or_rejected_entry_is_never_pushed(repo, origin, tmp_path):
    res = integrate(repo, tmp_path)
    before = origin_main(origin)
    with pytest.raises(pusher.PushRefused, match="awaiting_approval"):
        pusher.push(res.queue_id, "claude", config=ON)
    store.decide(res.queue_id, "rejected", "owner")
    with pytest.raises(pusher.PushRefused, match="rejected"):
        pusher.push(res.queue_id, "claude", config=ON)
    with pytest.raises(pusher.PushRefused, match="who pushes"):
        pusher.push(res.queue_id, "", config=ON)
    with pytest.raises(KeyError):
        pusher.push("nope", "claude", config=ON)
    assert origin_main(origin) == before
    assert [a["action"] for a in store.list_audit(res.queue_id)].count("push_refused") == 2


def test_the_pusher_is_off_by_default(repo, origin, tmp_path):
    res = approved(repo, tmp_path)
    assert conductor.settings()["pusher"]["enabled"] is False
    before = origin_main(origin)
    with pytest.raises(pusher.PushRefused, match="disabled"):
        pusher.push(res.queue_id, "claude")
    with pytest.raises(pusher.PushRefused, match="disabled"):
        run.push(res.queue_id, "claude")
    assert origin_main(origin) == before and store.get_queue_entry(res.queue_id)["status"] == "approved"


def test_a_branch_that_moved_after_the_approval_is_refused(repo, origin, tmp_path):
    res = approved(repo, tmp_path)
    other = branch(repo, "feat/sneaky", {"sneaky.py": "X = 1\n"})
    git(repo, "update-ref", f"refs/heads/{res.branch}", git(repo, "rev-parse", other).strip())
    before = origin_main(origin)
    with pytest.raises(pusher.PushRefused, match="changed after the approval"):
        pusher.push(res.queue_id, "claude", config=ON)
    assert origin_main(origin) == before and store.get_queue_entry(res.queue_id)["status"] == "approved"
    assert not (store.get_queue_entry(res.queue_id)["head_sha"] == git(repo, "rev-parse", other).strip())


def test_a_remote_that_moved_on_makes_the_entry_stale(repo, origin, tmp_path):
    res = approved(repo, tmp_path)
    newer = branch(repo, "feat/newer", {"newer.py": "N = 1\n"})
    git(repo, "push", "-q", "origin", f"{newer}:main")                                                   # someone else pushed first
    moved = origin_main(origin)
    with pytest.raises(pusher.PushStale, match="stale"):
        pusher.push(res.queue_id, "claude", config=ON)
    assert origin_main(origin) == moved and store.get_queue_entry(res.queue_id)["status"] == "stale"
    with pytest.raises(pusher.PushRefused, match="stale"):                                               # a re-run is needed
        pusher.push(res.queue_id, "claude", config=ON)
    assert "stale" in [a["action"] for a in store.list_audit(res.queue_id)]


def test_a_leak_in_the_exact_tree_stops_the_push(repo, origin, tmp_path, monkeypatch):
    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)
    leaked = "Q" + ":\\" + "Users" + "\\" + "bob" + "\\" + "project" + "\\" + "file.txt"
    res = approved(repo, tmp_path, files={"doc.txt": f"see {leaked}\n"})
    before = origin_main(origin)
    with pytest.raises(pusher.PushRefused, match="leak guard"):
        pusher.push(res.queue_id, "claude", config=ON)
    assert origin_main(origin) == before
    entry = store.get_queue_entry(res.queue_id)
    assert entry["status"] == "approved"                                                                  # nothing pushed, nothing lost
    assert [a["action"] for a in store.list_audit(res.queue_id)][-1] == "push_failed"
    assert "abp-push-" not in git(repo, "worktree", "list")                                               # the scan tree is gone


def test_a_remote_that_cannot_be_reached_pushes_nothing(repo, origin, tmp_path):
    res = approved(repo, tmp_path)
    with pytest.raises(pusher.PushRefused, match="could not fetch"):
        pusher.push(res.queue_id, "claude", config={**ON, "remote": "nowhere"})
    assert store.get_queue_entry(res.queue_id)["status"] == "approved"


def test_the_api_cli_and_mcp_push(repo, origin, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from abp_cli import __main__ as cli
    from bot.dashboard.server import build_app

    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "t" * 48}
    res = approved(repo, tmp_path)
    assert c.post(f"/api/conductor/queue/{res.queue_id}/push", json={"by": "o"}).status_code == 401
    assert c.post(f"/api/conductor/queue/{res.queue_id}/push", headers=h, json={"by": "claude"}).status_code == 403    # off by default
    assert c.post("/api/conductor/queue/zzz/push", headers=h, json={"by": "claude"}).status_code == 404
    real = conductor.settings
    monkeypatch.setattr(conductor, "settings", lambda: {**real(), "pusher": ON})
    out = c.post(f"/api/conductor/queue/{res.queue_id}/push", headers=h, json={"by": "claude"})
    assert out.status_code == 200 and out.json()["status"] == "pushed" and origin_main(origin) == res.head_sha
    audit = c.get(f"/api/conductor/queue/{res.queue_id}/audit", headers=h).json()["audit"]
    assert [a["action"] for a in audit] == ["approved", "push_refused", "push_started", "pushed"]
    assert c.get("/api/conductor/queue/zzz/audit", headers=h).status_code == 404

    stale = approved(repo, tmp_path)
    git(repo, "push", "-q", "origin", f"{branch(repo, 'feat/moved', {'m.py': 'M = 1'}, start=origin_main(origin))}:main")
    assert c.post(f"/api/conductor/queue/{stale.queue_id}/push", headers=h, json={"by": "claude"}).status_code == 409

    parsed = cli._parser().parse_args(["conductor", "push", "abc", "--by", "owner"])
    assert parsed.conductor_cmd == "push" and parsed.by == "owner" and parsed.queue_id == "abc"

    class Fake:
        async def _request(self, method, path, params=None, json=None):
            r = c.request(method, path, headers=h, params=params, json=json)
            r.raise_for_status()
            return r.json()

    class Args:
        json = True
        conductor_cmd = "push"
        queue_id = res.queue_id
        by = "claude"

    with pytest.raises(Exception):                                                                        # already pushed -> 403 from the API
        asyncio.run(cli._conductor(Args(), Fake()))

    from bot import mcp_server

    seen = []

    async def fake(method, path, timeout=15.0, **kw):
        seen.append((method, path, kw.get("json")))
        return {}

    monkeypatch.setattr(mcp_server, "_request", fake)
    asyncio.run(mcp_server.conductor_push("q9", "claude"))
    assert seen == [("POST", "/api/conductor/queue/q9/push", {"by": "claude"})]
