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
from contextlib import redirect_stdout

import pytest

from abp_cli import gate as gate_cli
from abp_cli.__main__ import _parser
from abp_gate import limits
from abp_gate import manager as gate_manager

GATE_ENV_VARS = ("ABP_HOME", "ABP_INSTANCES_DIR", "ABP_GATE_CODE_ROOT", "ABP_GATE_PUBLIC_PORTS",
                 "ABP_GATE_CONTROL_PORT", "DASHBOARD_TOKEN", "ABP_GATE_INSTANCE_LIFETIME")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """A throwaway instances dir, and os.environ restored afterwards.

    abp_gate.paths resolves every root from the environment on EVERY call, and the
    CLI writes the flags it is given into the environment - which would otherwise
    leak this test's throwaway paths into every later test in the run. Swapping
    the whole dict means monkeypatch can put the original back."""
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