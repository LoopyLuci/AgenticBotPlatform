"""The Telegram adapter's entry points (bot/handlers.py) with fake Update and
Context objects: authorization, logging, chunked replies, reactions, command
dispatch and slash-command tiers."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import commands, db, handlers


class _Msg:
    def __init__(self, text, chat):
        self.text, self.caption, self.chat = text, None, chat
        self.chat_id, self.message_id = chat.id, 1
        self.replies = []
        self.message_thread_id = None

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(edit_text=_noop, delete=_noop)


async def _noop(*a, **k):
    return None


class _Chat:
    def __init__(self, cid=100, kind="private"):
        self.id, self.type = cid, kind

    async def send_action(self, *a, **k):
        return None


class _Bot:
    def __init__(self):
        self.reactions, self.sent = [], []

    async def set_message_reaction(self, **kw):
        self.reactions.append(kw["reaction"])

    async def send_message(self, **kw):
        self.sent.append(kw)


def _update(text, user_id=1, chat_kind="private"):
    chat = _Chat(kind=chat_kind)
    msg = _Msg(text, chat)
    user = SimpleNamespace(id=user_id, username=f"u{user_id}", first_name="U")
    return SimpleNamespace(effective_user=user, effective_chat=chat, message=msg, effective_message=msg)


def _context(instance_id, allowed=(1,)):
    return SimpleNamespace(bot=_Bot(), args=[], user_data={},
                           bot_data={"allowed_ids": set(allowed), "instance_id": instance_id, "instance_name": "tg"})


@pytest.fixture
def tg(temp_db, monkeypatch):
    from bot import bot_instances

    monkeypatch.setitem(bot_instances.PLATFORM_TOKEN_VALIDATORS["telegram"], "bot_token", lambda v: (True, "ok"))

    async def no_push(*a, **k):
        return None

    monkeypatch.setattr(handlers.push, "notify_new_message", no_push)
    asked = []

    async def fake_ask(ctx, raw):
        asked.append(raw)
        return fake_ask.reply(raw)

    fake_ask.reply = lambda raw: f"echo: {raw}"
    monkeypatch.setattr(commands, "cmd_ask", fake_ask)

    def make(**kw):
        return bot_instances.create_instance(name="tg", platform="telegram", backend="api",
                                             credentials={"bot_token": "unused"}, allowed_user_ids=[1, 2], **kw)
    return SimpleNamespace(make=make, asked=asked, ask=fake_ask)


def run(coro):
    return asyncio.run(coro)


def test_allowed_text_is_logged_answered_and_reacted_to(tg):
    iid = tg.make()
    up, ctx = _update("hello"), _context(iid)
    run(handlers.on_text(up, ctx))
    assert up.message.replies == ["echo: hello"]
    rows = db.get_conn().execute("SELECT direction, text FROM messages ORDER BY id").fetchall()
    assert [(r["direction"], r["text"]) for r in rows] == [("in", "hello"), ("out", "echo: hello")]
    assert len(ctx.bot.reactions) == 2  # 👀 while working, then 👍


def test_a_stranger_is_rejected_and_audited(tg):
    iid = tg.make()
    up = _update("hello", user_id=99)
    run(handlers.on_text(up, _context(iid)))
    assert up.message.replies == [] and tg.asked == []
    assert db.get_conn().execute("SELECT COUNT(*) FROM audit_log WHERE action='unauthorized_attempt'").fetchone()[0] == 1


def test_long_replies_are_split_at_telegrams_limit(tg):
    iid = tg.make()
    tg.ask.reply = lambda raw: "y" * (handlers.TELEGRAM_MAX_LEN * 2 + 10)
    up = _update("long")
    run(handlers.on_text(up, _context(iid)))
    assert [len(r) for r in up.message.replies] == [handlers.TELEGRAM_MAX_LEN, handlers.TELEGRAM_MAX_LEN, 10]


def test_a_backend_failure_gets_a_thumbs_down(tg):
    iid = tg.make()
    tg.ask.reply = lambda raw: "Backend failed: quota"
    up, ctx = _update("hi"), _context(iid)
    run(handlers.on_text(up, ctx))
    assert up.message.replies == ["Backend failed: quota"]
    assert "👎" in str(ctx.bot.reactions[-1])


def test_unknown_commands_say_so(tg):
    iid = tg.make()
    up = _update("/definitely_not_a_command")
    run(handlers.on_command(up, _context(iid)))
    assert up.message.replies[0].startswith("Unknown command: /definitely_not_a_command")


def test_command_with_bot_suffix_resolves(tg):
    iid = tg.make()
    up = _update("/whoami@SomeBot")
    run(handlers.on_command(up, _context(iid)))
    assert "Tier: unrestricted" in up.message.replies[0]


def test_tiers_block_non_admins_but_not_admins(tg):
    iid = tg.make(admin_user_ids=[1])
    up = _update("/backend api", user_id=2)
    run(handlers.on_command(up, _context(iid, allowed=(1, 2))))
    assert "not authorized to run /backend" in up.message.replies[0]
    up = _update("/whoami", user_id=2)
    run(handlers.on_command(up, _context(iid, allowed=(1, 2))))
    assert "Tier: user" in up.message.replies[0]
    up = _update("/whoami", user_id=1)
    run(handlers.on_command(up, _context(iid, allowed=(1, 2))))
    assert "Tier: admin" in up.message.replies[0]


def test_group_scope_is_detected():
    assert handlers._scope_of(_update("x", chat_kind="supergroup")) == "group"
    assert handlers._scope_of(_update("x")) == "dm"
