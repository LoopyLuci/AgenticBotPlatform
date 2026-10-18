"""IncidentJournal: append-only JSONL persistence for WatchDog incidents — round-trips, tolerates a torn
line, wires as a WatchDog callback, and aggregates a dashboard summary. No containment is executed."""
from bot.redblue.events import Event
from bot.redblue.journal import IncidentJournal
from bot.redblue.schemas import Finding, Incident, VectorClass
from bot.redblue.watchdog import WatchDog


def _inc(ts=1.0, verdict="escape", sig="network_attempt_confined", contain="kill_cell"):
    return Incident(timestamp=ts, detector="watchdog", actor="pid:1", action="socket.connect",
                    target="1.2.3.4:80", verdict=verdict, matched_signature=sig, containment_action=contain)


def test_record_and_read_back_roundtrips(tmp_path):
    j = IncidentJournal(tmp_path / "incidents.jsonl")
    j.record(_inc(ts=1.0))
    j.record(_inc(ts=2.0, verdict="suspicious", sig="grant_denied_generic", contain="none"))
    incs = j.incidents()
    assert [i.timestamp for i in incs] == [1.0, 2.0]          # write order preserved
    assert incs[0].matched_signature == "network_attempt_confined"


def test_findings_and_incidents_are_separated_in_a_mixed_journal(tmp_path):
    j = IncidentJournal(tmp_path / "mixed.jsonl")
    j.record(_inc())
    j.record(Finding(vector_class=VectorClass.NETWORK, hypothesis="h", repro="r", predicted_blue_signal="s"))
    assert len(j.incidents()) == 1 and len(j.findings()) == 1
    assert j.findings()[0].vector_class == VectorClass.NETWORK


def test_a_torn_trailing_line_is_skipped_not_fatal(tmp_path):
    p = tmp_path / "crash.jsonl"
    j = IncidentJournal(p)
    j.record(_inc(ts=5.0))
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"_kind": "incident", "timestamp": 6.0, "detec')   # half-written crash line
    incs = j.incidents()
    assert len(incs) == 1 and incs[0].timestamp == 5.0


def test_an_invalid_but_whole_row_is_dropped(tmp_path):
    p = tmp_path / "bad.jsonl"
    j = IncidentJournal(p)
    j.record(_inc(ts=7.0))
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"_kind": "incident", "timestamp": "not-a-number"}\n')   # fails validation
    assert [i.timestamp for i in j.incidents()] == [7.0]


def test_reading_a_missing_file_is_empty(tmp_path):
    j = IncidentJournal(tmp_path / "nope.jsonl")
    assert j.incidents() == [] and j.findings() == [] and j.summary()["total"] == 0


def test_summary_aggregates_for_a_dashboard(tmp_path):
    j = IncidentJournal(tmp_path / "s.jsonl")
    j.record(_inc(ts=1.0, verdict="escape"))
    j.record(_inc(ts=3.0, verdict="escape", sig="job_breakaway", contain="kill_cell"))
    j.record(_inc(ts=2.0, verdict="suspicious", sig="grant_denied_generic", contain="none"))
    s = j.summary()
    assert s["total"] == 3 and s["escapes"] == 2
    assert s["by_verdict"] == {"escape": 2, "suspicious": 1}
    assert s["by_containment"]["kill_cell"] == 2
    assert s["last_ts"] == 3.0


def test_wires_as_a_watchdog_callback_and_persists_live(tmp_path):
    j = IncidentJournal(tmp_path / "live.jsonl")
    wd = WatchDog(on_incident=j.as_watchdog_callback())
    wd.observe_event(Event(ts=1.0, actor="pid:9", action="socket.connect", target="9.9.9.9:80"))
    wd.observe_event(Event(ts=2.0, actor="pid:9", action="stat", target="x", grant_result="denied"))
    # both an escape and a suspicious incident were recorded by WatchDog -> both persisted
    assert len(j.incidents()) == 2 and j.summary()["escapes"] == 1
