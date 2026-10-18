import json

import pytest

from bot.redblue.events import Event, EventFeed, normalize
from bot.redblue.schemas import (FINDING_SCHEMA, INCIDENT_SCHEMA, Finding, Incident, SchemaError,
                                 VectorClass)
from bot.redblue.verifier import Verifier
from bot.sandbox_ns.registry import Registry


def _finding():
    return Finding(VectorClass.FILESYSTEM, "h", "touch x", "sig", severity=7.5, novelty=0.4, confidence=0.9)


def test_finding_roundtrip():
    f = _finding()
    d = json.loads(json.dumps(f.to_dict()))
    assert Finding.from_dict(d) == f
    assert d["vector_class"] == "filesystem"
    assert set(FINDING_SCHEMA["required"]) <= set(d)


def test_incident_roundtrip():
    i = Incident(1.0, "det", "a", "open", "/x", grant_result="denied", verdict="escape", matched_signature="s")
    d = json.loads(json.dumps(i.to_dict()))
    assert Incident.from_dict(d) == i
    assert set(INCIDENT_SCHEMA["required"]) <= set(d)


@pytest.mark.parametrize("mut", [
    {"vector_class": "nope"}, {"severity": 11}, {"confidence": -1}, {"found_by": "x"},
    {"breached": "yes"}, {"extra": 1}, {"hypothesis": None},
])
def test_finding_rejects_malformed(mut):
    d = {**_finding().to_dict(), **mut}
    with pytest.raises(SchemaError):
        Finding.from_dict(d)


def test_malformed_rejected_missing_and_nondict():
    d = _finding().to_dict()
    del d["repro"]
    with pytest.raises(SchemaError):
        Finding.from_dict(d)
    with pytest.raises(SchemaError):
        Incident.from_dict([])
    with pytest.raises(SchemaError):
        Incident.from_dict({"timestamp": 1, "detector": "d", "actor": "a", "action": "x", "target": "t",
                            "verdict": "bad"})


def test_verifier_detects_touch(tmp_path):
    v = Verifier(tmp_path / "c")
    f = _finding()

    def run(repro, canary):
        (canary.root / "pwned.txt").write_text("x")
        return ""
    assert v.verify(f, run) is True
    assert f.breached and "created" in f.observed_side_effect


def test_verifier_detects_modify_and_exfil(tmp_path):
    v = Verifier(tmp_path / "c")

    def mod(repro, canary):
        next(iter(map(__import__("pathlib").Path, canary.files))).write_text("changed")
    assert v.verify(_finding(), mod) is True

    def leak(repro, canary):
        return "got " + next(iter(canary.secrets.values()))
    assert v.verify(_finding(), leak) is True


def test_verifier_untouched_is_false(tmp_path):
    assert Verifier(tmp_path / "c").verify(_finding(), lambda r, c: "nothing") is False


def test_verifier_ignores_false_claim(tmp_path):
    out = "SUCCESS: escaped the sandbox, wrote to host, canary modified"
    assert Verifier(tmp_path / "c").verify(_finding(), lambda r, c: out) is False


def test_verifier_network_sink(tmp_path):
    import socket
    v = Verifier(tmp_path / "c", with_sink=True)

    def run(repro, canary):
        socket.create_connection(("127.0.0.1", canary.sink.port), timeout=2).close()
        import time
        time.sleep(0.3)
    assert v.verify(_finding(), run) is True


def test_normalize_sandbox_ns_event():
    reg = Registry(path=None)
    row = reg.event("spawn", pid=42, cell="c1", detail="python x.py")
    e = normalize(row, "sandbox_ns")
    assert (e.actor, e.action, e.target, e.cell_id) == ("pid:42", "spawn", "python x.py", "c1")
    assert e.ts == row["ts"] and e.resource == {"pid": 42}


def test_normalize_tool_call():
    e = normalize({"agent": "bot1", "tool": "read_file", "args": {"path": "/etc/x"}, "ts": 5, "cell_id": "c"},
                  "tool_call")
    assert (e.actor, e.action, e.target, e.cell_id, e.ts) == ("bot1", "read_file", "/etc/x", "c", 5.0)


def test_normalize_grant_check():
    e = normalize({"actor": "a", "action": "net", "target": "1.2.3.4", "allowed": False, "ts": 1}, "grant_check")
    assert e.grant_result == "denied" and e.target == "1.2.3.4"
    assert normalize({"allowed": True}, "grant_check").grant_result == "allowed"
    with pytest.raises(ValueError):
        normalize({}, "bogus")


def test_event_feed_recent():
    feed = EventFeed()
    for i in range(10):
        feed.add(Event(ts=i, actor="a", action=str(i), target="t"))
    assert [e.action for e in feed.recent(3)] == ["7", "8", "9"]
    assert len(feed.recent(100)) == 10
    assert feed.ingest({"allowed": True, "ts": 20}, "grant_check") is feed.recent(1)[0]
