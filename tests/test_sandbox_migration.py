"""The call sites the Sandbox Nervous System surface moved over, against the real processes they start.

Every test here starts a process through the *site's own* code path (no stub of `spawn()`, the registry or the cell),
then checks what the registry wrote down about it - owner, cell, policy - and that stopping that cell stops the process
**and** the child it started. The isolation is tests/test_sandbox_ns.py's: the guard is installed, and the state file is
this test's own tmp folder, never the checkout's `data/sandbox_ns/live.json`.

**The process a site starts is not always the process that does the work.** On Windows a venv's `Scripts/python.exe` is a
launcher: it starts the base interpreter as a child and waits for it, and that child is what runs the build step, the
training worker, the hub. So `record_of()` waits for the record - the nervous system learns about a descendant on a
sampling pass, not at the spawn - and `assert_one_tree()` checks the shape that has to hold either way: every process
below the spawn is recorded under the same owner, cell and policy, and points up at the process that started it.

One stand-in executable is used everywhere (`agent.cmd` on Windows, `agent` elsewhere): it either answers a run with
the JSON a CLI backend parses, or - `ABP_SNS_TEST_MODE=hang` - starts a grandchild that would outlive it, writes
`<its pid> <the child's pid>` to `ABP_SNS_TEST_PIDS`, and hangs. The child's stdout is DEVNULL on purpose: a child
that inherited the caller's pipe would keep `communicate()` open after the tree was killed, which is a hang rather
than a failure.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from bot.sandbox_ns import guard, registry as registry_mod
from bot.sandbox_ns.cell import cell_for
from bot.sandbox_ns.registry import registry

PIDS = "ABP_SNS_TEST_PIDS"
MODE = "ABP_SNS_TEST_MODE"

STANDIN = '''\
import json, os, subprocess, sys, time

def hang():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(os.environ["ABP_SNS_TEST_PIDS"], "w") as f:
        f.write("%d %d" % (os.getpid(), p.pid))
    time.sleep(120)

if os.environ.get("ABP_SNS_TEST_MODE") == "hang":
    hang()
else:
    print(json.dumps({"result": "hi from the stand-in"}))
'''

SLEEPER_WITH_CHILD = STANDIN      # the same program is what a build, a worker and a daemon run


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """The guard on, and this test's own state file - a parallel worker must never reap, or be
    reaped by, another's records."""
    guard.install()
    monkeypatch.setattr(registry_mod.registry, "_path", tmp_path / "sandbox_ns" / "live.json")
    monkeypatch.setenv(PIDS, str(tmp_path / "pids.txt"))
    monkeypatch.setenv(MODE, "hang")
    return tmp_path


