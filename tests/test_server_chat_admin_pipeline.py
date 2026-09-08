"""bot/server_chat_admin.py — the permanent Server Chat group room
doubling as the channel you talk to BotServer in (Section 3 of the
"Admin control surface" plan). Every test mocks bot.router.router.ask
directly — never a real LLM call.
"""
from __future__ import annotations

import asyncio

from bot import agent_settings, bot_instances, db, router as router_module
from bot import server_chat_admin


def _run(coro):
    return asyncio.run(coro)


def _make_admin_instance():
    instance_id = bot_instances.create_instance(
        name="admin-bot", platform="telegram", backend="native_agent",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1],
    )
    agent_settings.set_settings(instance_id, is_admin_instance=True)
    return instance_id


class _FakeResult:
    def __init__(self, text):
        self.text = text


def test_direct_conversation_never_triggers_the_admin_pipeline(temp_db, monkeypatch):
    called = {"ask": False}
    monkeypatch.setattr(router_module.router, "ask", lambda *a, **kw: called.__setitem__("ask", True))
    _make_admin_instance()
    key_id, _ = db.create_api_key("phone")
    conversation_id = db.ensure_direct_conversation(db.SERVER_CHAT_DESKTOP_DEVICE_ID, key_id)

    _run(server_chat_admin.maybe_handle_group_message(conversation_id, key_id, "engage estop"))

    assert called["ask"] is False


def test_group_message_is_a_noop_when_no_admin_instance_configured(temp_db, monkeypatch):
    called = {"ask": False}
    monkeypatch.setattr(router_module.router, "ask", lambda *a, **kw: called.__setitem__("ask", True))
    group_id = db.ensure_server_chat_group()

    _run(server_chat_admin.maybe_handle_group_message(group_id, db.SERVER_CHAT_DESKTOP_DEVICE_ID, "hello"))

    assert called["ask"] is False


def test_group_message_dispatches_to_the_admin_instance_with_device_tier(temp_db, monkeypatch):
    admin_id = _make_admin_instance()
    group_id = db.ensure_server_chat_group()
    key_id, _ = db.create_api_key("phone", permission_tier="elevated")

    captured = {}

    async def fake_ask(prompt, **kwargs):
        captured.update(kwargs)
        captured["prompt"] = prompt
        return _FakeResult("Here is the status.")

    monkeypatch.setattr(router_module.router, "ask", fake_ask)

    _run(server_chat_admin.maybe_handle_group_message(group_id, key_id, "what's the status"))

    assert captured["instance_id"] == admin_id
    assert captured["context"]["device_tier"] == "elevated"
    assert captured["context"]["desktop_session_key"] == f"serverchat:{group_id}"
    assert callable(captured["context"]["approval_notify"])


def test_admin_reply_is_posted_back_from_the_bot_sentinel(temp_db, monkeypatch):
    _make_admin_instance()
    group_id = db.ensure_server_chat_group()

    async def fake_ask(prompt, **kwargs):
        return _FakeResult("All systems normal.")

    monkeypatch.setattr(router_module.router, "ask", fake_ask)

    _run(server_chat_admin.maybe_handle_group_message(group_id, db.SERVER_CHAT_DESKTOP_DEVICE_ID, "status?"))

    messages = db.get_conn().execute(
        "SELECT * FROM server_chat_messages WHERE conversation_id=? ORDER BY id", (group_id,)
    ).fetchall()
    bot_messages = [m for m in messages if m["sender_device_id"] == db.SERVER_CHAT_BOT_DEVICE_ID]
    assert len(bot_messages) == 1
    assert bot_messages[0]["text"] == "All systems normal."
    assert bot_messages[0]["kind"] == "message"


def test_the_bots_own_reply_never_re_triggers_the_pipeline(temp_db, monkeypatch):
    called = {"ask": False}
    monkeypatch.setattr(router_module.router, "ask", lambda *a, **kw: called.__setitem__("ask", True))
    _make_admin_instance()
    group_id = db.ensure_server_chat_group()

    _run(server_chat_admin.maybe_handle_group_message(group_id, db.SERVER_CHAT_BOT_DEVICE_ID, "All systems normal."))

    assert called["ask"] is False


def test_approval_notify_posts_an_approval_request_message(temp_db, monkeypatch):
    _make_admin_instance()
    group_id = db.ensure_server_chat_group()
    captured_notify = {}

    async def fake_ask(prompt, **kwargs):
        notify = kwargs["context"]["approval_notify"]
        await notify(42, "admin_engage_estop", {"reason": "testing"})
        captured_notify["called"] = True
        return _FakeResult("")

    monkeypatch.setattr(router_module.router, "ask", fake_ask)

    _run(server_chat_admin.maybe_handle_group_message(group_id, db.SERVER_CHAT_DESKTOP_DEVICE_ID, "engage estop"))

    assert captured_notify["called"] is True
    approval_msgs = db.get_conn().execute(
        "SELECT * FROM server_chat_messages WHERE conversation_id=? AND kind='approval_request'", (group_id,)
    ).fetchall()
    assert len(approval_msgs) == 1
    assert approval_msgs[0]["approval_id"] == 42


def test_resolve_device_tier_treats_desktop_as_unrestricted(temp_db):
    assert server_chat_admin.resolve_device_tier(db.SERVER_CHAT_DESKTOP_DEVICE_ID) == "unrestricted"


def test_resolve_device_tier_reads_the_devices_own_tier(temp_db):
    key_id, _ = db.create_api_key("phone", permission_tier="standard")
    assert server_chat_admin.resolve_device_tier(key_id) == "standard"


def test_resolve_device_tier_defaults_to_none_for_unknown_device(temp_db):
    assert server_chat_admin.resolve_device_tier(999999) == "none"


def test_pipeline_failure_never_raises_into_the_caller(temp_db, monkeypatch):
    _make_admin_instance()
    group_id = db.ensure_server_chat_group()

    async def broken_ask(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(router_module.router, "ask", broken_ask)

    # Must not raise.
    _run(server_chat_admin.maybe_handle_group_message(group_id, db.SERVER_CHAT_DESKTOP_DEVICE_ID, "hi"))
