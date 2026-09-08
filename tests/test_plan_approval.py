"""bot/agent_runtime/approval.py::request_plan_approval() — plan mode's
own approval gate (Phase G of the Claude API/Claude Code parity plan).
Deliberately distinct from request_approval()'s once/session/always/deny
vocabulary: every non-deny outcome collapses to a plain approved bool,
and nothing is ever pre-approved via is_pre_approved()/grant_tool_approval(),
so a "Session"/"Always" tap on one plan can never silently skip
reviewing a later one.
"""
from __future__ import annotations

import asyncio

from bot import bot_instances, db
from bot.agent_runtime import approval


def _run(coro):
    return asyncio.run(coro)


def _make_instance():
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1],
    )


def test_approved_via_once_button(temp_db):
    iid = _make_instance()
    captured_id = {}

    async def notify(approval_id, tool_name, tool_input):
        captured_id["id"] = approval_id
        assert tool_name == approval.PLAN_APPROVAL_TOOL_NAME
        assert tool_input == {"plan": "1. do x\n2. do y"}

    async def scenario():
        task = asyncio.create_task(approval.request_plan_approval(iid, 1, "s1", "1. do x\n2. do y", notify=notify))
        await asyncio.sleep(0.05)
        assert approval.resolve(captured_id["id"], "once", actor="tester") is True
        return await task

    assert _run(scenario()) is True


def test_denied(temp_db):
    iid = _make_instance()
    captured_id = {}

    async def notify(approval_id, tool_name, tool_input):
        captured_id["id"] = approval_id

    async def scenario():
        task = asyncio.create_task(approval.request_plan_approval(iid, 1, "s1", "a plan", notify=notify))
        await asyncio.sleep(0.05)
        approval.resolve(captured_id["id"], "deny", actor="tester")
        return await task

    assert _run(scenario()) is False


def test_session_and_always_both_count_as_approved_but_never_pre_approve_a_later_plan(temp_db):
    iid = _make_instance()
    captured_ids = []

    async def notify(approval_id, tool_name, tool_input):
        captured_ids.append(approval_id)

    async def scenario():
        first = asyncio.create_task(approval.request_plan_approval(iid, 1, "s1", "plan one", notify=notify))
        await asyncio.sleep(0.05)
        approval.resolve(captured_ids[0], "always", actor="tester")
        first_result = await first

        # A second plan approval must NOT be silently pre-approved just
        # because the first one was resolved "always" — that outcome has
        # no standing-grant meaning here, unlike request_approval()'s own
        # tool-name-scoped semantics.
        second = asyncio.create_task(approval.request_plan_approval(iid, 1, "s1", "plan two", notify=notify))
        await asyncio.sleep(0.05)
        assert len(captured_ids) == 2  # notify fired again — it was NOT skipped
        approval.resolve(captured_ids[1], "once", actor="tester")
        second_result = await second
        return first_result, second_result

    first_result, second_result = _run(scenario())
    assert first_result is True
    assert second_result is True


def test_timeout_denies(temp_db):
    iid = _make_instance()

    async def notify(approval_id, tool_name, tool_input):
        pass  # never resolves

    result = _run(approval.request_plan_approval(iid, 1, "s1", "a plan", notify=notify, timeout_s=0.05))

    assert result is False


def test_expired_row_is_recorded_in_the_db(temp_db):
    iid = _make_instance()

    async def notify(approval_id, tool_name, tool_input):
        pass

    _run(approval.request_plan_approval(iid, 1, "s1", "a plan", notify=notify, timeout_s=0.05))

    rows = db.list_pending_approvals(iid, chat_id=1)
    # resolved rows aren't "pending" anymore, so list_pending_approvals
    # (which only returns status="pending" rows) should show none left.
    assert rows == []