@pytest.fixture
def standin(isolated, tmp_path, monkeypatch):
    script = tmp_path / "stand_in.py"
    script.write_text(STANDIN, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "agent.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "agent"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return type("Standin", (), {"script": script, "exe": exe})


# ---- what every test needs to see --------------------------------------------------------------------------------
def _alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:  # noqa: BLE001
        return False


def wait_gone(pids, timeout: float = 20.0) -> list:
    """Which of `pids` are still alive after waiting for them to go."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = [p for p in pids if _alive(p)]
        if not left:
            return []
        time.sleep(0.2)
    return [p for p in pids if _alive(p)]


def read_pids(path: Path, timeout: float = 20.0) -> tuple[int, int]:
    """`(the stand-in's pid, its child's pid)`, once it has written them."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                own, child = text.split()
                return int(own), int(child)
        except (OSError, ValueError):            # not written yet, or half written
            pass
        time.sleep(0.1)
    raise AssertionError(f"nothing was written to {path}")


async def async_read_pids(path: Path, timeout: float = 20.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                own, child = text.split()
                return int(own), int(child)
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.1)
    raise AssertionError(f"nothing was written to {path}")


async def async_wait_cell(owner: str, timeout: float = 20.0):
    """The open cell some site is starting a process in right now - captured while the run is
    still going, because a completed run closes (and unregisters) its cell."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        mine = [c for c in registry.cells() if c.owner == owner and not c.closed]
        if mine:
            return mine[-1]
        await asyncio.sleep(0.05)
    raise AssertionError(f"no open cell with owner {owner!r}")


def record_of(pid: int, timeout: float = 20.0):
    """The registry's record for `pid`, once it has one.

    A process that is only a *descendant* of what ABP spawned - the interpreter behind a venv
    launcher, which is the one that actually runs the work - is noticed on a sampling pass rather
    than at the spawn, so this waits for that and drives a pass itself rather than sleeping on it."""
    deadline = time.monotonic() + timeout
    while True:
        row = registry.record_for(pid)
        if row is not None:
            return row
        if time.monotonic() >= deadline:
            raise AssertionError(f"pid {pid} was started but never recorded")
        registry.sample(reflexes=False)
        time.sleep(0.05)


def records_of(pids, root: int) -> dict:
    """`{pid: record}` for every pid in a spawned tree, the spawn itself included."""
    return {pid: record_of(pid) for pid in {int(root), *(int(p) for p in pids)}}


def assert_one_tree(rows: dict, root: int, *, owner: str, cell: str, policy: str) -> None:
    """Every record in `rows` is the spawn the call site made, or something that spawn started.

    That is the shape that has to hold whether or not the process a `Popen` hands back is the one
    doing the work: on Windows under a venv it is a launcher whose child runs the build, and on a
    plain interpreter it is the worker itself. Either way the records share the one owner, cell and
    policy, and each of them says which process started it."""
    assert int(root) in rows, f"the process the site spawned ({root}) is not recorded"
    assert rows[int(root)].spawned(), "the spawn itself is recorded as somebody's descendant"
    for pid, row in rows.items():
        assert (row.owner, row.cell, row.policy) == (owner, cell, policy), row
        if int(pid) != int(root):
            assert row.parent_pid in rows, f"pid {pid} is recorded but nothing in this tree started it: {row}"


@contextlib.contextmanager
def noticing(interval: float = 0.1):
    """Keep the registry's sampling work going while a site runs.

    In the app that is the sampler's own thread, which looks at the tree a spawn started a moment
    after it. A test whose site kills its own tree inside one blocking call - a build's timeout -
    cannot wait for that, so it does the sampler's work itself for the length of the block."""
    stop = threading.Event()

    def pump() -> None:
        while not stop.wait(interval):
            registry.sample(reflexes=False)

    worker = threading.Thread(target=pump, name="test-sandbox-ns-noticing", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(10)


# ------------------------------------------------------------------ the CLI agent backends

def _cli(exe: str):
    from bot.backends.cli_backend import CliBackend

    return CliBackend(binary=exe)


def _external(exe: str):
    from bot.backends.external_agent_backend import OpenCodeBackend

    return OpenCodeBackend(binary=exe)


def _hermes(exe: str):
    from bot.backends.hermes_cli_backend import HermesCliBackend

    return HermesCliBackend(binary=exe)


BACKENDS = [
    ("cli", _cli, "backends.cli"),
    ("external", _external, "backends.external"),
    ("hermes", _hermes, "backends.hermes_cli"),
]


@pytest.mark.parametrize("kind, factory, owner", BACKENDS, ids=[b[0] for b in BACKENDS])
def test_a_backend_run_lives_in_an_agent_cell_and_its_timeout_takes_the_tree(kind, factory, owner, standin, isolated):
    """One cell per run (bot/backends/base.py's process_cell), and the timeout stops that cell -
    the CLI's child with it, which is what a bare `proc.kill()` never did."""
    from bot.backends.base import BackendError

    backend = factory(str(standin.exe))
    pids_path = Path(os.environ[PIDS])

    async def go() -> tuple:
        task = asyncio.create_task(backend.ask("do the thing", timeout_s=4))
        cell = await async_wait_cell(owner)
        pids = await async_read_pids(pids_path)
        with pytest.raises(BackendError, match="timed out"):
            await task
        return cell, pids

    cell, pids = asyncio.run(go())
    assert cell.policy.name == "agent" and not cell.policy.persistent, cell.policy
    assert cell.closed, "the timeout was supposed to take the cell with it"
    assert cell.status()["kills"] >= 1, "the cell says nothing was killed, so the tree was not stopped by it"
    row = record_of(cell.pids()[0])
    assert (row.owner, row.cell, row.policy) == (owner, cell.id, "agent"), row
    assert wait_gone([*pids, cell.pids()[0]]) == [], "the backend's timeout left its tree running"


def test_the_cli_backend_still_gets_its_answer_out_of_a_real_run(standin, isolated, monkeypatch):
    """The migration must not have changed what a caller sees: same JSON in, same text out."""
    from bot.backends.cli_backend import CliBackend

    monkeypatch.delenv(MODE)
    result = asyncio.run(CliBackend(binary=str(standin.exe)).ask("hello", timeout_s=60))
    assert result.text == "hi from the stand-in"


# ------------------------------------------------------------------ module builds

def test_a_builds_command_runs_in_its_build_cell_and_a_timeout_takes_the_tree(standin, isolated):
    from bot.modules import harness
    from bot.modules.client import ModuleError
    from bot.modules.manifest import Manifest

    m = Manifest(id="probe-mod", name="Probe", repo="https://example.invalid/probe.git")
    lines: list[str] = []
    with cell_for("build", name="probe-mod build", owner="modules.harness") as cell:
        with noticing():        # the tree the build starts is only recorded while it is running
            with pytest.raises(ModuleError):
                harness._stream(m, [sys.executable, str(standin.script)], cwd=isolated, env=dict(os.environ),
                                log=lines.append, timeout=2, cell=cell)
        pids = read_pids(Path(os.environ[PIDS]))
        spawned = next(r for r in registry.records() if r.cell == cell.id and r.spawned())
        assert spawned.argv[:2] == [sys.executable, str(standin.script)], spawned
        assert_one_tree(records_of(pids, spawned.pid), spawned.pid, owner="modules.harness",
                        cell=cell.id, policy="build")
        assert lines and lines[0].startswith("$ "), "the build's own log line is what a person reads"
        assert cell.closed and cell.status()["kills"] >= 1, "the timeout did not take the cell with it"
        # What actually stopped it is recorded against the cell, in the human words the build used.
        kills = [e for e in registry.events(limit=100, kind="kill") if e.get("cell") == cell.id]
        assert any("passed its 2s timeout" in (e.get("detail") or "") for e in kills), kills
        assert wait_gone(pids) == [], "the build's timeout stopped the compiler but not what it started"


# ------------------------------------------------------------------ training and lab workers

def test_a_training_worker_runs_in_a_worker_cell_that_stops_its_tree(standin, isolated, tmp_path, monkeypatch):
    from bot.localai import train

    monkeypatch.setenv("ABP_LOCALAI_HOME", str(tmp_path / "localai"))
    monkeypatch.setattr(train, "python", lambda: Path(sys.executable))   # no real training venv is needed to spawn
    monkeypatch.setattr(train, "WORKER", standin.script)                  # the worker script itself is not the point
    base = tmp_path / "base"
    base.mkdir()
    (base / "config.json").write_text("{}", encoding="utf-8")
    (base / "tokenizer.json").write_text("{}", encoding="utf-8")          # resolve_base's own requirements
    data = tmp_path / "rows.jsonl"
    data.write_text('{"text": "hi"}\n', encoding="utf-8")

    info = train.start(str(base), [str(data)])
    pids = read_pids(Path(os.environ[PIDS]))
    cell = train.worker_cell(info["id"])
    assert cell is not None and cell.policy.name == "worker" and not cell.policy.persistent, cell
    # `info["pid"]` is what ABP started - a venv launcher on Windows - and `pids[0]` is the
    # interpreter behind it, which is the process doing the training. Both are in the worker's cell.
    assert_one_tree(records_of(pids, int(info["pid"])), int(info["pid"]), owner="localai.train",
                    cell=cell.id, policy="worker")
    cell.kill("the test asked for it")
    assert wait_gone(pids) == [], "the training worker's cell did not take its child with it"
    assert train.worker_cell(info["id"]) is None, "a killed run must not still be holding a cell"


def test_a_lab_worker_runs_in_a_worker_cell_that_stops_its_tree(standin, isolated, tmp_path, monkeypatch):
    from bot.localai import train
    from bot.neurallab import lab, spec

    monkeypatch.setenv("ABP_LOCALAI_HOME", str(tmp_path / "localai"))
    monkeypatch.setattr(train, "python", lambda: Path(sys.executable))
    monkeypatch.setattr(lab, "WORKER", standin.script)
    data = tmp_path / "rows.jsonl"
    data.write_text('{"text": "hi"}\n', encoding="utf-8")
    design = spec.moe_regressor("probe", 3, 1, dim=16, experts=4, top_k=2, hidden=32)

    info = lab.start(design, {"path": str(data)})
    pids = read_pids(Path(os.environ[PIDS]))
    cell = lab.worker_cell(info["id"])
    assert cell is not None and cell.policy.name == "worker" and not cell.policy.persistent, cell
    assert_one_tree(records_of(pids, int(info["pid"])), int(info["pid"]), owner="neurallab.lab",
                    cell=cell.id, policy="worker")
    cell.kill("the test asked for it")
    assert wait_gone(pids) == [], "the lab worker's cell did not take its child with it"
    assert lab.worker_cell(info["id"]) is None, "a killed run must not still be holding a cell"


# ------------------------------------------------------------------ the long-running programs hosting starts

def test_hosting_starts_a_daemon_in_a_cell_and_stopping_it_takes_the_tree(standin, isolated, tmp_path):
    from bot.hosting import procs

    home = tmp_path / "hosting"
    started = procs.start("probe-daemon", [sys.executable, str(standin.script)], home=home)
    assert started["running"] is True, started
    pids = read_pids(Path(os.environ[PIDS]))
    cell = next((c for c in registry.cells() if c.owner == "hosting.procs" and c.name == "probe-daemon"
                 and not c.closed), None)
    assert cell is not None and cell.policy.persistent, cell
    # The daemon is one cell with one persistent policy, and the record covers the launcher and the
    # process it launched - a daemon that outlives ABP has to be reaped by name, not by one pid.
    rows = records_of(pids, int(started["pid"]))
    assert_one_tree(rows, int(started["pid"]), owner="hosting.procs", cell=cell.id, policy="daemon")
    assert all(r.persistent for r in rows.values()), "a daemon is recorded persistent, so the reaper leaves it"
    assert procs.stop("probe-daemon", home=home) is True, "it was running, so stop() had something to stop"
    assert wait_gone(pids) == [], "stopping the program left what it started running"
    assert procs.status("probe-daemon", home=home)["running"] is False


# ------------------------------------------------------------------ git

def test_git_calls_are_recorded_by_the_nervous_system(isolated, tmp_path):
    """bot/git_stacks.py's `_git()` goes through `spawn.run()`: no cell (one buffered git call is
    its own tree), but recorded with its owner, so the diagnostics page can say who ran git."""
    from bot import git_stacks

    git_stacks._git(["init", "-q"], cwd=tmp_path)
    out = git_stacks._git(["status"], cwd=tmp_path)
    assert "On branch" in out or "No commits yet" in out, out

    rows = [r for r in registry.records() if r.owner == "git_stacks"]
    mine = [r for r in rows if r.name in ("git init", "git status")]
    assert {r.name for r in mine} >= {"git init", "git status"}, [r.name for r in rows]
    status_row = next(r for r in mine if r.name == "git status")
    assert status_row.policy == "tool" and status_row.argv[:2] == ["git", "status"], status_row
    assert status_row.cell == "", "one buffered git call has no tree to hold: it gets no cell"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and status_row.exit_code is None:
        time.sleep(0.1)
    assert status_row.exit_code == 0, "a completed git call must have been marked with its exit code"
