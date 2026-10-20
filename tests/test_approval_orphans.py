"""An approval nobody can answer any more (its run was cancelled, ended, or its process died) is marked expired instead of
sitting in every pending list for good."""
from __future__ import annotations

import asyncio

from bot import approvals_view, db
from bot.agent_runtime import approval


def _pending():
    return [a["id"] for a in approvals_view.listing(status="pending")]


def test_a_cancelled_run_leaves_no_pending_approval(temp_db):
    seen = {}

    async def notify(aid, tool, inp):
        seen["id"] = aid

    async def go():
        task = asyncio.create_task(approval.request_approval(1, "c", "s", "run_shell", {"command": "ls"}, notify,
                                                             timeout_s=60))
        while "id" not in seen:
            await asyncio.sleep(0.01)
        assert seen["id"] in _pending()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert seen["id"] not in _pending()
    assert db.get_pending_approval(seen["id"])["status"] == "expired"


def test_an_orphan_is_expired_when_someone_answers_it_and_at_startup(temp_db):
    a = db.create_pending_approval(1, "c", "s", "run_shell", {"command": "ls"})     # no run waits on these
    b = db.create_pending_approval(1, "c", "s", "run_shell", {"command": "dir"})
    assert approvals_view.decide(a, "deny", actor="t") == "already_resolved"
    assert db.get_pending_approval(a)["status"] == "expired"
    assert approval.expire_orphans() == 1 and _pending() == []
    assert db.get_pending_approval(b)["status"] == "expired"


def test_startup_keeps_an_approval_a_run_still_waits_on(temp_db):
    live = db.create_pending_approval(1, "c", "s", "run_shell", {"command": "ls"})
    dead = db.create_pending_approval(1, "c", "s", "run_shell", {"command": "dir"})
    approval._waiters[live] = asyncio.Event()
    try:
        assert approval.expire_orphans() == 1
        assert _pending() == [live] and db.get_pending_approval(dead)["status"] == "expired"
    finally:
        approval._waiters.pop(live, None)
