"""abp_cli's gate/instance/dev commands - the parts that can be exercised without
starting anything.

`gate start` is deliberately NOT run here: it launches a real gate, and on this
machine that means an ABP on the real data. What is here is everything a person
runs when something is wrong - `gate status`, and the commands that have to say
so plainly when there is no gate - plus the one piece of formatting that decides
whether "the gate has given up on this instance" is visible or buried in JSON.

The commands are driven through the same parser and the same `run()` the real CLI
uses (`python -m abp_cli ...` calls exactly this), so a broken subcommand fails
here too. The gate itself, its instances and its sockets are covered end to end
in tests/test_gate.py.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from abp_cli import gate as gate_cli
from abp_cli.__main__ import _parser
from abp_gate import limits
from abp_gate import manager as gate_manager
from abp_gate import paths, procs

# Every test here starts real ABP instances (and a gate). Run them one after another in one worker: several at once on
# a machine that is also running the rest of the suite starve each other of the CPU their health checks need.
pytestmark = pytest.mark.xdist_group("abp_gate_live")

GATE_ENV_VARS = ("ABP_HOME", "ABP_INSTANCES_DIR", "ABP_GATE_CODE_ROOT", "ABP_GATE_PUBLIC_PORTS",
                 "ABP_GATE_CONTROL_PORT", "DASHBOARD_TOKEN", "ABP_GATE_INSTANCE_LIFETIME",
                 "ABP_GATE_STARTUP_DIR")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """A throwaway instances dir, and os.environ restored afterwards.

    abp_gate.paths resolves every root from the environment on EVERY call, and the
    CLI writes the flags it is given into the environment - which would otherwise
    leak this test's throwaway paths into every later test in the run. Swapping the
    whole dict means monkeypatch can put the original back."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for name in GATE_ENV_VARS:
        os.environ.pop(name, None)
    yield


def run(argv: list[str], tmp_path, capsys) -> tuple[int, str, str]:
    """`python -m abp_cli <argv>`, in process, pointed at a throwaway instances dir."""
    args = _parser().parse_args([*argv, "--instances-dir", str(tmp_path / "instances")])
    out = io.StringIO()
    with redirect_stdout(out):
        code = asyncio.run(gate_cli.run(args))
    captured = capsys.readouterr()
    return code, out.getvalue() + captured.out, captured.err



def test_gate_status_without_a_gate_says_how_to_start_one(tmp_path, capsys):
    code, out, err = run(["gate", "status"], tmp_path, capsys)
    assert code == 0, err
    printed = json.loads(out)
    assert printed["running"] is False
    assert "abp_cli gate start" in printed["hint"]
    assert str(tmp_path / "instances") in printed["instances_dir"]


def test_instance_commands_refuse_without_a_gate(tmp_path, capsys):
    """`instance` and `dev` need a gate to talk to; saying which command to run
    is the difference between a two-second fix and an afternoon."""
    for argv in (["instance", "list"], ["instance", "swap", str(tmp_path)], ["dev", "status"]):
        code, _out, err = run(argv, tmp_path, capsys)
        assert code == 1, argv
        assert "no gate is running" in err, (argv, err)
        assert "abp_cli gate start" in err, (argv, err)


def test_the_documented_lifetime_defaults_differ_on_purpose():
    """`gate start` asks for `detached`; the daemon's own default is `gate`.

    The difference is the whole "ABP survives the gate" promise: `detached` lets
    the active instance outlive the gate so the next gate re-adopts it, `gate`
    lets nothing survive. Both are defaults somebody could change without
    noticing, so both are asserted."""
    assert _parser().parse_args(["gate", "start"]).instance_lifetime == gate_manager.LIFETIME_DETACHED
    assert limits.instance_lifetime() == gate_manager.LIFETIME_GATE
    assert gate_manager.LIFETIMES == (gate_manager.LIFETIME_GATE, gate_manager.LIFETIME_DETACHED)


