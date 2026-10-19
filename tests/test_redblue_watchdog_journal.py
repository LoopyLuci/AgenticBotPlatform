"""WatchDog -> IncidentJournal wiring and the read-only /api/redblue/summary endpoint: incidents land in the journal,
a failing journal never breaks monitoring, the summary has the expected shape/auth/empty case, and nothing is executed."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bot.redblue import journal as journal_mod
from bot.redblue.containment import ContainmentExecutor
from bot.redblue.detector import SIGNATURES
from bot.redblue.events import Event
from bot.redblue.journal import IncidentJournal
from bot.redblue.schemas import Finding, VectorClass
from bot.redblue.taxonomy import SIGNATURE_VECTORS, vector_class_of
from bot.redblue.watchdog import WatchDog

_SSH = Event(ts=10.0, actor="pid:7", action="read_file", target="/home/agent/.ssh/id_ed25519", grant_result="denied")
_NET = Event(ts=11.0, actor="pid:7", action="socket.connect", target="93.184.216.34:80")
_DENY = Event(ts=12.0, actor="pid:7", action="read_file", target="/ws/notes.txt", grant_result="denied")
_OK = Event(ts=13.0, actor="pid:7", action="read_file", target="/ws/notes.txt", grant_result="allowed")


class _BrokenJournal:
    path = "/home/agent/unwritable/incidents.jsonl"

    def __init__(self):
        self.calls = 0

    def record(self, _item):
        self.calls += 1
        raise OSError("disk full")


# --- WatchDog -> journal ---------------------------------------------------------------------------------------

def test_every_incident_watchdog_records_lands_in_the_journal(tmp_path):
    j = IncidentJournal(tmp_path / "incidents.jsonl")
    wd = WatchDog(journal=j)
    for ev in (_SSH, _NET, _OK, _DENY):
        wd.observe_event(ev)
    assert wd.observe({"tool": "read_file", "args": {"path": "/ws/a"}, "grant_result": "allowed", "ts": 14.0},
                      "tool_call") is None                 # benign: nothing raised, nothing journaled
    persisted = j.incidents()
    assert [i.timestamp for i in persisted] == [10.0, 11.0, 12.0]
    assert [i.to_dict() for i in persisted] == [i.to_dict() for i in wd.incidents]


def test_no_journal_is_the_default_and_unchanged():
    wd = WatchDog()
    assert wd.journal is None
    assert wd.observe_event(_NET).matched_signature == "network_attempt_confined"


def test_a_failing_journal_is_logged_and_never_breaks_watchdog(caplog):
    broken = _BrokenJournal()
    seen = []
    wd = WatchDog(journal=broken, on_incident=seen.append)
    with caplog.at_level("WARNING", logger="bot.redblue.watchdog"):
        a = wd.observe_event(_SSH)
        b = wd.observe_event(_NET)
    assert a is not None and b is not None                   # detection still returns the incidents
    assert len(wd.incidents) == 2 and len(seen) == 2          # and still records + calls back
    assert broken.calls == 2
    assert "could not persist" in caplog.text


def test_the_journal_write_never_triggers_containment(tmp_path):
    fired = []
    wd = WatchDog(journal=IncidentJournal(tmp_path / "j.jsonl"), handlers={"kill_cell": fired.append})
    wd.observe_event(_SSH)
    assert fired == []                                        # recording/persisting never calls a handler


# --- signature -> vector class ---------------------------------------------------------------------------------

def test_every_detector_signature_has_a_vector_class_entry():
    assert {s.name for s in SIGNATURES} == set(SIGNATURE_VECTORS)
    assert vector_class_of("network_attempt_confined") == "network"
    assert vector_class_of("grant_denied_generic") == "uncategorized"
    assert vector_class_of(None) == "uncategorized"


# --- GET /api/redblue/summary ----------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, temp_db, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setattr(journal_mod, "DEFAULT_PATH", tmp_path / "redblue" / "incidents.jsonl")
    from bot.dashboard.server import build_app

    return TestClient(build_app())


_AUTH = {"X-Dashboard-Token": "test-token"}


def test_summary_needs_the_dashboard_token(client):
    assert client.get("/api/redblue/summary").status_code == 401
    assert client.get("/api/redblue/summary", headers={"X-Dashboard-Token": "wrong"}).status_code == 401


def test_summary_of_an_empty_or_missing_journal(client, tmp_path):
    r = client.get("/api/redblue/summary", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0 and body["recent"] == [] and body["proposed_containments"] == []
    assert body["by_verdict"] == {} and body["by_vector_class"] == {} and body["last_ts"] is None
    assert body["findings"] == {"total": 0, "by_vector_class": {}, "breached": 0}
    assert body["dry_run"] is True
    assert not (tmp_path / "redblue").exists()                # reading never creates the journal


def test_summary_shape_counts_and_recent_order(client, tmp_path):
    j = IncidentJournal(tmp_path / "redblue" / "incidents.jsonl")
    wd = WatchDog(journal=j)
    for ev in (_SSH, _NET, _DENY):
        wd.observe_event(ev)
    j.record(Finding(vector_class=VectorClass.NETWORK, hypothesis="h", repro="r", predicted_blue_signal="s",
                     breached=True))
    before = (tmp_path / "redblue" / "incidents.jsonl").read_text(encoding="utf-8")

    body = client.get("/api/redblue/summary", params={"limit": 2}, headers=_AUTH).json()
    assert body["total"] == 3 and body["escapes"] == 2 and body["last_ts"] == 12.0
    assert body["by_verdict"] == {"escape": 2, "suspicious": 1}
    assert body["by_vector_class"] == {"filesystem": 1, "network": 1, "uncategorized": 1}
    assert [i["timestamp"] for i in body["recent"]] == [12.0, 11.0]          # newest first, capped at limit
    assert body["recent"][1]["vector_class"] == "network"
    assert body["findings"] == {"total": 1, "by_vector_class": {"network": 1}, "breached": 1}
    # _DENY proposes "none", so only the network incident in the window has a proposal
    assert [(p["signature"], p["action"]) for p in body["proposed_containments"]] == \
        [("network_attempt_confined", "kill_cell")]
    assert (tmp_path / "redblue" / "incidents.jsonl").read_text(encoding="utf-8") == before   # journal untouched


def test_summary_limit_is_bounded(client):
    assert client.get("/api/redblue/summary", params={"limit": 0}, headers=_AUTH).status_code == 422
    assert client.get("/api/redblue/summary", params={"limit": 201}, headers=_AUTH).status_code == 422


def test_proposed_containments_are_dry_run_and_nothing_live_executes(client, tmp_path, monkeypatch):
    j = IncidentJournal(tmp_path / "redblue" / "incidents.jsonl")
    wd = WatchDog(journal=j)
    for ev in (_SSH, _NET):
        wd.observe_event(ev)
    seen_executors = []
    real_init = ContainmentExecutor.__init__

    def spy_init(self, *a, **kw):
        real_init(self, *a, **kw)
        seen_executors.append(self)

    monkeypatch.setattr(ContainmentExecutor, "__init__", spy_init)
    body = client.get("/api/redblue/summary", headers=_AUTH).json()
    assert body["proposed_containments"]
    for p in body["proposed_containments"]:
        assert p["executed"] is False and p["outcome"] == f"dry-run:{p['action']}"
    assert seen_executors and all(e.dry_run and not e.sinks for e in seen_executors)


def test_the_redblue_routes_are_get_only(client):
    methods = {m for r in client.app.routes if getattr(r, "path", "").startswith("/api/redblue")
               for m in getattr(r, "methods", ())}
    assert methods and methods <= {"GET", "HEAD"}
