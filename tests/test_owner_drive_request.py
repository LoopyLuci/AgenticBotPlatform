"""D7: an agent asks DriveTier to quarantine owner-drive paths through space_propose. The request only proposes: it calls
nothing but space_propose, and DriveTier's owner approval is the gate (docs/recovery-spec.md C2). These tests use a fake
DriveTier, so nothing can move here."""
from __future__ import annotations

import asyncio

import pytest

import bot.modules.tools  # noqa: F401  (registers the module tools, including this one)
from bot.agent_runtime import toolspec
from bot.modules import harness, owner_drive
from bot.modules.client import ModuleError


class FakeDriveTier:
    def __init__(self):
        self.calls = []
        self.answer = {"plan": {"plan_id": "p1", "moves": 1, "total_bytes": 4096, "notes": [], "items": []},
                       "approval": {"id": "ap-7", "status": "pending", "moves": 1}, "message": "waiting"}

    def __call__(self, mid, op, args=None, timeout=900.0):
        self.calls.append((mid, op, args))
        return self.answer


@pytest.fixture
def drivetier(monkeypatch):
    fake = FakeDriveTier()
    monkeypatch.setattr(harness, "call", fake)
    return fake


def test_the_request_calls_only_space_propose_with_quarantine(drivetier):
    out = owner_drive.request_quarantine([r"/ws/old/games"], "stale")
    assert drivetier.calls == [("drivetier", "space_propose",
                                {"paths": [r"/ws/old/games"], "action": "quarantine", "reason": "stale"})]
    assert "ap-7" in out and "pending" in out and "Nothing has moved" in out


@pytest.mark.parametrize("paths", [[], None, "/ws/old", ["relative\\folder"], [""], [r"\\server\share\x"],
                                   [r"/ws/old/../windows"], [r"/ws/old/../x"]])
def test_bad_paths_are_refused_before_drivetier_is_called(drivetier, paths):
    with pytest.raises(ModuleError) as err:
        owner_drive.request_quarantine(paths)
    assert err.value.code == "bad_request"
    assert drivetier.calls == []


def test_too_many_paths_are_refused(drivetier):
    with pytest.raises(ModuleError, match="at most"):
        owner_drive.request_quarantine([rf"/ws/old/{i}" for i in range(owner_drive.MAX_PATHS + 1)])
    assert drivetier.calls == []


def test_nothing_to_propose_says_why(drivetier):
    drivetier.answer = {"plan": {"moves": 0, "notes": [r"no scanned candidate under /ws/old: scan that folder first"]},
                        "message": "no moves to propose"}
    out = owner_drive.request_quarantine([r"/ws/old"])
    assert out.startswith("Nothing requested") and "scan that folder first" in out


def test_a_drivetier_refusal_is_an_error(drivetier):
    drivetier.answer = {"error": "no space scan yet: run space_scan first"}
    with pytest.raises(ModuleError, match="no space scan") as err:
        owner_drive.request_quarantine([r"/ws/old"])
    assert err.value.code == "refused"


def test_the_tool_needs_no_abp_prompt_because_the_owner_approves_in_drivetier():
    spec = toolspec.spec_for(owner_drive.NAME)
    assert spec.origin == "registered" and spec.permission == "external"
    assert not toolspec.registered_dangerous(owner_drive.NAME, {"paths": [r"/ws/old"]})


def test_the_tool_returns_the_line_or_a_plain_error(drivetier, tmp_path):
    line = asyncio.run(toolspec.dispatch(owner_drive.NAME, {"paths": [r"/ws/old"], "reason": "r"}, workspace=tmp_path))
    assert line.startswith("Requested 1 move(s)")
    bad = asyncio.run(toolspec.dispatch(owner_drive.NAME, {"paths": ["relative"]}, workspace=tmp_path))
    assert bad.startswith("Error (bad_request)") and drivetier.calls == [("drivetier", "space_propose", {
        "paths": [r"/ws/old"], "action": "quarantine", "reason": "r"})]
