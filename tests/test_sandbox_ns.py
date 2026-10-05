"""bot/sandbox_ns/ - the Sandbox Nervous System, against real processes and the real OS.

Nothing here is mocked: children are real `python` processes, the containment is a real Win32
Job Object, the reaper kills a real sleeping process, and the console questions are answered by
the child's own `GetConsoleWindow()`/`IsWindowVisible()` through ctypes. The Windows-only tests
skip elsewhere (there is no console window to pop up on Linux or macOS, and job objects and
cgroups are different mechanisms), and the pure rule functions are tested on every OS.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from bot.sandbox_ns import guard, policy as policy_mod, reflexes, registry as registry_mod, reaper
from bot.sandbox_ns.cell import Cell, cell_for
from bot.sandbox_ns.policy import Policy
from bot.sandbox_ns.registry import Registry
from bot.sandbox_ns.spawn import async_spawn, spawn

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="console windows and job objects are Windows")

# Answers the two questions this package exists to get right, in the child's own process: does it
# have a console window, and is that window visible on the desktop?
#
# GetConsoleWindow() returns NULL for a console whose window is hidden (measured on this machine:
# CREATE_NO_WINDOW and an inherited hidden console both report hwnd=0), so "did I get a hidden
# console" is answered by GetConsoleProcessList() > 0 instead - which is also the property that
# matters, because a console-less process makes every console program it starts open a new,
# visible window, while a hidden one is inherited.
PROBE = """\
import ctypes, subprocess, sys, time
out, grand = sys.argv[1], sys.argv[2]
k, u = ctypes.windll.kernel32, ctypes.windll.user32
h = k.GetConsoleWindow()
listing = (ctypes.c_ulong * 8)()
console = k.GetConsoleProcessList(listing, 8)
with open(out, "w") as f:
    f.write("hwnd=%d visible=%d console=%d\\n" % (h, u.IsWindowVisible(h), console))
if grand and grand != "no-grandchild":
    # no flags at all, exactly like the code this package is meant to make safe everywhere else:
    # the grandchild must INHERIT this process's console (hidden) rather than make a visible one
    p = subprocess.Popen([sys.executable, __file__, grand, "no-grandchild"])
    with open(grand + ".pid", "w") as f:
        f.write(str(p.pid))
    p.wait()
"""


SLEEPER = "import time; time.sleep(%d)"
# bytearray() is served from the zero page until it is written to, so an allocator that only
# reserves never commits: this one touches every page, which is what the job's memory limit counts.
EATER = ("b = bytearray({mb} * 1024 * 1024)\n"
         "for i in range(0, len(b), 4096):\n    b[i] = 1\n"
         "open({out!r}, 'w').write('ran')\n"
         "print(len(b))")


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Every test writes its state file into its own tmp folder, never the checkout's data/."""
    monkeypatch.setattr(registry_mod.registry, "_path", tmp_path / "sandbox_ns" / "live.json")
    return registry_mod.registry.path


@pytest.fixture(autouse=True)
def guard_installed():
    """The guard is a boot-time call in the real app; here every test gets it, so the windowless
    behaviour below is what ABP actually does rather than a special case."""
    guard.install()
    yield


@pytest.fixture
def probe(tmp_path):
    path = tmp_path / "probe.py"
    path.write_text(PROBE, encoding="utf-8")
    return path


def sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", SLEEPER % seconds], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_gone(pids, timeout: float = 15.0) -> list:
    """Which of `pids` are still alive after waiting for them to go."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = [p for p in pids if _alive(p)]
        if not left:
            return []
        time.sleep(0.2)
    return [p for p in pids if _alive(p)]


def _alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:  # noqa: BLE001
        return False


def read_state() -> dict:
    """The state file as it is on disk, which is the only thing the next ABP run will get to see."""
    return json.loads(registry_mod.registry.path.read_text(encoding="utf-8"))


def run_probe(script: Path, tmp_path: Path, out: str = "child.txt", grand: str = "grandchild.txt") -> tuple:
    """Run the console probe to completion and return its own line and its grandchild's."""
    with cell_for("tool", name="probe") as cell:
        proc = spawn([sys.executable, str(script), str(tmp_path / out), str(tmp_path / grand)],
                     cell=cell, name="probe", owner="test", stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True)
        assert proc.wait(timeout=60) == 0
    grandchild = tmp_path / grand
    return ((tmp_path / out).read_text(encoding="utf-8").strip(),
            grandchild.read_text(encoding="utf-8").strip() if grandchild.exists() else "")


def parse(line: str) -> tuple[int, int, int]:
    """(console window handle, is it visible, how many processes are in its console)."""
    values = dict(piece.split("=") for piece in line.split())
    return int(values["hwnd"]), int(values["visible"]), int(values["console"])


# ------------------------------------------------------------------ the guard's rules (pure)

