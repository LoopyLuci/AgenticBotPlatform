"""LF8: build and test caches. Content keys change with every input; only passes are stored; a cache never hides a failure (the
integrator re-runs a file whose inputs changed, and a deploy run in the pipeline never looks anything up). Real git and real pytest
for the integrator; an in-memory stand-in for CacheIt's hub."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.conductor import buildcache, integrator
from bot.conductor import cache as cachelib

from tests.test_conductor_integ import PYTEST, branch, git, write

ROOT = Path(__file__).resolve().parents[1]


class FakeHub:
    """cache.get / cache.put / cache.put_cas over a dict, like CacheIt's hub answers them."""

    def __init__(self):
        self.data: dict[tuple[str, str], bytes] = {}
        self.calls: list[str] = []

    def __call__(self, op, args):
        self.calls.append(op)
        ns = args["namespace"]
        if op == "cache.get":
            v = self.data.get((ns, args["key"]))
            if v is None:
                return {"found": False}
            if args.get("encoding") == "base64":
                return {"found": True, "value_b64": base64.b64encode(v).decode()}
            return {"found": True, "value": v.decode()}
        raw = base64.b64decode(args["value_b64"]) if "value_b64" in args else args["value"].encode()
        if op == "cache.put":
            self.data[(ns, args["key"])] = raw
            return {"stored": True}
        if op == "cache.put_cas":
            key = hashlib.sha256(raw).hexdigest()
            self.data[(ns, key)] = raw
            return {"key": key}
        raise AssertionError(op)


@pytest.fixture(autouse=True)
def _fixed_interpreter(monkeypatch):
    monkeypatch.setattr(buildcache, "_interpreter_facts", lambda python: "3.x\nplatform\npytest==9\n")


def _tc(hub=None):
    return buildcache.TestCache(cache=cachelib.Cache(backend=hub or FakeHub()))


def _tree(root):
    write(root, "pytest.ini", "[pytest]\npythonpath = .\n")
    write(root, "bot/__init__.py", "")
    write(root, "bot/core.py", "from bot import util\n\ndef answer():\n    return util.base() + 1\n")
    write(root, "bot/util.py", "def base():\n    return 41\n")
    write(root, "bot/other.py", "X = 1\n")
    write(root, "tests/test_core.py", "from bot.core import answer\n\ndef test_answer():\n    assert answer() == 42\n")
    write(root, "tests/test_other.py", "from bot.other import X\n\ndef test_x():\n    assert X == 1\n")


# ------------------------------------------------------------------ keys
def test_a_key_changes_with_every_input_and_only_then(tmp_path, monkeypatch):
    _tree(tmp_path)

    def key(f="tests/test_core.py"):
        return buildcache.TestKeys(tmp_path).key(f)

    k0 = key()
    assert key() == k0 and key("tests/test_other.py") != k0
    write(tmp_path, "bot/other.py", "X = 1  # unrelated\n")
    assert key() == k0                                               # a file test_core never imports
    write(tmp_path, "bot/util.py", "def base():\n    return 40\n")
    k1 = key()
    assert k1 != k0                                                  # a transitive import (core -> util)
    write(tmp_path, "tests/conftest.py", "import os\n")
    k2 = key()
    assert k2 != k1                                                  # test infrastructure
    write(tmp_path, "bot/data.json", "{}")
    k3 = key()
    assert k3 != k2                                                  # a non-Python file under an indexed root
    write(tmp_path, "lib/helper.py", "Y = 1\n")
    assert key() != k3                                               # Python outside the indexed roots (not in the graph)
    k3 = key()
    write(tmp_path, "requirements.lock", "x==1\n")
    k4 = key()
    assert k4 != k3                                                  # the lock file
    monkeypatch.setenv("ABP_SOMETHING", "1")
    k5 = key()
    assert k5 != k4                                                  # the ABP_ environment
    monkeypatch.setattr(buildcache, "_interpreter_facts", lambda python: "3.y\nplatform\npytest==9\n")
    assert key() != k5                                               # the interpreter and its packages


