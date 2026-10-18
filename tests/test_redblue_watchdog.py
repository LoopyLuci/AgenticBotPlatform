"""WatchDog P1 runtime: ingest->detect->record, propose-only by default, handlers when wired, summary."""
from bot.redblue.events import Event
from bot.redblue.watchdog import WatchDog


def test_benign_telemetry_records_nothing():
    wd = WatchDog()
    assert wd.observe({"actor": "pid:1", "action": "write_file", "target": r"C:\work\a.py",
                       "allowed": True}, "grant_check") is None
    assert wd.incidents == []


def test_escape_grant_check_is_detected_and_recorded():
    wd = WatchDog()
    inc = wd.observe({"actor": "pid:9", "action": "write_file", "target": r"C:\Users\x\.ssh\k",
                      "allowed": False}, "grant_check")
    assert inc is not None and inc.verdict == "escape" and inc.containment_action == "kill_cell"
    assert len(wd.incidents) == 1 and wd.recent_incidents()[-1] is inc


def test_tool_call_network_attempt_detected():
    wd = WatchDog()
    inc = wd.observe({"agent": "worker", "tool": "run_shell",
                      "args": {"command": "curl https://evil.example"}}, "tool_call")
    assert inc is not None and inc.matched_signature == "network_attempt_confined"


def test_contain_is_propose_only_without_a_handler():
    wd = WatchDog()
    inc = wd.observe_event(Event(ts=1.0, actor="pid:2", action="write_file",
                                 target=r"C:\Users\x\.ssh\k", grant_result="denied"))
    assert wd.contain(inc) == "proposed:kill_cell"      # nothing executed by default


def test_contain_runs_a_wired_handler():
    calls = []
    wd = WatchDog(handlers={"kill_cell": lambda i: calls.append(i.actor) or "killed:" + i.actor})
    inc = wd.observe_event(Event(ts=1.0, actor="pid:7", action="socket.connect", target="1.2.3.4:80"))
    assert wd.contain(inc) == "killed:pid:7" and calls == ["pid:7"]


def test_on_incident_callback_and_summary():
    seen = []
    wd = WatchDog(on_incident=seen.append)
    wd.observe_event(Event(ts=1.0, actor="a", action="write_file", target=r"C:\Users\x\.ssh\k",
                           grant_result="denied"))
    wd.observe_event(Event(ts=2.0, actor="b", action="stat", target="x", grant_result="denied"))  # suspicious
    assert len(seen) == 2
    s = wd.summary()
    assert s["total"] == 2 and s["by_verdict"]["escape"] == 1 and s["by_verdict"]["suspicious"] == 1
    assert s["by_signature"]["fs_escape_denied_sensitive"] == 1


def test_a_failing_handler_is_surfaced_not_raised():
    def boom(_i):
        raise RuntimeError("down")
    wd = WatchDog(handlers={"kill_cell": boom})
    inc = wd.observe_event(Event(ts=1.0, actor="p", action="socket.connect", target="1.1.1.1:53"))
    assert wd.contain(inc).startswith("failed:kill_cell:")