def test_the_guard_picks_the_flag_the_caller_meant():
    assert guard.rewrite_flags(0) == guard.CREATE_NO_WINDOW                    # nothing asked: windowless
    assert guard.rewrite_flags(guard.CREATE_NO_WINDOW) == guard.CREATE_NO_WINDOW
    # A console-less process makes every console program it later starts pop a visible window,
    # so DETACHED_PROCESS becomes a hidden console instead.
    assert guard.rewrite_flags(guard.DETACHED_PROCESS) == guard.CREATE_NO_WINDOW
    assert guard.rewrite_flags(guard.DETACHED_PROCESS | 0x00000200) == guard.CREATE_NO_WINDOW | 0x00000200
    # A console for a person is not something to take away, but only inside visible().
    assert guard.rewrite_flags(guard.CREATE_NEW_CONSOLE) == guard.CREATE_NO_WINDOW
    with guard.visible():
        assert guard.is_visible() is True
        assert guard.rewrite_flags(guard.CREATE_NEW_CONSOLE) == guard.CREATE_NEW_CONSOLE
        assert guard.rewrite_flags(guard.DETACHED_PROCESS) == guard.CREATE_NEW_CONSOLE
        with guard.visible():
            assert guard.rewrite_flags(guard.CREATE_NEW_CONSOLE) == guard.CREATE_NEW_CONSOLE
    assert guard.is_visible() is False


def test_installing_twice_is_one_wrapper_and_uninstall_puts_popen_back():
    guard.uninstall()                       # the fixture already installed it; start from the real Popen
    original = subprocess.Popen
    assert guard.install() is WINDOWS
    wrapped = subprocess.Popen
    # It must still be a class: asyncio's Windows transport does `class Popen(subprocess.Popen)`,
    # and a plain function in that slot makes the very next `import asyncio` raise a TypeError.
    assert isinstance(wrapped, type), "the guard installed a function where a class has to be"
    guard.install()
    assert subprocess.Popen is wrapped, "install() must be idempotent, not layered"
    guard.uninstall()
    assert subprocess.Popen is original
    assert guard.install() is WINDOWS       # leave the guard in place for the rest of the session


@windows_only
def test_the_guarded_popen_is_still_a_class_and_still_a_popen():
    """A drop-in replacement, not just a callable: `subprocess.Popen` is subscripted in annotations
    that are evaluated while a class body runs - `popen_obj: subprocess.Popen[bytes]` in
    `mcp/os/win32/utilities.py`, which does not use `from __future__ import annotations`. Making the
    guard a plain wrapper *function* therefore made the whole `mcp` package unimportable, and
    `bot/mcp_server.py` with it, the moment the guard went session-wide. Found by the test suite,
    which is the only reason it is written down."""
    real = guard._original_popen
    assert real is not None and isinstance(real, type), "the saved original must be the real class"
    assert isinstance(subprocess.Popen, type) and issubclass(subprocess.Popen, real)
    assert subprocess.Popen[bytes] is not None, "Popen must stay subscriptable"
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert isinstance(proc, subprocess.Popen) and proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_the_guard_can_be_installed_before_a_process_ever_imports_asyncio():
    """The failure an entry point would actually hit: it calls `guard.install()` first thing, and
    only then does something import asyncio - whose Windows transport is built by writing
    `class Popen(subprocess.Popen)` against whatever the guard left there."""
    code = ("import subprocess\n"
            "from bot.sandbox_ns import guard\n"
            "assert guard.install() is True\n"
            "import asyncio\n"
            "assert isinstance(subprocess.Popen, type), 'the guard left a function behind'\n"
            "asyncio.new_event_loop()\n"
            "print('ok')\n")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=120, env=env)
    assert proc.stdout.strip().endswith("ok"), proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stderr


# ------------------------------------------------------------------ no window on the desktop

@windows_only
def test_a_child_and_the_grandchild_it_starts_are_both_windowless(probe, tmp_path):
    child, grandchild = run_probe(probe, tmp_path)
    hwnd, visible, in_console = parse(child)
    assert visible == 0, f"the child ABP started has a visible console window: {child}"
    assert in_console > 0, "no console at all: every console program it starts later would open its own window"
    assert grandchild, "the probe did not get as far as starting a grandchild"
    g_hwnd, g_visible, g_console = parse(grandchild)
    assert g_visible == 0, f"the grandchild inherited something visible: {grandchild}"
    assert g_console > 0, "the grandchild must inherit the hidden console, not be left without one"
    grandchild_pid = int((tmp_path / "grandchild.txt.pid").read_text().strip())
    assert grandchild_pid > 0 and _alive(grandchild_pid) is False, "the grandchild should have finished"


