"""The live WatchDog (bot/redblue/live.py): passive, propose-only monitoring of agent tool calls and permission decisions,
journaled, feeding GET /api/redblue/summary. The test session turns it off (tests/conftest.py); these tests turn it on with a
journal under tmp_path."""
from __future__ import annotations

import asyncio
import queue

import pytest
from fastapi.testclient import TestClient

from bot.agent_runtime import permissions, tools
from bot.redblue import journal as journal_mod
from bot.redblue import live
from bot.redblue.watchdog import WatchDog

SSH = "/home/agent/.ssh/id_ed25519"


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setenv("ABP_REDBLUE_WATCHDOG", "on")
    monkeypatch.setattr(journal_mod, "DEFAULT_PATH", tmp_path / "redblue" / "incidents.jsonl")
    live._reset_for_tests()
    yield journal_mod.IncidentJournal(tmp_path / "redblue" / "incidents.jsonl")
    live._reset_for_tests()


def deny(reason="denied by rule"):
    return permissions.Verdict("deny", reason)


def test_the_test_session_keeps_it_off(monkeypatch):
    assert live.enabled() is False                            # conftest: never the live install's journal from a test run
    before = live.stats()["received"]
    live.tool_call("read_file", {"path": SSH})
    assert live.stats()["received"] == before


def test_a_denied_sensitive_read_and_a_network_call_are_journaled(on):
    live.grant_check("read_file", {"path": SSH}, deny(), instance_id=4)
    live.tool_call("web_fetch", {"url": "http://93.184.216.34/"}, instance_id=4)
    live.tool_call("read_file", {"path": "notes.txt"}, instance_id=4)      # benign: nothing
    assert live.drain(5)
    sigs = [i.matched_signature for i in on.incidents()]
    assert "fs_escape_denied_sensitive" in sigs and len(on.incidents()) >= 2
    assert all(i.actor == "instance:4" for i in on.incidents())
    st = live.stats()
    assert st["received"] == st["observed"] == 3 and st["incidents"] == len(on.incidents()) and st["dropped"] == 0
    assert st["mode"] == "propose-only" and st["handlers"] == 0


def test_it_never_contains_anything(on, monkeypatch):
    monkeypatch.setattr(WatchDog, "contain", lambda self, inc: pytest.fail("the live WatchDog must never contain"))
    for _ in range(3):
        live.grant_check("read_file", {"path": SSH}, deny())
    assert live.drain(5) and live.watchdog().handlers == {}
    assert len(on.incidents()) == 3


def test_only_the_fields_the_detector_reads_are_kept(on, monkeypatch):
    seen = []
    monkeypatch.setattr(live, "emit", lambda raw, source: seen.append((raw, source)))
    live.tool_call("write_file", {"path": "a.py", "content": "SECRET-CONTENT" * 1000}, instance_id=1)
    raw, source = seen[0]
    assert source == "tool_call" and raw["args"] == {"path": "a.py"} and "SECRET-CONTENT" not in str(raw)


def test_a_full_queue_drops_and_counts_instead_of_blocking(on, monkeypatch):
    full = queue.Queue(maxsize=1)
    full.put(({}, "tool_call"))
    monkeypatch.setattr(live, "_queue", full)
    monkeypatch.setattr(live, "_ensure_thread", lambda: None)            # no worker: the queue stays full
    live.tool_call("read_file", {"path": "x"})
    assert live.stats()["dropped"] == 1


def test_permission_decisions_and_tool_calls_are_fed_in_from_the_runtime(on, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(live, "emit", lambda raw, source: seen.append(source))
    v = permissions.evaluate("read_file", {"path": "a.txt"}, workspace=tmp_path, instance_id=None, session="s")
    assert v.decision and seen == ["grant_check"]
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    assert "hi" in asyncio.run(tools.execute_tool("read_file", {"path": "a.txt"}, workspace=tmp_path))
    assert seen == ["grant_check", "tool_call"]


def test_a_broken_monitor_never_changes_a_decision_or_a_tool(on, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("monitor down")

    monkeypatch.setattr(live, "grant_check", boom)
    monkeypatch.setattr(live, "tool_call", boom)
    v = permissions.evaluate("read_file", {"path": "a.txt"}, workspace=tmp_path, instance_id=None, session="s")
    assert v.decision
    (tmp_path / "a.txt").write_text("still works", encoding="utf-8")
    assert "still works" in asyncio.run(tools.execute_tool("read_file", {"path": "a.txt"}, workspace=tmp_path))


def test_the_summary_shows_the_live_feed(on, monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app

    live.grant_check("read_file", {"path": SSH}, deny())
    assert live.drain(5)
    body = TestClient(build_app()).get("/api/redblue/summary", headers={"X-Dashboard-Token": "test-token"}).json()
    assert body["total"] >= 1 and body["live"]["mode"] == "propose-only" and body["live"]["handlers"] == 0
    assert body["live"]["observed"] >= 1 and body["dry_run"] is True


def test_a_self_test_is_counted_not_detected(on):
    with live.self_test("unit"):
        live.tool_call("run_shell", {"command": "curl.exe http://127.0.0.1:9/"})
    live.tool_call("run_shell", {"command": "curl.exe http://127.0.0.1:9/"})       # outside it: detected as before
    assert live.drain(5)
    st = live.stats()
    assert st["suppressed"] == 1 and st["received"] == 1 and len(on.incidents()) == 1


@pytest.mark.skipif(__import__("os").name != "nt", reason="the confinement probes need a Windows AppContainer")
def test_the_conductor_s_confinement_probe_is_not_an_escape(on, tmp_path):
    """Run 5 of the LF5 measurement journaled the probe's deliberate loopback attempt as network_attempt_confined."""
    from bot.conductor import confine

    ws = tmp_path / "ws"
    ws.mkdir()
    confine.verify(ws, extra_paths=confine.sandbox_paths())
    assert live.drain(10)
    st = live.stats()
    assert st["suppressed"] > 0 and st["received"] == 0 and on.incidents() == []
