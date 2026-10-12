"""The stuck-work check: each kind of warning, only for real trouble, and rate limited. The board is a LocalBoard on a
moving clock; alerts go to a list instead of the Sentinel journal."""
from __future__ import annotations

import asyncio

import pytest

from bot import coordination
from bot.config import config
from bot.coordination import LocalBoard, watch
from bot.coordination.watch import StuckWatch, watch_settings
from coordination_fakes import FakeClock


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock(start_ms=10_000_000)
        self.board = LocalBoard(tmp_path / "w.db", clock=self.clock, min_lease_s=0.01)
        self.sent: list[tuple[str, str, int]] = []
        self.watch = StuckWatch(emit=lambda k, m, b: self.sent.append((k, m, b)), clock=lambda: self.clock.ms / 1000.0,
                                alert_every_s=1800, idle_grace_s=60)

    def kinds(self):
        return [k for k, _m, _b in self.sent]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.board.close()


def test_a_healthy_board_raises_nothing(rig):
    t, _ = rig.board.create_task("abp/x", "fine", key="a")
    rig.board.claim(t["id"], "w1", lease_s=120)
    rig.board.presence("w1", "busy", task=t["id"])
    assert rig.watch.run_once(rig.board) == [] and rig.sent == []


def test_a_lease_about_to_expire_is_reported(rig):
    t, _ = rig.board.create_task("abp/x", "slow job", key="a")
    rig.board.claim(t["id"], "w1", lease_s=20)
    rig.clock.advance(10)                                           # 10 s left, no heartbeat
    [msg] = rig.watch.run_once(rig.board)
    assert "slow job" in msg and "w1" in msg and rig.kinds() == ["lease"]


def test_a_task_that_failed_after_all_attempts_is_reported_once(rig):
    t, _ = rig.board.create_task("abp/x", "doomed", key="a", max_attempts=2)
    for i in range(2):
        rig.board.claim(t["id"], f"w{i}", lease_s=1)
        rig.clock.advance(5)
    assert rig.board.get_task(t["id"])["state"] == "failed"
    [msg] = rig.watch.run_once(rig.board)
    assert "doomed" in msg and "lease expired" in msg and rig.kinds() == ["failed"]
    rig.clock.advance(3600)                                         # well past the rate limit: still just once
    assert rig.watch.run_once(rig.board) == []


def test_a_failure_with_attempts_left_is_not_stuck(rig):
    t, _ = rig.board.create_task("abp/x", "reported failure", key="a", max_attempts=3)
    c = rig.board.claim(t["id"], "w1", lease_s=60)
    rig.board.set_state(t["id"], "w1", c["fence"], "failed", note="it said no")
    assert rig.board.get_task(t["id"])["attempts"] < 3
    assert rig.watch.run_once(rig.board) == []


def test_runnable_work_next_to_idle_agents_is_reported_after_a_grace_period(rig):
    rig.board.create_task("abp/x", "nobody takes me", key="a")
    rig.board.presence("idle-bot", "idle")
    assert rig.watch.run_once(rig.board) == []                      # just noticed: workers pause between steps
    rig.clock.advance(30)
    rig.board.presence("idle-bot", "idle")
    assert rig.watch.run_once(rig.board) == []
    rig.clock.advance(40)
    rig.board.presence("idle-bot", "idle")
    [msg] = rig.watch.run_once(rig.board)
    assert "idle-bot" in msg and "1 runnable" in msg and rig.kinds() == ["idle"]


def test_work_blocked_by_deps_is_not_idle_trouble(rig):
    a, _ = rig.board.create_task("abp/x", "first", key="a")
    rig.board.create_task("abp/x", "second", key="b", deps=[a["id"]])
    rig.board.claim(a["id"], "w1", lease_s=600)
    rig.board.presence("w1", "busy", task=a["id"])
    rig.board.presence("other", "idle")
    rig.clock.advance(300)
    rig.board.presence("other", "idle")
    rig.board.presence("w1", "busy", task=a["id"])
    assert rig.watch.run_once(rig.board) == []


def test_the_idle_timer_resets_when_the_work_is_taken(rig):
    t, _ = rig.board.create_task("abp/x", "late pickup", key="a")
    rig.board.presence("idle-bot", "idle")
    rig.watch.run_once(rig.board)
    rig.clock.advance(50)
    claim = rig.board.claim(t["id"], "idle-bot", lease_s=600)
    rig.watch.run_once(rig.board)                                    # nothing runnable now: timer forgotten
    rig.board.set_state(t["id"], "idle-bot", claim["fence"], "released")
    rig.clock.advance(50)
    rig.board.presence("idle-bot", "idle")
    assert rig.watch.run_once(rig.board) == []                       # a fresh 50 s, not 100


def test_locks_of_a_gone_agent_are_reported(rig):
    rig.board.acquire("ghost", [{"resource": "path:abp:bot/db.py"}], lease_s=3600)
    rig.board.presence("ghost", "busy")
    rig.clock.advance(60)                                            # > 3 x 15 s since it was last seen
    [msg] = rig.watch.run_once(rig.board)
    assert "path:abp:bot/db.py" in msg and "ghost" in msg and rig.kinds() == ["locks"]


def test_the_same_condition_is_reported_once_per_period(rig):
    rig.board.acquire("ghost", [{"resource": "port:9000"}], lease_s=36000)
    rig.board.presence("ghost", "busy")
    rig.clock.advance(60)
    assert len(rig.watch.run_once(rig.board)) == 1
    rig.clock.advance(600)
    assert rig.watch.run_once(rig.board) == []                       # inside 30 minutes
    rig.clock.advance(1300)
    assert len(rig.watch.run_once(rig.board)) == 1                   # a new period
    assert len(rig.sent) == 2 and rig.sent[0][2] != rig.sent[1][2]   # a new journal bucket, so the journal does not swallow it


def test_the_watch_never_changes_the_board(rig):
    t, _ = rig.board.create_task("abp/x", "job", key="a")
    rig.board.claim(t["id"], "w1", lease_s=20)
    rig.clock.advance(10)
    before = rig.board.get_task(t["id"])
    rig.watch.run_once(rig.board)
    assert rig.board.get_task(t["id"]) == before


def test_the_off_switch_and_intervals_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "_data", {"coordination": {"stuck_watch": False, "stuck_watch_interval_s": 120, "stuck_alert_every_s": 60}})
    assert watch_settings() == {"enabled": False, "interval_s": 120.0, "alert_every_s": 60.0}
    monkeypatch.setattr(config, "_data", {})
    assert watch_settings() == {"enabled": True, "interval_s": 60.0, "alert_every_s": 1800.0}


def test_run_forever_checks_nothing_when_switched_off_and_stops_on_the_event(monkeypatch, tmp_path):
    board = LocalBoard(tmp_path / "off.db")
    coordination.set_board(board)
    t, _ = board.create_task("abp/x", "job", key="a", max_attempts=1)
    board.claim(t["id"], "w", lease_s=5)
    monkeypatch.setattr(config, "_data", {"coordination": {"stuck_watch": False}})
    emitted = []
    monkeypatch.setattr(watch, "_journal_emit", lambda *a: emitted.append(a))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.ensure_future(watch.run_forever(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert emitted == []
    coordination.set_board(None)
    board.close()