@windows_only
def test_a_detached_process_ends_up_windowless_but_still_with_a_console(tmp_path):
    """The whole point of the guard: code that asks for DETACHED_PROCESS (which is what "run this
    quietly" usually gets written as) gets a hidden console instead.

    Measured on this machine before the guard existed: a DETACHED_PROCESS child of this test suite
    produced a *visible* ConsoleWindowClass window (IsWindowVisible == 1, reproduced through a raw
    Win32 CreateProcess as well), which is the popup this package exists to stop. It is not
    re-created here on purpose - a test suite must never put a window on this person's desktop."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    out = tmp_path / "detached.txt"
    proc = subprocess.Popen([sys.executable, str(probe), str(out), "no-grandchild"], creationflags=guard.DETACHED_PROCESS,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.wait(timeout=60) == 0
    hwnd, visible, in_console = parse(out.read_text(encoding="utf-8").strip())
    assert visible == 0, f"a DETACHED_PROCESS spawn still put a window on the desktop: hwnd={hwnd}"
    assert in_console > 0, "the process must end up with the hidden console, not with none"
    assert any(e["kind"] == "guard_converted" for e in registry_mod.registry.events(limit=200)), \
        "the guard must say what it rewrote"


@windows_only
def test_an_asyncio_subprocess_is_windowless_too(tmp_path):
    """asyncio's Windows transport goes through subprocess.Popen, so one wrapper covers it -
    checked here against a real asyncio child rather than assumed."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    out = tmp_path / "async.txt"

    async def go() -> int:
        with cell_for("tool", name="async-probe") as cell:
            proc = await async_spawn([sys.executable, str(probe), str(out), "no-grandchild"], cell=cell, name="async-probe",
                                     owner="test", stdout=asyncio.subprocess.PIPE,
                                     stderr=asyncio.subprocess.STDOUT)
            await proc.stdout.read()
            return await proc.wait()

    assert asyncio.run(go()) == 0
    hwnd, visible, in_console = parse(out.read_text(encoding="utf-8").strip())
    assert visible == 0, f"an asyncio subprocess got a visible console window: hwnd={hwnd} visible={visible}"
    assert in_console > 0


@windows_only
def test_a_visible_console_is_the_one_thing_the_guard_leaves_alone():
    """Checked on the flag, not by really opening a window: this test suite must never put a
    console on this person's desktop - that is the whole complaint (the pure rule test above
    covers what visible() does with each flag, and bot/modules/harness.py's open_tui is the only
    caller that uses it)."""
    assert guard.is_installed()
    with guard.visible():
        assert guard.rewrite_flags(guard.CREATE_NEW_CONSOLE) == guard.CREATE_NEW_CONSOLE


# ------------------------------------------------------------------ containment

