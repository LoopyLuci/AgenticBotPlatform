"""A blank console window must never appear on this person's desktop - checked, not assumed.

`bot/sandbox_ns/guard.py` is the fix: every process ABP starts gets `CREATE_NO_WINDOW`. This is
the regression test for the fix, at the only level that can actually see the complaint - Windows
itself. A daemon thread polls `EnumWindows` for the window classes a console program puts up (the
same three `X:/Dev/swarm/popup_watch.ps1` watches), remembers which handles were already open when
the session started - the person's own windows are none of our business - and fails the test that
was running when a new one appeared, with the process chain that opened it.

The chain matters because the window's owner is usually gone by the time anyone notices the flash:
`popup_watch.ps1` had to keep a 1.5 s process cache for exactly that reason. Polling at 200 ms and
naming the chain on the spot catches nearly everything; a window both created and closed inside
one poll interval is invisible to any poller, which is why the fix is the guard and not this.

A window is only blamed on a test when its process chain reaches *this* worker. Several ABP test
runs can share one desktop, and anything else on the machine may open a window; a popup somebody
else caused is not this suite's regression.

Off Windows there is no console window to pop up and the plugin does nothing;
`ABP_ALLOW_WINDOWS=1` turns it off for a person deliberately debugging one.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

import pytest

# ConsoleWindowClass is what cmd.exe/python.exe/qemu.exe put up; the other two are what a Windows
# Terminal tab and a ConPTY-hosted shell put up. Same list as popup_watch.ps1.
CONSOLE_CLASSES = frozenset({"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow"})
POLL_SECONDS = 0.2
MAX_CHAIN = 8

if os.name == "nt":                                            # ctypes.WinDLL does not exist elsewhere
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def visible_consoles() -> list[tuple[int, int, str, str]]:
    """Every visible console-shaped window on this desktop right now, as (hwnd, pid, class, title).

    The real `EnumWindows`, not a stand-in: the whole point is to see what Windows is actually
    showing, so there is nothing here worth mocking.
    """
    if os.name != "nt":
        return []
    found: list[tuple[int, int, str, str]] = []

    @_ENUM_PROC                                        # noqa: N802 - a Win32 callback, not a class
    def visit(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        if buf.value in CONSOLE_CLASSES:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            text = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, text, 512)
            found.append((hwnd, pid.value, buf.value, text.value))
        return True

    _user32.EnumWindows(visit, 0)
    return found


def forget_dead_handles(handles: set[int]) -> None:
    """Drop remembered handles whose window is gone.

    Windows reuses window handles, so a remembered handle that no longer names a window would hide
    the *next* popup that lands on it. Normally this set is empty - that is the point - so the cost
    is one `IsWindow` call per handle per scan.
    """
    if os.name != "nt":
        handles.clear()
        return
    for hwnd in [h for h in handles if not _user32.IsWindow(h)]:
        handles.discard(hwnd)


def _mask(argv: list[str]) -> str:
    """The command line as it may be printed: ABP's own secret-argument masking, same as live.json."""
    line = " ".join(argv).split()
    try:
        from bot.sandbox_ns.registry import mask_argv

        line = mask_argv(line)
    except Exception:  # noqa: BLE001 - a diagnostics line must never be the reason a run dies
        pass
    text = " ".join(line)
    return text if len(text) <= 200 else text[:200] + " ..."


def process_chain(pid: int, me: int) -> tuple[list[str], bool]:
    """`pid` and its parents, nearest first, plus whether the chain reaches this process.

    An ancestor that exits mid-walk shows up as "(already gone)": Windows reuses pids, so naming a
    stale one would be worse than saying nothing.
    """
    import psutil

    rows: list[str] = []
    seen: set[int] = set()
    cur = pid
    for _ in range(MAX_CHAIN):
        if cur <= 0 or cur in seen:
            break
        seen.add(cur)
        if cur == me:
            rows.append(f"{cur} this pytest worker")
            return rows, True
        try:
            proc = psutil.Process(cur)
            parent = proc.ppid()
            rows.append(f"{cur} {proc.name()} [{_mask(proc.cmdline())}]")
        except psutil.NoSuchProcess:
            rows.append(f"{cur} (already gone)")
            break
        except (psutil.AccessDenied, psutil.ZombieProcess):
            rows.append(f"{cur} (no permission to look)")
            break
        cur = parent
    return rows, me in seen


def _explain(lines: list[str]) -> str:
    return (
        "a new visible console window appeared while this test was running "
        "(ABP must never put one on this desktop - see docs/sandbox-nervous-system.md):\n"
        + "\n".join("  " + line for line in lines)
    )


