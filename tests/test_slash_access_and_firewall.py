"""bot/slash_access.py (who may run which slash command) and bot/firewall.py
(the dashboard port's Windows Firewall rule). Both are security-relevant and
had no direct tests."""
from __future__ import annotations

import subprocess

import pytest

from bot import firewall, slash_access


# ------------------------------------------------------------ slash access --

def _inst(**kw):
    base = {"platform": "telegram", "admin_user_ids": [], "action_overrides": {}}
    base.update(kw)
    return base


def test_no_admin_list_means_gating_is_off():
    inst = _inst()
    assert slash_access.tier(inst, 42) == "unrestricted"
    assert slash_access.allowed_commands(inst, 42, "dm") is None
    assert slash_access.can_run(inst, 42, "group", "restart")


def test_admins_can_run_everything_and_ids_are_normalised_per_platform():
    inst = _inst(admin_user_ids=["42"])
    assert slash_access.tier(inst, 42) == "admin"
    assert slash_access.can_run(inst, "42", "group", "restart")
    slack = _inst(platform="slack", admin_user_ids=["U123"])
    assert slash_access.is_admin(slack, "U123")
    assert not slash_access.is_admin(slack, "u123")


def test_non_admins_get_their_scope_list_plus_the_floor():
    inst = _inst(admin_user_ids=[1], action_overrides={"slash_access": {"dm_user_commands": ["ask"],
                                                                        "group_user_commands": ["status"]}})
    assert slash_access.tier(inst, 2) == "user"
    assert slash_access.allowed_commands(inst, 2, "dm") == {"ask", "help", "start", "whoami"}
    assert slash_access.allowed_commands(inst, 2, "group") == {"status", "help", "start", "whoami"}
    assert not slash_access.can_run(inst, 2, "dm", "restart")
    assert slash_access.can_run(inst, 2, "group", "whoami")


def test_group_scope_falls_back_to_the_dm_list():
    inst = _inst(admin_user_ids=[1], action_overrides={"slash_access": {"dm_user_commands": ["ask"]}})
    assert slash_access.can_run(inst, 2, "group", "ask")


def test_a_user_with_no_configured_list_still_has_the_floor_only():
    inst = _inst(admin_user_ids=[1])
    assert slash_access.allowed_commands(inst, 2, "dm") == {"help", "start", "whoami"}


# ------------------------------------------------------------ firewall --

class _Result:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_everything_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(firewall.platform, "system", lambda: "Linux")
    assert firewall.has_inbound_rule(8787) is None
    assert firewall.open_inbound_port(8787)[0] is False
    assert firewall.status(8787) == {"supported": False, "port": 8787, "rule_present": None}


@pytest.mark.parametrize("out,expected", [
    ("No rules match the specified criteria.", False),
    ("Rule Name:   AgenticBotPlatform Dashboard (TCP 8787)", True),
    ("something unexpected", None),
])
def test_rule_presence_is_read_from_netsh_output(monkeypatch, out, expected):
    monkeypatch.setattr(firewall.platform, "system", lambda: "Windows")
    monkeypatch.setattr(firewall.subprocess, "run", lambda *a, **k: _Result(1, out))
    assert firewall.has_inbound_rule(8787) is expected


@pytest.mark.parametrize("bad", ["8787; Remove-Item C:\\", "8787' ; calc ; '", 0, 70000, True, 3.5, None])
def test_only_a_real_port_ever_reaches_the_elevated_command(monkeypatch, bad):
    monkeypatch.setattr(firewall.platform, "system", lambda: "Windows")
    ran = []
    monkeypatch.setattr(firewall.subprocess, "run", lambda *a, **k: ran.append(a) or _Result(0))
    ok, msg = firewall.open_inbound_port(bad)
    assert ok is False and "not a valid TCP port" in msg
    assert ran == []


@pytest.mark.parametrize("rc,ok,fragment", [(0, True, "added"), (1223, False, "declined"), (5, False, "code 5")])
def test_open_reports_the_elevated_outcome(monkeypatch, rc, ok, fragment):
    monkeypatch.setattr(firewall.platform, "system", lambda: "Windows")
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return _Result(rc)

    monkeypatch.setattr(firewall.subprocess, "run", fake_run)
    got_ok, msg = firewall.open_inbound_port(8787)
    assert got_ok is ok and fragment in msg
    assert "localport=8787" in seen["cmd"][-1] and "-Verb RunAs" in seen["cmd"][-1]


def test_a_launch_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(firewall.platform, "system", lambda: "Windows")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("powershell", 60)

    monkeypatch.setattr(firewall.subprocess, "run", boom)
    assert firewall.open_inbound_port(8787)[0] is False
    assert firewall.has_inbound_rule(8787) is None