def _child_with_a_sleeping_grandchild(cell, tmp_path) -> tuple:
    """A child that starts a grandchild which would outlive it, and records the grandchild's pid."""
    (tmp_path / "grand.txt").write_text("")
    code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        f"open({str(tmp_path / 'grand.txt')!r}, 'w').write(str(p.pid));"
        "time.sleep(120)"
    )
    proc = spawn([sys.executable, "-c", code], cell=cell, name="tree", owner="test",
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (tmp_path / "grand.txt").read_text().strip():
        time.sleep(0.2)
    return proc, int((tmp_path / "grand.txt").read_text().strip())


def test_a_cells_kill_kills_the_grandchild_too(tmp_path):
    with cell_for("tool", name="tree") as cell:
        proc, grandchild = _child_with_a_sleeping_grandchild(cell, tmp_path)
        assert grandchild > 0 and _alive(grandchild)
        cell.kill("the test asked for it")
        left = wait_gone([proc.pid, grandchild])
    assert left == [], f"the tree outlived the cell: {left}"


# ------------------------------------------------- what a record has to cover, not just who started it

def _wait_record(reg, pid: int, timeout: float = 30.0):
    """The registry's record for `pid`, once it has one. A process that is only a descendant of the
    spawn is noticed on a sampling pass rather than at the spawn, so this drives those passes."""
    deadline = time.monotonic() + timeout
    while True:
        row = reg.record_for(pid)
        if row is not None:
            return row
        assert time.monotonic() < deadline, f"pid {pid} was started but never recorded"
        reg.sample(reflexes=False)
        time.sleep(0.05)


def _wait_descendant(reg, parent_pid: int, name: str = "", timeout: float = 30.0):
    """A recorded process directly below `parent_pid` - the one whose executable is `name`, if a
    name is given - once there is one. `wait` because the child of a launcher is not there yet in
    the microsecond after CreateProcess returns."""
    deadline = time.monotonic() + timeout
    while True:
        for row in reg.records(alive_only=True):
            if row.parent_pid == parent_pid and (not name or row.name.lower() == name.lower()):
                return row
        assert time.monotonic() < deadline, f"no {name or 'process'} under pid {parent_pid} was ever recorded"
        reg.sample(reflexes=False)
        time.sleep(0.05)


def test_the_processes_a_spawned_process_starts_are_recorded_under_it(tmp_path):
    """A record is a tree, not a process: `spawn()` hands back the pid `Popen` created, and that is
    not always the process that does the work. Here the child starts a grandchild, and everything
    below the spawn is recorded under it - same owner, same cell, same policy, each pointing at the
    record that started it - because a `live.json` listing only the spawn is a list of what ABP asked
    for rather than of what is running."""
    reg = registry_mod.registry
    with cell_for("tool", name="recorded-tree") as cell:
        proc, grandchild = _child_with_a_sleeping_grandchild(cell, tmp_path)
        assert reg.record_for(proc.pid).spawned(), "the spawn itself is the one record with no parent"
        for pid in {proc.pid, grandchild}:
            _wait_record(reg, pid)
        tree = {r.pid: r for r in reg.records(alive_only=True) if r.cell == cell.id}
        for pid, row in tree.items():
            assert (row.owner, row.cell, row.policy) == ("test", cell.id, "tool"), row
            assert row.create_time > 0, "without a create time the reaper cannot prove a pid is the same process"
            if pid != proc.pid:
                assert row.parent_pid in tree, f"pid {pid} is recorded, but nothing in this tree started it: {row}"
        assert any(r.parent_pid == proc.pid for r in tree.values()), f"the spawn started nothing: {tree}"
        assert tree[grandchild].summary()["role"] == "descendant"
        on_disk = {p["pid"] for p in read_state()["runs"][reg.run_id]["processes"]}
        assert set(tree) <= on_disk, "the tree has to be in live.json, or the next run cannot reap it"
        cell.kill("the test asked for it")
    assert wait_gone(list(tree)) == [], "the cell took the launcher and everything below it"


@windows_only
@pytest.mark.skipif(sys.executable == getattr(sys, "_base_executable", ""),
                    reason="this interpreter is not behind a venv launcher, so a spawn is only one process")
def test_a_venvs_launcher_and_the_interpreter_behind_it_are_both_recorded(tmp_path):
    """The case that made this a bug: ABP runs from a venv, and on Windows
    `.venv\\Scripts\\python.exe` is a launcher that starts the base interpreter as its child and
    waits for it (measured on this machine). So `spawn([sys.executable, ...])` is two processes, and
    recording only the pid `Popen` returned recorded the wrapper rather than the interpreter that
    runs the work."""
    reg = registry_mod.registry
    with cell_for("tool", name="launcher") as cell:
        proc = spawn([sys.executable, "-c", "import time; time.sleep(120)"], cell=cell, owner="test",
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = _wait_descendant(reg, proc.pid, "python.exe")
        assert base.parent_pid == proc.pid and not base.spawned(), base
        assert (base.owner, base.cell, base.policy) == ("test", cell.id, "tool"), base
        assert Path(psutil.Process(base.pid).exe()) == Path(sys.base_prefix) / "python.exe", base
        assert reg.record_for(base.pid) is base, "record_for() answers for a descendant as well"
        assert base.pid in {p["pid"] for p in reg.status()["processes"]}, "and so does the status page"
        cell.kill("the test asked for it")
    assert wait_gone([proc.pid, base.pid]) == [], "the cell took the launcher and the interpreter"


def test_closing_abps_cells_kills_the_non_persistent_ones_only():
    """What ABP does on its way out (bot/main.py's shutdown): every cell it still holds is closed,
    and only the daemons are let go. Built without `with` on purpose - a context manager would
    have closed them before close_cells() ever saw them.

    The registry is one per *process* and this worker runs every test in it, so the count
    close_cells() reports is not this test's to make: a command cell left over from an earlier
    test (sandbox.py's per-command cell is reclaimed by the sampler, up to one sample interval
    after the command exited) is closed by that same shutdown too. So this asserts about its own
    two cells - both closed, both gone from the registry, the non-persistent one's process dead
    and the daemon's alive - and only asks that the count covers them."""
    reg = registry_mod.registry
    quick = Cell(policy_mod.preset("tool"), name="short-lived", owner="test")
    daemon = Cell(policy_mod.preset("daemon"), name="long-lived", owner="test")
    quick_proc = spawn([sys.executable, "-c", SLEEPER % 60], cell=quick, owner="test",
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    daemon_proc = spawn([sys.executable, "-c", SLEEPER % 60], cell=daemon, owner="test",
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert reg.close_cells(reason="the test is shutting ABP down") >= 2, (
            "both of this test's cells are registered, so both must be among the ones closed")
        left = {c.id for c in reg.cells()}
        assert not {quick.id, daemon.id} & left, "a cell ABP has closed must not still be registered"
        assert wait_gone([quick_proc.pid]) == [], "a non-persistent cell must die with the process"
        time.sleep(0.5)
        assert _alive(daemon_proc.pid), "a daemon is supposed to outlive ABP"
        assert daemon.closed and quick.closed
    finally:
        daemon_proc.kill()
        daemon_proc.wait(10)


def test_a_memory_capped_cell_stops_a_process_that_allocates_past_the_cap(tmp_path):
    marker = tmp_path / "ran.txt"
    code = EATER.format(out=str(marker), mb=768)
    cell = Cell(Policy(name="mem", memory_mb=128), name="greedy", owner="test")
    try:
        proc = spawn([sys.executable, "-c", code], cell=cell, owner="test",
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # While it is running: what the OS says the limits actually are (Windows reads them back
        # from the job object itself, not from this module's memory of what it asked for).
        limits = cell.status()["limits_applied"]
        assert limits.get("memory_mb", 128) == 128, limits
        assert proc.wait(timeout=120) != 0
        assert not marker.exists(), "the process allocated past its cap and still finished its work"
        assert wait_gone([proc.pid]) == []
    finally:
        cell.close()


def test_the_memory_reflex_kills_the_cell_and_says_so():
    """The backstop for a cap the OS did not enforce: the measurement is put into the registry
    by hand (a real growing process is covered by the rule test below and by the job's own limit
    above - here the point is that a decision reaches the cell and really stops the process)."""
    cell = Cell(Policy(name="mem", memory_mb=128), name="greedy", owner="test")
    try:
        proc = spawn([sys.executable, "-c", SLEEPER % 60], cell=cell, owner="test",
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        registry_mod.registry._samples[cell.id] = {"pid": 1, "rss_mb": 900.0, "cpu_percent": 0.0,
                                                   "at": time.time()}
        done = reflexes.apply(registry_mod.registry, sample=False)
        assert [d.kind for d in done] == ["memory"] and done[0].cell == cell.id
        assert cell.closed and wait_gone([proc.pid]) == [], "the reflex said it stopped the cell, and it did"
        events = [e for e in registry_mod.registry.events(limit=50) if e["kind"] == "limit_hit"]
        assert events and "128 MB cap" in events[0]["detail"]
    finally:
        cell.close()


@windows_only
def test_the_cpu_rate_affinity_and_priority_are_set_and_read_back():
    from bot.agent_runtime import win_job

    cpus = policy_mod.available_cpus()
    wanted = tuple(sorted(cpus)[:max(1, len(cpus) // 2)])
    cell = Cell(Policy(name="capped", cpu_rate_percent=25.0, affinity=wanted, priority="below_normal",
                       max_processes=8), name="capped", owner="test")
    try:
        assert cell.job_handle(), "no job object was created, so nothing is contained"
        limits = win_job.query(cell.job_handle())
        assert limits["cpu_rate_percent"] == 25.0, limits
        assert limits["affinity"] == list(wanted), limits
        assert limits["priority"] == "below_normal" and limits["active_process_limit"] == 8, limits
        proc = spawn([sys.executable, "-c", SLEEPER % 30], cell=cell, owner="test",
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p = psutil.Process(proc.pid)
        assert sorted(p.cpu_affinity()) == list(wanted), "the process itself did not get the affinity"
        assert p.nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS
    finally:
        cell.close()


def test_a_policies_timeout_stops_the_whole_tree(tmp_path):
    """A build preset's two-hour limit is enforced by ABP itself when it fires, and it takes the
    compiler with the shell - which is the whole point of giving the timeout to the cell."""
    cell = Cell(Policy(name="slow", timeout_s=1), name="slow", owner="test")
    try:
        proc, grandchild = _child_with_a_sleeping_grandchild(cell, tmp_path)
        assert wait_gone([proc.pid, grandchild], timeout=20) == [], "the timeout did not stop the tree"
        limits = [e for e in registry_mod.registry.events(limit=50) if e["kind"] == "limit_hit"]
        assert any("passed its 1s limit" in e["detail"] for e in limits), limits
        assert cell.closed
    finally:
        cell.close()


@windows_only
def test_a_job_with_more_than_64_processors_in_its_affinity_says_it_skipped_it():
    cell = Cell(Policy(name="wide", affinity=tuple(range(0, 200, 4))), name="wide", owner="test")
    try:
        assert any("affinity skipped" in note for note in cell.notes), cell.notes
    finally:
        cell.close()


def test_run_captures_output_and_its_timeout_takes_the_cell_with_it(tmp_path):
    """`run()` is spawn() plus communicate(): the many callers that only want a result still get
    containment, an exit record, and a timeout that stops the whole cell instead of one pid."""
    from bot.sandbox_ns import spawn as spawn_mod

    done = spawn_mod.run([sys.executable, "-c", "print('hello-from-run')"], timeout=60)
    assert done.returncode == 0 and "hello-from-run" in done.stdout
    with cell_for("tool", name="run-timeout") as cell:
        with pytest.raises(subprocess.TimeoutExpired):
            spawn_mod.run([sys.executable, "-c", SLEEPER % 60], timeout=1, cell=cell)
        assert cell.closed, "a timed-out run must not leave its cell (and its tree) alive"


# ------------------------------------------------------------------ the registry's memory

def test_the_registry_records_a_spawn_and_its_exit_and_the_state_file_follows(tmp_path):
    reg = registry_mod.registry
    with cell_for("tool", name="recorded") as cell:
        proc = spawn([sys.executable, "-c", "print('hi')"], cell=cell, owner="test", cwd=tmp_path,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        row = reg.record_for(proc.pid)
        assert row is not None
        assert row.owner == "test" and row.cell == cell.id and row.policy == "tool"
        assert row.cwd == str(tmp_path) and row.argv[0] == sys.executable
        assert row.create_time > 0, "without a create time the reaper can never prove a pid is the same process"
        pids = [p["pid"] for p in read_state()["runs"][reg.run_id]["processes"]]
        assert proc.pid in pids, "a live process must be in live.json, or the next run cannot reap it"
        assert proc.wait(timeout=30) == 0
    deadline = time.monotonic() + 30
    mine = []
    while time.monotonic() < deadline:
        mine = [e["kind"] for e in reversed(reg.events(limit=500)) if e["pid"] == proc.pid]
        if "exit" in mine:
            break
        time.sleep(0.2)
    assert "spawn" in mine and "exit" in mine, mine
    assert reg.record_for(proc.pid).exit_code == 0, "the exit code is what a route or the reaper reports"
    left = read_state()["runs"][reg.run_id]["processes"]
    assert all(p["pid"] != proc.pid for p in left), "a finished process must not be left in live.json"


def test_the_registry_status_is_something_a_route_could_serve():
    status = registry_mod.registry.status()
    assert set(status) >= {"run_id", "state_file", "guard", "processes", "cells", "events"}
    assert status["guard"]["installed"] in (True, False)


def test_secret_looking_arguments_are_masked_before_they_are_written_down():
    from bot.agent_runtime import secrets_guard

    secrets_guard.register("ABP_TEST_TOKEN", "unused-not-a-real-token")
    try:
        masked = registry_mod.mask_argv(
            ["tool", "--api-key", "sk-live-1234", "--token=abc12345", "PASSWORD=hunter22", "--verbose",
             "unused-not-a-real-token"])
        assert masked[0] == "tool" and masked[1] == "--api-key" and masked[2] == "[secret]"
        assert masked[3] == "--token=[secret]"
        assert masked[4] == "PASSWORD=[secret]"
        assert masked[5] == "--verbose", "an ordinary flag must survive, or the record is unreadable"
        assert masked[6] == "[secret]", "a value that is a known secret is masked wherever it appears"
    finally:
        secrets_guard.unregister("ABP_TEST_TOKEN")


def test_a_second_runs_entries_survive_this_runs_write(state_file):
    """Two ABP processes share one state file: a worker must not erase the server's bookkeeping."""
    reg = registry_mod.registry
    other = Registry(path=state_file, run_id="some-other-run")
    other.record(pid=sleeper().pid, argv=["other", "run"], owner="test")
    reg.write_state()
    runs = read_state()["runs"]
    assert "some-other-run" in runs


# ------------------------------------------------------------------ the reaper

def _write_state(path: Path, run: str, processes: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated": time.time(),
                                "runs": {run: {"pid": 1, "updated": time.time(), "processes": processes}}}),
                    encoding="utf-8")


def test_the_reaper_kills_what_a_previous_run_left_running(state_file):
    victim = sleeper()
    try:
        _write_state(state_file, "previous-run", [
            {"pid": victim.pid, "create_time": psutil.Process(victim.pid).create_time(),
             "argv": ["llama-server", "-m", "big.gguf"], "cwd": "", "owner": "localai.engine", "cell": "engine-1",
             "policy": "engine", "persistent": False, "started": time.time(), "exited": None, "exit_code": None}])
        report = reaper.reap(state_file)
        assert [r["pid"] for r in report["killed"]] == [victim.pid]
        assert wait_gone([victim.pid]) == [], "the leftover was recorded as killed but is still running"
        assert any(e["kind"] == "reap" and e["pid"] == victim.pid for e in registry_mod.registry.events(limit=50))
    finally:
        victim.kill()
        victim.wait(10)


def test_the_reaper_never_kills_a_pid_that_belongs_to_a_different_process(state_file):
    """Windows reuses pids. A record whose create time does not match is somebody else's process -
    this one is literally the test runner."""
    me = os.getpid()
    keeper = sleeper()
    try:
        _write_state(state_file, "previous-run", [
            {"pid": me, "create_time": psutil.Process(me).create_time() - 3600,
             "argv": ["someone-elses-program"], "owner": "test", "policy": "tool", "persistent": False}])
        report = reaper.reap(state_file)
        assert [r["pid"] for r in report["reused"]] == [me] and report["killed"] == []
        assert _alive(me), "the reaper killed the process it was running in"
        assert _alive(keeper.pid)
    finally:
        keeper.kill()
        keeper.wait(10)


def test_the_reaper_leaves_daemons_and_records_without_a_create_time_alone(state_file):
    keeper = sleeper()
    try:
        _write_state(state_file, "previous-run", [
            {"pid": keeper.pid, "create_time": psutil.Process(keeper.pid).create_time(), "argv": ["transferd"],
             "owner": "transferdaemon", "policy": "daemon", "persistent": True},
            {"pid": 999999, "create_time": 0.0, "argv": ["ancient-record"], "owner": "test", "policy": "tool",
             "persistent": False}])
        report = reaper.reap(state_file)
        assert [r["policy"] for r in report["kept"]] == ["daemon"]
        assert [r["argv"][0] for r in report["gone"]] == ["ancient-record"]
        assert report["killed"] == []
        assert _alive(keeper.pid)
    finally:
        keeper.kill()
        keeper.wait(10)


def test_the_reaper_leaves_another_running_apps_processes_alone(state_file):
    """Two ABPs on one machine share one live.json: a run whose owning pid is still alive is a
    running server, not a leftover, and none of its processes are touched."""
    other_run = sleeper()
    try:
        _write_state(state_file, "live-server", [
            {"pid": other_run.pid, "create_time": psutil.Process(other_run.pid).create_time(),
             "argv": ["python", "-m", "bot.main"], "owner": "bot.main", "policy": "tool", "persistent": False}])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["runs"]["live-server"]["pid"] = os.getpid()          # this test process "is" that ABP
        state_file.write_text(json.dumps(state), encoding="utf-8")
        report = reaper.reap(state_file)
        assert report == {"killed": [], "kept": [], "gone": [], "reused": []}, report
        assert _alive(other_run.pid)
    finally:
        other_run.kill()
        other_run.wait(10)


# ------------------------------------------------------------------ policy and reflexes (pure)

def test_the_presets_say_what_they_are():
    assert set(policy_mod.PRESETS) == {"tool", "agent", "build", "engine", "daemon", "worker"}
    for name, p in policy_mod.PRESETS.items():
        assert p.name == name
        assert p.priority == "below_normal", f"{name} would compete with the person at the keyboard"
        assert p.affinity is None, "the default processor set is resolved by with_defaults(), not baked in"
    assert policy_mod.PRESETS["daemon"].persistent is True
    assert all(not p.persistent for n, p in policy_mod.PRESETS.items() if n != "daemon")
    with pytest.raises(KeyError, match="unknown sandbox preset"):
        policy_mod.preset("server")


def test_the_default_processor_set_is_everything_except_what_must_be_avoided(monkeypatch):
    monkeypatch.setattr(policy_mod, "available_cpus", lambda: (0, 1, 2, 3, 4, 5))
    monkeypatch.setattr(policy_mod, "avoid_cpus", lambda: (2, 3))
    monkeypatch.setitem(policy_mod._affinity_default, "policy", None)
    assert policy_mod.with_defaults(policy_mod.preset("tool")).affinity == (0, 1, 4, 5)
    # An empty or impossible set means "all of them", never "none of them": a cell that cannot run
    # at all is not what avoiding two cores meant.
    monkeypatch.setattr(policy_mod, "avoid_cpus", lambda: (0, 1, 2, 3, 4, 5))
    monkeypatch.setitem(policy_mod._affinity_default, "policy", None)
    assert policy_mod.with_defaults(policy_mod.preset("tool")).affinity is None, (
        "nothing left to use means no restriction at all, never an empty one: a process that cannot run is not"
        " what avoiding five of six processors meant")


def test_a_configured_preset_override_changes_only_that_field():
    p = policy_mod.apply_overrides(policy_mod.preset("build"), {"memory_mb": 2048, "nonsense": 1})
    assert p.memory_mb == 2048 and p.cpu_rate_percent == policy_mod.PRESETS["build"].cpu_rate_percent
    assert policy_mod.apply_overrides(policy_mod.preset("build"), {"nonsense": 1}) == policy_mod.PRESETS["build"]


def test_the_avoided_processors_come_from_config_and_from_what_the_machine_reports(monkeypatch):
    monkeypatch.setitem(policy_mod._detected, "stamp", time.time())
    monkeypatch.setitem(policy_mod._detected, "cpus", (7,))
    monkeypatch.setattr(policy_mod, "_configured_avoid", lambda: (3, 7))
    assert policy_mod.avoid_cpus() == (3, 7)
    monkeypatch.setattr(policy_mod, "_configured_avoid", lambda: ())
    assert policy_mod.avoid_cpus() == (7,), "a machine with no history and no config runs everywhere"


def test_the_tool_policy_scrubs_secrets_and_delegates_the_offline_launcher(monkeypatch):
    from bot.agent_runtime import sandbox

    environ = {"PATH": "x", "OPENAI_API_KEY": "sk-something-long-enough", "HOME": "/home/me"}
    env = policy_mod.PRESETS["tool"].environment(environ)
    assert "OPENAI_API_KEY" not in env and env["PATH"] == "x" and env["HOME"] == "/home/me"
    assert policy_mod.PRESETS["agent"].environment(environ) == environ, "a CLI agent keeps what it needs"
    assert policy_mod.PRESETS["tool"].offline() is False
    assert dataclasses.replace(policy_mod.PRESETS["tool"], network="none").offline() is True
    monkeypatch.setattr(sandbox, "network", lambda cfg=None: "none")
    assert policy_mod.PRESETS["tool"].offline() is True, "it defers to the sandbox setting when it has none of its own"


def test_the_reflexes_decide_from_a_snapshot_alone():
    class FakeCell:
        def __init__(self, policy):
            self.policy = policy
            self.id = "c1"
            self.name = "the-build"

    build = FakeCell(policy_mod.preset("build"))
    tool = FakeCell(policy_mod.preset("tool"))
    assert reflexes.memory_decision(build, {"rss_mb": 100, "pid": 1}) is None, "under the cap: nothing happens"
    over = reflexes.memory_decision(build, {"rss_mb": 9000, "pid": 1})
    assert over is not None and over.kind == "memory" and "6144 MB cap" in over.detail
    assert reflexes.memory_decision(FakeCell(policy_mod.PRESETS["engine"]), {"rss_mb": 90000, "pid": 1}) is None, \
        "no cap, nothing to enforce"
    assert reflexes.memory_decision(build, {"rss_mb": 9000, "pid": 0}) is None, "an empty cell cannot be over"

    assert reflexes.cpu_decision(20.0, 10, [build, tool]) == []
    assert reflexes.cpu_decision(99.0, 1, [build, tool]) == [], "one hot sample is a burst, not a trend"
    hot = reflexes.cpu_decision(99.0, 4, [build, tool])
    assert [d.cell for d in hot] == [build.id], "a tool call is what the person is waiting for; never demoted"
    assert "demoted to idle" in hot[0].detail

    daemon = FakeCell(policy_mod.PRESETS["daemon"])
    engaged = reflexes.estop_decision(True, [build, daemon])
    assert [d.cell for d in engaged] == [build.id], "the estop stops work, not somebody's running service"
    assert reflexes.estop_decision(False, [build]) == []


def test_the_cpu_reflex_only_ever_lowers_a_priority():
    cell = Cell(policy_mod.PRESETS["build"], name="b", owner="test")
    try:
        assert reflexes._set_idle(cell) is True
        assert cell.policy.priority == "idle"
        assert reflexes._set_idle(cell) is True, "demoting twice is not an error"
    finally:
        cell.close()


def test_the_emergency_stop_stops_the_work_and_leaves_the_services(temp_db):
    """ABP's own estop sentinel (the one every agent entry point already checks) reaching the
    process layer: work dies, a running daemon does not."""
    from bot.agent_runtime import estop

    work = Cell(policy_mod.preset("tool"), name="work", owner="test")
    service = Cell(policy_mod.preset("daemon"), name="service", owner="test")
    work_proc = spawn([sys.executable, "-c", SLEEPER % 60], cell=work, owner="test",
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    service_proc = spawn([sys.executable, "-c", SLEEPER % 60], cell=service, owner="test",
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert reflexes.apply(registry_mod.registry, sample=False) == [], "nothing to do while estop is off"
        estop.engage("the test pressed the button")
        try:
            done = reflexes.apply(registry_mod.registry, sample=False)
            assert [d.kind for d in done] == ["estop"], done
            assert wait_gone([work_proc.pid]) == [], "the estop stopped the cell but not the process"
            assert _alive(service_proc.pid), "a daemon is somebody's running service, not a turn in progress"
        finally:
            estop.disengage()
    finally:
        service_proc.kill()
        service_proc.wait(10)
        work.close()
        service.close()


# ------------------------------------------------------------------ the sandbox's own backends

def test_the_sandbox_local_backend_still_runs_and_still_contains_its_tree(tmp_path):
    """The migration's own regression test: bot/agent_runtime/sandbox.py's local backend now goes
    through a cell, and its behaviour (run, capture output, kill the tree on stop) has not changed."""
    from bot.agent_runtime import sandbox, toolspec

    token = toolspec.session_var.set("sandbox-ns-test")
    original = sandbox._config
    sandbox._config = lambda: {"backend": "local"}
    ws = tmp_path / "ws"
    ws.mkdir()

    async def go() -> tuple:
        proc = await sandbox.start("echo hello-from-local", ws, ws)
        out = (await proc.stdout.read()).decode(errors="replace")
        await proc.wait()
        sleeper_proc = await sandbox.start("ping -n 30 127.0.0.1 > nul", ws, ws)
        sandbox.kill(sleeper_proc)                       # what shell.kill_tree() does on a timeout
        deadline = time.monotonic() + 20
        while sleeper_proc.returncode is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        return out, sleeper_proc.returncode

    try:
        out, returncode = asyncio.run(go())
    finally:
        sandbox._config = original
        toolspec.session_var.reset(token)
    assert "hello-from-local" in out
    assert returncode is not None, "kill() did not stop the command"
    assert sandbox.kill.__doc__


# ------------------------------------------------------------------ the surface

def test_the_diagnostics_processes_panel_is_in_both_uis_and_wired():
    """Diagnostics shows what ABP is running in the web dashboard *and* in the desktop app's own
    page: both serve it from the same /api/sandbox/status route, and neither may be left with a
    table nobody fills in - an id in the HTML with no renderer behind it is a panel that says
    "Loading." forever."""
    root = Path(__file__).resolve().parent.parent
    ids = ('id="diag-cells-tbody"', 'id="diag-processes-tbody"', 'id="diag-events-tbody"', 'id="diag-processes-note"')
    for rel in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (root / rel).read_text(encoding="utf-8")
        for needle in ids:
            assert needle in text, f"{rel} is missing {needle}"
        assert 'data-tab="cells"' in text and 'data-tab="events"' in text, rel
        assert ".tab-panel" in text, f"{rel} has the panels but not the css that hides the inactive ones"
    for rel in ("bot/dashboard/static/dashboard.js", "desktop-app/ui/main.js"):
        text = (root / rel).read_text(encoding="utf-8")
        for needle in ("renderSandboxCells", "renderSandboxProcesses", "renderSandboxEvents", "/api/sandbox/status"):
            assert needle in text, f"{rel} never calls {needle}"
        assert "data-kill-cell" in text, f"{rel} cannot stop a cell from the page"