class NoWindows:
    """Watches for new visible console windows and blames the test that was running."""

    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self._poll = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._baseline: set[int] = set()          # handles already open: the person's own windows
        self._open: set[int] = set()              # every handle ever seen, so one window is one report
        self._current: Optional[str] = None       # the test running right now
        self._popups: dict[str, list[str]] = {}   # nodeid -> the report lines for it
        self._unattributed: list[str] = []
        self.scans = 0

    def should_watch(self, config: pytest.Config) -> bool:
        """Whether this process is the one that should be watching.

        An xdist *controller* runs no tests, so anything it saw would belong to a worker and be
        blamed on nobody: the workers watch, the controller stays out of it. A plain run watches too
        - the promise is not conditional on how the suite was started.
        """
        if os.name != "nt" or os.environ.get("ABP_ALLOW_WINDOWS") == "1":
            return False
        if hasattr(config, "workerinput"):
            return True
        return getattr(config.option, "numprocesses", None) in (None, 0)

    # ---------------------------------------------------------------- the watcher
    def _scan(self) -> None:
        self.scans += 1
        me = os.getpid()
        forget_dead_handles(self._open)
        for hwnd, pid, cls, title in visible_consoles():
            if hwnd in self._open:
                continue
            self._open.add(hwnd)
            if hwnd in self._baseline:
                continue
            chain, ours = process_chain(pid, me)
            if not ours:
                continue                           # somebody else's window on a shared desktop
            lines = [f"{cls} window {hwnd:#x} titled {title!r}", *(" <= " + row for row in chain)]
            with self._lock:
                if self._current is None:
                    self._unattributed.append("\n".join(lines))
                else:
                    self._popups.setdefault(self._current, []).extend(lines)

    def _watch(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self._scan()
            except Exception:  # noqa: BLE001 - a watcher that dies silently is worse than useless
                pass

    def start(self) -> None:
        self._baseline = {hwnd for hwnd, _pid, _cls, _title in visible_consoles()}
        self._open = set(self._baseline)
        self._thread = threading.Thread(target=self._watch, name="abp-no-windows", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---------------------------------------------------------------- what pytest asks of it
    def pytest_configure(self, config: pytest.Config) -> None:
        if self.should_watch(config):
            self.start()

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        self.stop()

    def pytest_runtest_logstart(self, nodeid: str, location) -> None:
        with self._lock:
            self._current = nodeid

    def pytest_runtest_logfinish(self, nodeid: str, location) -> None:
        with self._lock:
            self._current = None

    def blame(self, nodeid: str, report):
        """Fail whichever report a popup belongs to, once per popup.

        A window seen during setup or the call itself lands on the call report, so the test shows up
        as an ordinary failure. One that only appears while fixtures are being torn down lands on the
        teardown report, which pytest prints as an error at teardown - still this test, still with
        the chain, and it does not overwrite a call-phase failure that already explained itself.
        """
        if report.when not in ("call", "teardown"):
            return report
        with self._lock:
            lines = self._popups.pop(nodeid, [])
        if lines:
            report.outcome = "failed"
            report.longrepr = _explain(lines)
        return report

    def pytest_terminal_summary(self, terminalreporter) -> None:
        with self._lock:
            left = list(self._unattributed)          # left in place: pytest_sessionfinish reads it
        if not left:
            return
        terminalreporter.write_sep("=", "a console window appeared outside any test", red=True, bold=True)
        for block in left:
            terminalreporter.write_line(block)
            terminalreporter.write_line("")

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.stop()
        if self._unattributed and exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


watcher = NoWindows()


# pytest registers the *module* named in conftest's pytest_plugins, never an instance of it, so
# these module-level hooks are the plugin's real surface; each one just hands off to the singleton
# above, which is also what the tests exercise directly.


def pytest_configure(config: pytest.Config) -> None:
    watcher.pytest_configure(config)


def pytest_unconfigure(config: pytest.Config) -> None:
    watcher.pytest_unconfigure(config)


def pytest_runtest_logstart(nodeid: str, location) -> None:
    watcher.pytest_runtest_logstart(nodeid, location)


def pytest_runtest_logfinish(nodeid: str, location) -> None:
    watcher.pytest_runtest_logfinish(nodeid, location)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    # A new-style hook wrapper: the inner reports arrive through the yield and whatever this
    # returns replaces them. An exception raised by an inner impl is thrown in at the yield instead,
    # so it propagates untouched.
    report = yield
    return watcher.blame(item.nodeid, report) if report is not None else report


def pytest_terminal_summary(terminalreporter) -> None:
    watcher.pytest_terminal_summary(terminalreporter)


def pytest_sessionfinish(session, exitstatus) -> None:
    watcher.pytest_sessionfinish(session, exitstatus)
