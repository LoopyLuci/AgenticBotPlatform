"""tests/no_windows_plugin.py - the watcher that fails a test which puts a window on the desktop.

Nothing here opens a window: this suite must never be the thing that flashes a console. So the
OS-facing parts are checked against the real Windows API on whatever is already open (the real
`EnumWindows`, the real process table), and the one branch that cannot be produced safely - a
popup seen while a test runs - is driven through the plugin's own pytest hook in a nested session
with the window entry handed in. That entry is the only fiction in the file and it is called out
where it is injected.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from no_windows_plugin import (
    CONSOLE_CLASSES,
    NoWindows,
    forget_dead_handles,
    process_chain,
    visible_consoles,
    watcher,
)

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="console windows are a Windows thing")

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def test_the_watcher_is_registered_and_actually_watching(request):
    """conftest.py registers it; without that it would be dead code nobody notices is broken, and
    without the thread it would be a plugin that checks nothing."""
    assert isinstance(watcher, NoWindows)
    assert request.config.pluginmanager.hasplugin("no_windows_plugin")
    if WINDOWS and os.environ.get("ABP_ALLOW_WINDOWS") != "1":
        assert watcher.should_watch(request.config) is True
        assert watcher._thread is not None and watcher._thread.is_alive(), "the watcher thread is not running"
    else:
        assert watcher.should_watch(request.config) is False


@windows_only
def test_enumerating_console_windows_is_the_real_windows_api():
    """The real EnumWindows against the real desktop. Only the shape is asserted: whether this
    machine happens to have a console window open is nobody's business but its owner's, and the
    windows that are open belong to whoever opened them."""
    found = visible_consoles()
    assert isinstance(found, list)
    for hwnd, pid, cls, title in found:
        assert isinstance(hwnd, int) and hwnd > 0
        assert isinstance(pid, int) and pid > 0
        assert cls in CONSOLE_CLASSES, f"{cls} is not one of the three window classes this watches"
        assert isinstance(title, str)


@windows_only
def test_the_process_chain_finds_this_process_and_stops_there():
    me = os.getpid()
    rows, ours = process_chain(me, me)
    assert ours is True and rows == [f"{me} this pytest worker"]


@windows_only
def test_the_process_chain_says_when_a_process_is_not_ours():
    """pid 4 is Windows' own System process on every Windows machine: a real entry in the real
    process table, and never a descendant of a pytest worker."""
    rows, ours = process_chain(4, os.getpid())
    assert ours is False
    assert rows and rows[0].startswith("4 System"), rows


@windows_only
def test_a_window_is_only_blamed_when_its_chain_reaches_this_process():
    """The attribution rule, on a real window rather than an invented one: whichever visible
    console window this desktop has open, pointing `me` at its owner walks a real chain and finds
    itself, and pointing `me` at this pytest worker does not."""
    open_windows = visible_consoles()
    if not open_windows:
        pytest.skip("no console window is open on this desktop to attribute")
    owner = open_windows[0][1]
    assert process_chain(owner, owner)[1] is True
    assert process_chain(owner, os.getpid())[1] is (owner == os.getpid())


@windows_only
def test_a_hand_handle_window_is_remembered_and_a_dead_one_is_forgotten():
    """Windows reuses window handles, so a remembered handle whose window has gone must not hide the
    next popup that lands on it. Real windows, taken from this desktop."""
    handles = {hwnd for hwnd, _pid, _cls, _title in visible_consoles()}
    if not handles:
        pytest.skip("no console window is open on this desktop to remember")
    forget_dead_handles(handles)
    assert handles, "a window that is still open must stay remembered"
    gone = max(handles) + 0x7FFFFFF0                 # a handle nothing on this desktop holds
    handles.add(gone)
    forget_dead_handles(handles)
    assert gone not in handles


@windows_only
def test_a_scan_finds_nothing_new_when_nothing_was_opened():
    """Real scans of the real desktop against a real baseline: the windows already open are in the
    baseline, so a session that never opens anything records nothing."""
    seen = NoWindows()
    seen.start()
    try:
        for _ in range(5):
            seen._scan()
        assert seen.scans == 5
        assert seen._popups == {} and seen._unattributed == []
    finally:
        seen.stop()


# A nested session with the plugin registered and one popup handed to it: the report, the failure
# and the printed chain are all real; the window entry is injected because a test suite may not
# create one.
NESTED_CONFTEST = '''\
import no_windows_plugin

pytest_plugins = ["no_windows_plugin"]

WINDOW = (
    "ConsoleWindowClass window 0x1 titled 'C:\\\\Windows\\\\System32\\\\wsl.exe'",
    "4242 wsl.exe [C:\\\\Windows\\\\System32\\\\wsl.exe]",
    " <= 5150 python.exe [python -m hermes_cli.main gateway run]",
)


def pytest_runtest_setup(item):
    no_windows_plugin.watcher._popups.setdefault(item.nodeid, []).extend(WINDOW)
'''

NESTED_TEST = """\
def test_a_session_that_opens_nothing():
    assert True
"""


@windows_only
def test_a_popup_during_a_test_fails_that_test_and_names_the_chain(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(NESTED_CONFTEST, encoding="utf-8")
    (tmp_path / "test_popup.py").write_text(NESTED_TEST, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(TESTS)]),
           "ABP_ALLOW_WINDOWS": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-cov",
                           "-p", "no:xdist", "test_popup.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    out = done.stdout + done.stderr
    assert "1 failed" in out, out[-3000:]
    assert "a new visible console window appeared while this test was running" in out, out[-3000:]
    assert "C:\\Windows\\System32\\wsl.exe" in out, out[-3000:]
    assert "hermes_cli.main gateway run" in out, out[-3000:]