def test_gate_status_reports_a_spent_restart_budget(capsys):
    """A circuit breaker nobody can see is just an outage that stops being fixed.

    Feeds `gate status`'s formatter exactly what the control API answers when the
    watcher has given up, and asserts the words come out in the plain output."""
    payload = {
        "gate": {
            "limits": {"alive": 1, "max_instances": 8, "instance_lifetime": "gate"},
            "restarts": {
                "window_s": 600.0, "limit": 3,
                "instances": {
                    "prod": {"restarts_last_window": 3, "budget": 3, "circuit_open": True,
                             "ever_healthy": True},
                    "agent-box": {"restarts_last_window": 0, "budget": 3, "circuit_open": False,
                                  "ever_healthy": True},
                },
            },
        },
        "instances": {
            "prod": {"name": "prod", "health": "failed",
                     "error": "prod was restarted 3 time(s) in the last 10 minute(s) and is "
                              "still not healthy; the gate has stopped restarting it."},
        },
    }
    printed = io.StringIO()
    args = _parser().parse_args(["gate", "status"])
    with redirect_stdout(printed):
        gate_cli._print(args, payload)
        gate_cli._print_watch(payload)
    out = printed.getvalue()
    assert "instances 1/8 alive, lifetime gate" in out
    assert "prod: 3/3 restarts in the last 10m - RESTARTING STOPPED" in out
    assert "agent-box: 0/3 restarts in the last 10m" in out
    assert "prod [failed]" in out
    assert "has stopped restarting it" in out
    # The token variable NAME is printed; there is no token to print here at all.
    assert "DASHBOARD_TOKEN=" not in out


def test_sandbox_output_names_the_token_variable_and_never_its_value(tmp_path, capsys):
    """A sandbox's .env is a copy of the real one, so printing its token would
    spread the real install's credentials into a terminal, a log, or an agent's
    transcript. What the caller needs is the URL and the variable to read."""
    args = _parser().parse_args(["instance", "sandbox", str(tmp_path)])
    printed = io.StringIO()
    with redirect_stdout(printed):
        gate_cli._print_sandbox(args, {"instance": "box", "url": "http://127.0.0.1:8123",
                                       "data_root": str(tmp_path / "box"),
                                       "token_var": "DASHBOARD_TOKEN", "note": "sandboxed"})
    out = printed.getvalue()
    assert "export ABP_URL=http://127.0.0.1:8123" in out
    assert f"export DASHBOARD_TOKEN=$(read from {tmp_path / 'box' / '.env'})" in out
    assert "abp_cli --host 127.0.0.1:8123 bots list" in out


# ----------------------------------------------------------------- autostart
#
# The Startup folder belongs to the person, so every one of these runs against a
# throwaway one (`--startup-dir`, which is what ABP_GATE_STARTUP_DIR is for). What
# is being tested is the file the command writes and the words it says - a
# Startup entry is a text file, and Windows is what reads it.


def autostart(tmp_path, capsys, action: str):
    return run(["gate", "autostart", action, "--startup-dir", str(tmp_path / "Startup")],
               tmp_path, capsys)


@pytest.fixture
def startup(tmp_path):
    folder = tmp_path / "Startup"
    folder.mkdir()
    return folder


def test_autostart_writes_a_hidden_logon_entry_and_removes_it_again(tmp_path, capsys, startup):
    """`on` writes exactly one file into the Startup folder, `status` can read it
    back, and `off` takes it away again - and says so when there is nothing left
    to take, because "no autostart entry" and "removed it" are different
    answers to different questions."""
    entry = startup / gate_cli.AUTOSTART_NAME

    code, out, err = autostart(tmp_path, capsys, "status")
    assert code == 0, err
    assert "not installed" in out and "abp_cli gate autostart on" in out
    assert not entry.exists()

    code, out, err = autostart(tmp_path, capsys, "on")
    assert code == 0, err
    assert entry.is_file(), "the logon entry was not written"
    assert [p.name for p in startup.iterdir()] == [gate_cli.AUTOSTART_NAME]
    assert "will be up at every logon" in out and str(entry) in out
    assert "abp_cli gate autostart off" in out

    code, out, err = autostart(tmp_path, capsys, "status")
    assert code == 0, err
    assert f"installed: {entry}" in out
    assert "starts: " in out and "-m abp_cli gate start" in out
    assert "points at this checkout" in out

    code, out, err = autostart(tmp_path, capsys, "off")
    assert code == 0, err
    assert not entry.exists() and list(startup.iterdir()) == []
    assert "removed" in out and str(entry) in out

    code, out, err = autostart(tmp_path, capsys, "off")
    assert code == 0, err
    assert "nothing to remove" in out


