"""Containment executor: dry-run by default, reversible-allowlist enforced, wired sinks, WatchDog integration."""
from bot.redblue.containment import ContainmentExecutor
from bot.redblue.events import Event
from bot.redblue.schemas import Incident
from bot.redblue.watchdog import WatchDog


def inc(action="kill_cell"):
    return Incident(timestamp=1.0, detector="watchdog", actor="pid:5", action="x", target="y",
                    verdict="escape", matched_signature="s", containment_action=action)


def test_dry_run_records_but_does_not_execute():
    ex = ContainmentExecutor()                       # dry_run=True default
    r = ex.execute(inc("kill_cell"))
    assert r.executed is False and r.outcome == "dry-run:kill_cell"
    assert len(ex.audit) == 1 and ex.audit[0].to_dict()["action"] == "kill_cell"


def test_non_reversible_action_is_refused():
    ex = ContainmentExecutor(dry_run=False, sinks={"rm_rf": lambda i: "boom"})
    r = ex.execute(inc("rm_rf"))
    assert r.executed is False and r.outcome.startswith("refused:not-reversible")


def test_live_wired_sink_executes():
    killed = []
    ex = ContainmentExecutor(dry_run=False, sinks={"kill_cell": lambda i: killed.append(i.actor) or "KILLED"})
    r = ex.execute(inc("kill_cell"))
    assert r.executed is True and r.outcome == "KILLED" and killed == ["pid:5"]


def test_live_but_unwired_is_not_executed():
    ex = ContainmentExecutor(dry_run=False)          # no sinks
    assert ex.execute(inc("quarantine")).outcome == "unwired:quarantine"


def test_failing_sink_is_surfaced():
    def boom(_i):
        raise RuntimeError("down")
    ex = ContainmentExecutor(dry_run=False, sinks={"kill_cell": boom})
    assert ex.execute(inc("kill_cell")).outcome.startswith("failed:kill_cell:")


def test_watchdog_integration_dry_run_by_default():
    ex = ContainmentExecutor()
    wd = WatchDog(handlers=ex.as_watchdog_handlers())
    i = wd.observe_event(Event(ts=1.0, actor="pid:9", action="socket.connect", target="1.2.3.4:80"))
    assert i is not None and i.containment_action == "kill_cell"
    assert wd.contain(i) == "dry-run:kill_cell"       # routed through the executor, nothing live happened
    assert len(ex.audit) == 1