def test_an_interpreter_that_cannot_be_asked_never_matches(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(buildcache, "_interpreter_facts", lambda python: "")
    assert buildcache.TestKeys(tmp_path).key("tests/test_core.py") != buildcache.TestKeys(tmp_path).key("tests/test_core.py")


# ------------------------------------------------------------- the cache
def test_only_passes_are_stored_and_flaky_files_never(tmp_path):
    _tree(tmp_path)
    tc, keys = _tc(), buildcache.TestKeys(tmp_path)
    files = ["tests/test_core.py", "tests/test_other.py"]
    cached, run, kmap = tc.partition(keys, files)
    assert cached == [] and run == files
    assert tc.record_passes(kmap, files, 4.0, exclude=["tests/test_other.py::test_x"]) == 1
    cached, run, _ = tc.partition(buildcache.TestKeys(tmp_path), files)
    assert cached == ["tests/test_core.py"] and run == ["tests/test_other.py"]
    write(tmp_path, "bot/util.py", "def base():\n    return 0\n")    # the cached file's input changed: it runs again
    cached, run, _ = tc.partition(buildcache.TestKeys(tmp_path), files)
    assert cached == [] and run == files


def test_a_down_or_disabled_cache_runs_everything(tmp_path):
    _tree(tmp_path)

    def down(op, args):
        raise OSError("hub down")

    files = ["tests/test_core.py"]
    for tc in (buildcache.TestCache(cache=cachelib.Cache(backend=down)), buildcache.TestCache(enabled=False, cache=cachelib.Cache(backend=FakeHub()))):
        cached, run, kmap = tc.partition(buildcache.TestKeys(tmp_path), files)
        assert cached == [] and run == files
        tc.record_passes(kmap, files)


def test_commands_that_deselect_are_never_per_file_passes():
    assert buildcache.whole_files(PYTEST)
    for extra in (["-k", "x"], ["-m", "slow"], ["--lf"], ["tests/a.py::test_one"], ["-k=x"]):
        assert not buildcache.whole_files([*PYTEST, *extra])


def test_narrow_swaps_the_files_or_the_tests_folder():
    assert buildcache.narrow(PYTEST, ["tests/a.py", "tests/b.py"], ["tests/b.py"]) == [*PYTEST[:3], "tests/b.py", *PYTEST[4:]]
    cmd = ["py", "-m", "pytest", "tests/a.py", "tests/b.py", "-q"]
    assert buildcache.narrow(cmd, ["tests/a.py", "tests/b.py"], ["tests/b.py"]) == ["py", "-m", "pytest", "tests/b.py", "-q"]


# ------------------------------------------- the integrator (the Factory)
def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    _tree(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def _integ(repo, tmp_path, hub, files, n):
    b = branch(repo, f"feat/{n}", files)
    integ = integrator.Integrator(repo, base="main", workdir=tmp_path / "wt", suite=PYTEST, selective_first=False,
                                  cache=cachelib.Cache(enabled=False), test_cache=_tc(hub))
    return integ.integrate([b])


def test_a_second_factory_run_reuses_passes_and_a_real_failure_still_fails(tmp_path):
    repo, hub = _repo(tmp_path), FakeHub()
    first = _integ(repo, tmp_path, hub, {"bot/unused.py": "Z = 1\n"}, 1)
    assert first.status == "green", first.tail
    assert first.cache["file_hits"] == 0 and first.cache["file_stores"] == 2

    second = _integ(repo, tmp_path, hub, {"bot/unused.py": "Z = 2\n"}, 2)   # code no test imports: every key is the same
    assert second.status == "green" and second.cache["file_hits"] == 2
    assert "[cached pass]" in second.tail and second.shards[0]["cached_files"] == 2

    third = _integ(repo, tmp_path, hub, {"bot/util.py": "def base():\n    return 0\n"}, 3)   # breaks test_core through an import
    assert third.status == "red", third.tail
    assert "tests/test_core.py::test_answer" in third.failed
    assert third.cache["file_hits"] == 1                                 # test_other is untouched and reused; test_core ran
    assert third.cache["file_stores"] == 0                               # a failing run stores nothing


def test_a_failure_is_never_stored_so_it_fails_again(tmp_path):
    repo, hub = _repo(tmp_path), FakeHub()
    bad = {"bot/other.py": "X = 2\n"}
    assert _integ(repo, tmp_path, hub, bad, 1).status == "red"
    again = _integ(repo, tmp_path, hub, {**bad, "bot/unused.py": "Z = 1\n"}, 2)
    assert again.status == "red" and "tests/test_other.py::test_x" in again.failed


# ------------------------------------------------------- the pipeline
@pytest.fixture
def lp(monkeypatch, tmp_path):
    scripts = ROOT / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("local_pipeline_buildcache", scripts / "local_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["local_pipeline_buildcache"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "TESTSELECT_STATE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "_run", lambda *a, **k: (True, ""))
    return mod


class _Spy:
    def __init__(self, cached):
        self.enabled, self.cached, self.partitioned, self.recorded = True, cached, [], []

    def partition(self, keys, files):
        self.partitioned.append(list(files))
        return [f for f in files if f in self.cached], [f for f in files if f not in self.cached], {}

    def record_passes(self, kmap, files, seconds=0.0, exclude=()):
        self.recorded.append(list(files))
        return len(files)


def _plan(lp, monkeypatch, tests, full, spy):
    state = SimpleNamespace(record=lambda *a, **k: None)
    sel = SimpleNamespace(tests=tests, full=False)
    monkeypatch.setattr(lp, "_test_plan", lambda: (sel, False, full, state))
    monkeypatch.setattr(lp, "_test_cache", lambda py: (spy, lambda: SimpleNamespace(key=lambda f: "k:" + f)))
    stages = []
    monkeypatch.setattr(lp, "_pytest_stage", lambda py, targets, has_dev, **kw: stages.append(list(targets)) or True)
    return stages


def test_a_deploy_run_never_looks_up_the_cache(lp, monkeypatch):
    spy = _Spy(cached={"tests/test_a.py"})
    monkeypatch.setattr(lp, "_FULL_SUITE_REQUIRED", "this run deploys")
    stages = _plan(lp, monkeypatch, ["tests/test_a.py"], True, spy)
    assert lp._check_tests_and_audit("py")
    assert stages == [[]]                                # the whole suite
    assert spy.partitioned == []                         # no lookup at all
    assert spy.recorded and spy.recorded[0]              # a clean full run still records its passes


def test_a_selected_non_deploy_run_skips_cached_files_only(lp, monkeypatch):
    spy = _Spy(cached={"tests/test_a.py"})
    monkeypatch.setattr(lp, "_FULL_SUITE_REQUIRED", "")
    stages = _plan(lp, monkeypatch, ["tests/test_a.py", "tests/test_b.py"], False, spy)
    assert lp._check_tests_and_audit("py")
    assert stages == [["tests/test_b.py"]] and spy.recorded == [["tests/test_b.py"]]


def test_a_failing_selected_run_records_nothing(lp, monkeypatch):
    spy = _Spy(cached=set())
    monkeypatch.setattr(lp, "_FULL_SUITE_REQUIRED", "")
    _plan(lp, monkeypatch, ["tests/test_b.py"], False, spy)
    monkeypatch.setattr(lp, "_pytest_stage", lambda *a, **k: False)
    assert not lp._check_tests_and_audit("py")
    assert spy.recorded == []


# ------------------------------------------------------ wheels and cargo
def test_a_wheelhouse_refills_from_cacheit_without_pip(tmp_path, monkeypatch):
    hub = FakeHub()
    c = cachelib.Cache(backend=hub)
    lock = tmp_path / "requirements.lock"
    lock.write_text("x==1\n", encoding="utf-8")
    built = []

    def fake_pip(cmd, **kw):
        built.append(cmd)
        folder = Path(cmd[cmd.index("-w") + 1])
        (folder / "x-1-py3-none-any.whl").write_bytes(b"wheel bytes")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(buildcache.subprocess, "run", fake_pip)
    first = buildcache.wheelhouse(lock, cache=c, root=tmp_path / "a")
    assert first and len(built) == 1
    again = buildcache.wheelhouse(lock, cache=c, root=tmp_path / "b")   # an empty local folder: CacheIt refills it
    assert again and len(built) == 1 and (again / "x-1-py3-none-any.whl").read_bytes() == b"wheel bytes"
    manifest = json.loads(hub.data[(buildcache.NS_BUILD, "wheels:" + buildcache.lock_key([lock]))])
    assert manifest["wheels"][0]["name"] == "x-1-py3-none-any.whl"


def test_cargo_targets_follow_the_crate_content(tmp_path):
    crate = tmp_path / "crate"
    write(crate, "Cargo.toml", "[package]\nname='x'\n")
    write(crate, "src/lib.rs", "pub fn a() {}\n")
    write(crate, "target/debug/junk", "ignored")
    cfg = {"buildcache": {"cargo": True}}
    one = buildcache.cargo_env(crate, cfg=cfg, root=tmp_path / "c", rustc="rustc 1.90")
    assert one == buildcache.cargo_env(crate, cfg=cfg, root=tmp_path / "c", rustc="rustc 1.90")
    write(crate, "target/debug/junk", "still ignored")
    assert one == buildcache.cargo_env(crate, cfg=cfg, root=tmp_path / "c", rustc="rustc 1.90")
    write(crate, "src/lib.rs", "pub fn b() {}\n")
    assert one != buildcache.cargo_env(crate, cfg=cfg, root=tmp_path / "c", rustc="rustc 1.90")
    assert buildcache.cargo_env(crate, cfg=cfg, root=tmp_path / "c", rustc="rustc 1.91") != one
    assert buildcache.cargo_env(crate, cfg={"buildcache": {"cargo": False}}, root=tmp_path / "c", rustc="x") == {}
    assert buildcache.cargo_env(crate, cfg=cfg, root=tmp_path / "c", rustc="") == {}


def test_venvs_are_off_by_default_and_need_requirements(tmp_path):
    assert buildcache.settings({})["venvs"] is False
    assert buildcache.venv_for(tmp_path, cfg={"buildcache": {"venvs": False}}) is None
    assert buildcache.venv_for(tmp_path, cfg={"buildcache": {"venvs": True}}) is None     # no requirements file


# ------------------------------------------------------------- the API
def test_the_buildcache_api_reads_and_audits_switches(monkeypatch, temp_db):
    from fastapi.testclient import TestClient

    from bot import db
    from bot.config import config
    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "t" * 48}
    assert c.get("/api/conductor/buildcache").status_code == 401
    doc = c.get("/api/conductor/buildcache", headers=h).json()
    assert set(doc["switches"]) == set(buildcache.SWITCHES) and "tests" in doc["settings"]
    changed = {}
    monkeypatch.setattr(config, "set_values", lambda ch, actor="": changed.update(ch))
    assert c.put("/api/conductor/buildcache/settings", headers=h, json={"tests": "no"}).status_code == 400
    assert c.put("/api/conductor/buildcache/settings", headers=h, json={}).status_code == 400
    assert c.put("/api/conductor/buildcache/settings", headers=h, json={"tests": False}).status_code == 200
    assert changed == {("conductor", "buildcache", "tests"): False}
    assert db.list_audit_log(["conductor_buildcache_set"])