@pytest.mark.skipif(sys.platform != "win32", reason="the logon entry is a Windows Startup-folder file")
def test_the_logon_entry_starts_the_gate_hidden_and_from_this_checkout(tmp_path, capsys, startup):
    """The three things that decide whether this works at all:

      * it runs `abp_cli gate start` - the documented way in, which detaches the
        daemon and says where its log is - and the interpreter and code root
        this checkout would use, not a path that only existed on the machine
        where the entry was written;
      * `sh.Run cmd, 0, False` - window style 0 and do not wait. A console
        window appearing while somebody is logging in to their own machine is
        the one thing this must never do, and a Startup shortcut would do
        exactly that;
      * PYTHONPATH is set on the PROCESS environment WScript.Shell hands it,
        because a logon entry inherits nothing from any shell."""
    autostart(tmp_path, capsys, "on")
    text = (startup / gate_cli.AUTOSTART_NAME).read_text(encoding="utf-8")
    code_root = str(paths.code_root())
    python = str(procs.python_for(paths.code_root()))
    run_line = [line for line in text.splitlines() if line.startswith("sh.Run ")][0]
    assert run_line.startswith(f'sh.Run "{python} -m abp_cli gate start --code-root {code_root}')
    assert run_line.endswith('", 0, False')
    # The roots are named rather than inherited: a logon entry has no shell to
    # inherit an ABP_HOME from, and one that came up against a different one
    # would be a second front door on the same port.
    assert f"--instances-dir {tmp_path / 'instances'}" in run_line
    assert f'env.Item("PYTHONPATH") = "{code_root}"' in text
    assert f'sh.CurrentDirectory = "{code_root}"' in text
    assert "WScript.Shell" in text and 'Environment("PROCESS")' in text
    # Nothing that would start a second, competing ABP on the same port.
    assert "DASHBOARD_PORT" not in text


@pytest.mark.skipif(sys.platform != "win32", reason="the logon entry is a Windows Startup-folder file")
def test_the_logon_entry_is_valid_vbscript(tmp_path, startup):
    """Windows runs this file with no error handling at all, so a syntax error
    is a logon with no ABP and nothing to see. cscript parses and executes the
    REAL generated file, with the one command line replaced by a harmless one so
    the check cannot start a gate: everything else - the object model calls, the
    string literals, the quoting - is exactly what will be there at logon."""
    if shutil.which("cscript.exe") is None:
        pytest.skip("cscript.exe is not on PATH")
    info = gate_cli.autostart_state(str(startup))
    script = startup / "syntax-check.vbs"
    script.write_text(gate_cli.autostart_script(
        command="cmd.exe /c rem abp-autostart-syntax-check",
        code_root=Path(info["code_root"]), python=Path(info["python"])), encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["cscript.exe", "//nologo", "//B", str(script)], capture_output=True, text=True, timeout=120,
        creationflags=0x08000000,   # CREATE_NO_WINDOW: never a console on the person's desktop
    )
    assert done.returncode == 0, f"cscript rejected the logon entry: {done.stdout} {done.stderr}"


def test_a_logon_entry_pointing_at_another_checkout_is_reported_not_hidden(tmp_path, capsys, startup):
    """The failure mode a Startup folder actually has: an entry written by a
    build in a checkout that has since been moved, renamed or deleted. It starts
    a gate that cannot work, and the only symptom would be a log nobody reads -
    so `status` compares what is in the file with what this checkout would write,
    and `on` says that it is replacing it."""
    (startup / gate_cli.AUTOSTART_NAME).write_text(
        gate_cli.autostart_script(command="D:/gone/venv/Scripts/python.exe -m abp_cli gate start --code-root D:/gone",
                                  code_root=Path("D:/gone"), python=Path("D:/gone/.venv/Scripts/python.exe")),
        encoding="utf-8")
    code, out, err = autostart(tmp_path, capsys, "status")
    assert code == 0, err
    assert "installed:" in out
    assert "does NOT point at this checkout" in out
    assert "D:/gone" in out

    code, out, err = autostart(tmp_path, capsys, "on")
    assert code == 0, err
    assert f"replaced the logon entry that was pointing at {gate_cli.paths.code_root()}" in out, out
    code, out, err = autostart(tmp_path, capsys, "status")
    assert "points at this checkout" in out, out


def test_the_startup_folder_can_come_from_the_environment(tmp_path, capsys, monkeypatch):
    """ABP_GATE_STARTUP_DIR is how a test - or somebody with an unusual profile
    layout - points this somewhere other than their own profile. A named folder
    still wins over the variable, and the variable is what is left when nothing
    is named."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("ABP_GATE_STARTUP_DIR", str(elsewhere))
    assert gate_cli.startup_dir() == elsewhere
    assert gate_cli.autostart_path() == elsewhere / gate_cli.AUTOSTART_NAME
    assert gate_cli.startup_dir(str(tmp_path / "Startup")) == tmp_path / "Startup"

    args = _parser().parse_args(["gate", "autostart", "on", "--instances-dir", str(tmp_path / "instances")])
    out = io.StringIO()
    with redirect_stdout(out):
        code = asyncio.run(gate_cli.run(args))
    assert code == 0, out.getvalue()
    assert (elsewhere / gate_cli.AUTOSTART_NAME).is_file()
    assert not (tmp_path / "Startup").exists()
