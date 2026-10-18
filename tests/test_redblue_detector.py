"""WatchDog P1 detector: each signature fires on the right event, benign events don't, Incidents are valid."""
from bot.redblue.detector import CONTAINMENT, Detector
from bot.redblue.events import Event, EventFeed
from bot.redblue.schemas import validate_incident

D = Detector()


def ev(**kw):
    kw.setdefault("ts", 1000.0)
    kw.setdefault("actor", "pid:123")
    kw.setdefault("action", "")
    kw.setdefault("target", "")
    return Event(**kw)


def test_sensitive_fs_escape_is_an_escape_with_containment():
    inc = D.inspect(ev(action="write_file", target=r"C:\Users\limpi\.ssh\authorized_keys", grant_result="denied"))
    assert inc is not None and inc.verdict == "escape"
    assert inc.matched_signature == "fs_escape_denied_sensitive"
    assert inc.containment_action in CONTAINMENT and inc.containment_action == "kill_cell"
    validate_incident(inc.to_dict())            # the produced Incident is schema-valid


def test_network_attempt_is_escape():
    assert D.inspect(ev(action="socket.connect", target="93.184.216.34:80")).verdict == "escape"
    assert D.inspect(ev(action="run_shell", target="curl https://example.com")).matched_signature == "network_attempt_confined"


def test_job_breakaway_is_escape():
    inc = D.inspect(ev(action="CreateProcess CREATE_BREAKAWAY_FROM_JOB"))
    assert inc.verdict == "escape" and inc.matched_signature == "job_breakaway"


def test_persistence_attempt_is_escape():
    inc = D.inspect(ev(action="reg_add", target=r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v x"))
    assert inc.verdict == "escape" and inc.containment_action == "revoke_tool"


def test_denied_outside_workspace_write_is_escape():
    inc = D.inspect(ev(action="write_file", target=r"D:\pwn.txt", grant_result="denied"))
    assert inc.verdict == "escape" and inc.matched_signature == "fs_write_denied_outside_ws"


def test_generic_denied_is_only_suspicious():
    inc = D.inspect(ev(action="stat", target="somefile", grant_result="denied"))
    assert inc.verdict == "suspicious" and inc.matched_signature == "grant_denied_generic"


def test_benign_workspace_write_is_not_flagged():
    assert D.inspect(ev(action="write_file", target=r"C:\work\pal.py", grant_result="allowed")) is None
    assert D.inspect(ev(action="read_file", target=r"C:\work\test_pal.py", grant_result="allowed")) is None


def test_spawn_storm_fires_only_over_the_threshold():
    feed = EventFeed()
    for i in range(11):
        feed.add(ev(ts=1000.0 + i * 0.1, action="process_spawn", cell_id="cellA"))
    last = ev(ts=1001.2, action="process_spawn", cell_id="cellA")
    feed.add(last)
    inc = D.inspect(last, feed)
    assert inc is not None and inc.matched_signature == "spawn_storm" and inc.verdict == "escape"
    # a lone spawn in a different cell does not trip it
    assert D.inspect(ev(action="process_spawn", cell_id="cellB"), feed) is None


def test_scan_returns_one_incident_per_bad_event():
    feed = EventFeed()
    feed.add(ev(action="write_file", target=r"C:\work\ok.py", grant_result="allowed"))   # benign
    feed.add(ev(action="socket.connect", target="10.0.0.1:443"))                          # escape
    feed.add(ev(action="write_file", target=r"C:\Users\x\.ssh\k", grant_result="denied")) # escape
    incs = D.scan(feed)
    assert len(incs) == 2 and all(i.verdict == "escape" for i in incs)
