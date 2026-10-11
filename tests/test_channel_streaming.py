"""Live reply streaming for chat channels that can edit a message (bot/platforms/_stream_edit.py): the shared
helper under a fake clock, then the Telegram, Discord and Slack wiring with fake clients."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import commands, db, handlers
from bot.agent_runtime.transports.base import StreamEvent
from bot.backends.base import BackendResult
from bot.platforms import _stream_edit, discord_platform, slack_platform
from bot.platforms._stream_edit import PLACEHOLDER, StreamEditor


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.t += delay


class Fake:
    """A message that records what was sent, edited and deleted."""

    def __init__(self, fail_edit_at=None):
        self.sent, self.edits, self.deleted = 0, [], 0
        self.fail_edit_at = fail_edit_at

    async def send(self):
        self.sent += 1

    async def edit(self, text):
        if self.fail_edit_at is not None and len(self.edits) + 1 >= self.fail_edit_at:
            raise RuntimeError("429 too many requests")
        self.edits.append(text)

    async def delete(self):
        self.deleted += 1


def editor(fake, clock=None, limit=100, interval_s=1.0):
    clock = clock or Clock()
    return StreamEditor(fake.send, fake.edit, limit=limit, delete=fake.delete, interval_s=interval_s,
                        clock=clock, sleep=clock.sleep), clock


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- the helper

def test_first_event_posts_a_placeholder_then_edits_are_throttled():
    async def go():
        fake = Fake()
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("text", "Hel"))
        assert fake.sent == 1 and fake.edits == []
        await ed.notify(StreamEvent("text", "lo"))
        await ed.notify(StreamEvent("text", " there"))
        await settle()
        # Everything in one edit, after waiting out the rest of the interval - not one edit per delta.
        assert fake.edits == ["Hello there"]
        assert clock.sleeps == [1.0]
        await ed.notify(StreamEvent("text", "!"))
        await settle()
        assert fake.edits == ["Hello there", "Hello there!"]
        await ed.finish("")
    run(go())


def test_an_edit_goes_out_at_once_when_the_interval_has_passed():
    async def go():
        fake = Fake()
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("text", "a"))
        clock.t += 5
        await ed.notify(StreamEvent("text", "b"))
        assert fake.edits == ["ab"] and clock.sleeps == []
    run(go())


def test_tool_line_is_the_name_only_and_goes_away_after_the_tool():
    async def go():
        fake = Fake()
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("text", "Looking"))
        clock.t += 2
        await ed.notify(StreamEvent("tool_start", "read_file"))
        assert fake.edits[-1] == "Looking\n\n🔧 read_file…"
        clock.t += 2
        await ed.notify(StreamEvent("tool_end", "read_file"))
        assert fake.edits[-1] == "Looking"
    run(go())


def test_a_tool_before_any_text_posts_the_placeholder():
    async def go():
        fake = Fake()
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("tool_start", "bash"))
        assert fake.sent == 1
        await settle()
        assert fake.edits == ["🔧 bash…"]
    run(go())


def test_reset_clears_the_text():
    async def go():
        fake = Fake()
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("text", "first try"))
        clock.t += 2
        await ed.notify(StreamEvent("text", " more"))
        clock.t += 2
        await ed.notify(StreamEvent("reset"))
        assert fake.edits[-1] == PLACEHOLDER
        clock.t += 2
        await ed.notify(StreamEvent("text", "second"))
        assert fake.edits[-1] == "second"
    run(go())


def test_reset_with_nothing_shown_posts_nothing():
    async def go():
        fake = Fake()
        ed, _ = editor(fake)
        await ed.notify(StreamEvent("reset"))
        assert fake.sent == 0 and not ed.started
        assert await ed.finish("answer") is False
    run(go())


def test_finish_replaces_the_placeholder_so_the_caller_skips_its_send():
    async def go():
        fake = Fake()
        ed, _ = editor(fake)
        await ed.notify(StreamEvent("text", "partial"))
        assert await ed.finish("the full reply") is True
        assert fake.edits[-1] == "the full reply" and fake.sent == 1 and fake.deleted == 0
    run(go())


def test_finish_with_no_stream_events_leaves_the_send_to_the_caller():
    async def go():
        fake = Fake()
        ed, _ = editor(fake)
        assert await ed.finish("reply") is False
        assert fake.sent == 0 and fake.edits == [] and fake.deleted == 0
    run(go())


def test_over_length_text_stops_editing_and_hands_over_to_the_normal_send():
    async def go():
        fake = Fake()
        ed, clock = editor(fake, limit=20)
        await ed.notify(StreamEvent("text", "short"))
        clock.t += 2
        await ed.notify(StreamEvent("text", "x" * 30))
        clock.t += 2
        await ed.notify(StreamEvent("text", "more"))
        assert fake.edits == []  # never edited past the limit
        assert await ed.finish("y" * 30) is False
        assert fake.deleted == 1
    run(go())


def test_a_final_reply_over_the_limit_deletes_the_placeholder_and_falls_back():
    async def go():
        fake = Fake()
        ed, _ = editor(fake, limit=20)
        await ed.notify(StreamEvent("text", "ok"))
        assert await ed.finish("z" * 50) is False
        assert fake.deleted == 1
    run(go())


def test_an_edit_error_stops_streaming_quietly_and_falls_back():
    async def go():
        fake = Fake(fail_edit_at=1)
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("text", "a"))
        clock.t += 2
        await ed.notify(StreamEvent("text", "b"))  # this edit raises
        clock.t += 2
        await ed.notify(StreamEvent("text", "c"))  # ignored from here on
        assert fake.edits == []
        assert await ed.finish("final") is False
        assert fake.deleted == 1
    run(go())


def test_a_placeholder_error_means_no_streaming_at_all():
    async def go():
        async def boom():
            raise RuntimeError("forbidden")

        fake = Fake()
        ed = StreamEditor(boom, fake.edit, limit=100, delete=fake.delete)
        await ed.notify(StreamEvent("text", "x"))
        assert await ed.finish("reply") is False
        assert fake.deleted == 0
    run(go())


def test_a_failing_delete_does_not_raise():
    async def go():
        fake = Fake()

        async def bad_delete():
            raise RuntimeError("gone")

        ed = StreamEditor(fake.send, fake.edit, limit=5, delete=bad_delete)
        await ed.notify(StreamEvent("text", "ab"))
        assert await ed.finish("too long for five") is False
    run(go())


def test_events_after_finish_are_ignored():
    async def go():
        fake = Fake()
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("text", "a"))
        await ed.finish("done")
        edits = list(fake.edits)
        clock.t += 5
        await ed.notify(StreamEvent("text", "late"))
        await settle()
        assert fake.edits == edits and fake.sent == 1
    run(go())


def test_abort_removes_the_placeholder():
    async def go():
        fake = Fake()
        ed, _ = editor(fake)
        await ed.notify(StreamEvent("text", "a"))
        await ed.abort()
        assert fake.deleted == 1
    run(go())


def test_identical_text_is_not_edited_twice():
    async def go():
        fake = Fake()
        ed, clock = editor(fake)
        await ed.notify(StreamEvent("text", "same"))
        clock.t += 2
        await ed.notify(StreamEvent("tool_start", "t"))
        clock.t += 2
        await ed.notify(StreamEvent("tool_end", "t"))
        clock.t += 2
        await ed.notify(StreamEvent("text", ""))
        assert fake.edits == ["same\n\n🔧 t…", "same"]
        assert await ed.finish("same") is True
        assert fake.edits == ["same\n\n🔧 t…", "same"]
    run(go())


def test_streaming_setting_defaults_on_and_can_be_switched_off_per_platform(monkeypatch):
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: None)
    assert all(_stream_edit.streaming_enabled(p) for p in ("telegram", "discord", "slack"))
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: {"discord": False})
    assert _stream_edit.streaming_enabled("telegram") and not _stream_edit.streaming_enabled("discord")


# ------------------------------------------------------------------ Telegram

class _TgMsg:
    def __init__(self, chat):
        self.text, self.caption, self.chat = "hi", None, chat
        self.chat_id, self.message_id, self.message_thread_id = chat.id, 1, None
        self.replies = []
        self.live = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        m = SimpleNamespace(text=text, edits=[], deleted=False)

        async def edit_text(t, **k):
            m.edits.append(t)

        async def delete():
            m.deleted = True

        m.edit_text, m.delete = edit_text, delete
        self.live.append(m)
        return m


class _Chat:
    id, type = 100, "private"

    async def send_action(self, *a, **k):
        return None


class _TgBot:
    async def set_message_reaction(self, **kw):
        return None

    async def send_message(self, **kw):
        return None


@pytest.fixture
def tg(temp_db, monkeypatch):
    from bot import bot_instances

    monkeypatch.setitem(bot_instances.PLATFORM_TOKEN_VALIDATORS["telegram"], "bot_token", lambda v: (True, "ok"))

    async def no_push(*a, **k):
        return None

    monkeypatch.setattr(handlers.push, "notify_new_message", no_push)
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: None)
    iid = bot_instances.create_instance(name="tg", platform="telegram", backend="api",
                                        credentials={"bot_token": "unused"}, allowed_user_ids=[1])
    chat = _Chat()
    msg = _TgMsg(chat)
    user = SimpleNamespace(id=1, username="u1", first_name="U")
    update = SimpleNamespace(effective_user=user, effective_chat=chat, message=msg, effective_message=msg)
    context = SimpleNamespace(bot=_TgBot(), args=[], user_data={},
                              bot_data={"allowed_ids": {1}, "instance_id": iid, "instance_name": "tg"})
    return SimpleNamespace(update=update, context=context, msg=msg)


def _fake_turn(monkeypatch, events, reply):
    seen = {}

    async def fake_ask(ctx, raw):
        seen["notify"] = ctx.stream_notify
        for ev in events:
            if ctx.stream_notify is not None:
                await ctx.stream_notify(ev)
        return reply

    monkeypatch.setattr(commands, "cmd_ask", fake_ask)
    return seen


def test_telegram_streams_into_one_message_and_sends_no_duplicate(tg, monkeypatch):
    _fake_turn(monkeypatch, [StreamEvent("text", "Hello "), StreamEvent("tool_start", "grep"),
                             StreamEvent("tool_end", "grep"), StreamEvent("text", "world")], "Hello world")
    run(handlers.on_text(tg.update, tg.context))
    assert tg.msg.replies == [PLACEHOLDER]  # the only message ever sent
    live = tg.msg.live[0]
    assert live.edits[-1] == "Hello world" and not live.deleted
    rows = db.get_conn().execute("SELECT direction, text FROM messages ORDER BY id").fetchall()
    assert [(r["direction"], r["text"]) for r in rows] == [("in", "hi"), ("out", "Hello world")]


def test_telegram_reply_over_the_limit_falls_back_to_the_chunked_send(tg, monkeypatch):
    big = "q" * (handlers.TELEGRAM_MAX_LEN + 10)
    _fake_turn(monkeypatch, [StreamEvent("text", "start")], big)
    run(handlers.on_text(tg.update, tg.context))
    assert tg.msg.live[0].deleted
    assert tg.msg.replies[1:] == [big[:handlers.TELEGRAM_MAX_LEN], big[handlers.TELEGRAM_MAX_LEN:]]


def test_telegram_without_stream_events_sends_the_reply_as_before(tg, monkeypatch):
    _fake_turn(monkeypatch, [], "plain answer")  # a backend that does not stream
    run(handlers.on_text(tg.update, tg.context))
    assert tg.msg.replies == ["plain answer"]


def test_telegram_streaming_off_hands_the_backend_no_callback(tg, monkeypatch):
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: {"telegram": False})
    seen = _fake_turn(monkeypatch, [StreamEvent("text", "x")], "answer")
    run(handlers.on_text(tg.update, tg.context))
    assert seen["notify"] is None and tg.msg.replies == ["answer"]


# ------------------------------------------------------------------- Discord

class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _DMessage:
    def __init__(self):
        self.content, self.edits, self.deleted = PLACEHOLDER, [], False

    async def edit(self, content):
        self.content = content
        self.edits.append(content)

    async def delete(self):
        self.deleted = True


class _DChannel:
    id = 555

    def __init__(self):
        self.messages = []

    async def send(self, text):
        m = _DMessage()
        m.content = text
        m.sent_text = text
        self.messages.append(m)
        return m

    def typing(self):
        return _Typing()


class _Router:
    def __init__(self):
        self.calls = []
        self.events, self.reply = [], "done"

    async def ask(self, text, **kw):
        self.calls.append(kw)
        notify = (kw.get("context") or {}).get("stream_notify")
        for ev in self.events:
            if notify is not None:
                await notify(ev)
        return BackendResult(text=self.reply)


@pytest.fixture
def router(monkeypatch, temp_db):
    r = _Router()
    monkeypatch.setattr(discord_platform, "router", r)
    monkeypatch.setattr(slack_platform, "router", r)

    async def no_push(*a, **k):
        return None

    monkeypatch.setattr(discord_platform.push, "notify_new_message", no_push)
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: None)
    return r


def _discord_message():
    return SimpleNamespace(content="hello", attachments=[], channel=_DChannel(),
                           author=SimpleNamespace(id=1, bot=False, __str__=lambda self: "user1"))


def _discord_turn():
    client = discord_platform.DiscordPlatformInstance(7, "dbot", "unused", {1})._build_client()
    msg = _discord_message()
    run(client.on_message(msg))
    return msg.channel


def test_discord_streams_into_one_message(router):
    router.events = [StreamEvent("text", "Hi"), StreamEvent("text", " there")]
    router.reply = "Hi there!"
    channel = _discord_turn()
    assert [m.sent_text for m in channel.messages] == [PLACEHOLDER]
    assert channel.messages[0].edits[-1] == "Hi there!" and not channel.messages[0].deleted


def test_discord_long_reply_deletes_the_draft_and_splits_as_before(router):
    router.events = [StreamEvent("text", "start")]
    router.reply = "x" * 4500
    channel = _discord_turn()
    assert channel.messages[0].deleted
    assert [len(m.sent_text) for m in channel.messages[1:]] == [2000, 2000, 500]


def test_discord_non_streaming_backend_replies_normally(router):
    router.reply = "just text"
    channel = _discord_turn()
    assert [m.sent_text for m in channel.messages] == ["just text"]


def test_discord_streaming_off_passes_no_callback(router, monkeypatch):
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: {"discord": False})
    router.events = [StreamEvent("text", "x")]
    router.reply = "answer"
    channel = _discord_turn()
    assert [m.sent_text for m in channel.messages] == ["answer"]
    assert router.calls[0]["context"] is None


# --------------------------------------------------------------------- Slack

class _SlackClient:
    def __init__(self, fail_update=False):
        self.updates, self.deletes, self.fail_update = [], [], fail_update

    async def chat_update(self, channel, ts, text):
        if self.fail_update:
            raise RuntimeError("ratelimited")
        self.updates.append((channel, ts, text))

    async def chat_delete(self, channel, ts):
        self.deletes.append((channel, ts))


def _slack_turn(monkeypatch, client):
    inst = slack_platform.SlackPlatformInstance(8, "sbot", "xoxb-unused", "xapp-unused", {"U1"})
    handler = {}

    class FakeApp:
        def __init__(self, token):
            self.client = client

        def event(self, name):
            def deco(fn):
                handler[name] = fn
                return fn
            return deco

    import slack_bolt.app.async_app as async_app

    monkeypatch.setattr(async_app, "AsyncApp", FakeApp)
    inst._app = inst._build_app()
    said = []

    async def say(text):
        said.append(text)
        return {"ts": f"1.{len(said)}"}

    run(handler["message"](event={"user": "U1", "text": "hi", "channel": "C1"}, say=say))
    return said


def test_slack_streams_into_one_message(router, monkeypatch):
    router.events = [StreamEvent("text", "Hey")]
    router.reply = "Hey you"
    client = _SlackClient()
    said = _slack_turn(monkeypatch, client)
    assert said == [PLACEHOLDER]
    assert client.updates[-1] == ("C1", "1.1", "Hey you") and client.deletes == []


def test_slack_edit_failure_falls_back_to_a_normal_reply(router, monkeypatch):
    router.events = [StreamEvent("text", "Hey")]
    router.reply = "Hey you"
    client = _SlackClient(fail_update=True)
    said = _slack_turn(monkeypatch, client)
    assert said == [PLACEHOLDER, "Hey you"] and client.deletes == [("C1", "1.1")]


def test_slack_streaming_off_is_unchanged(router, monkeypatch):
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: {"slack": False})
    router.events = [StreamEvent("text", "Hey")]
    router.reply = "Hey you"
    said = _slack_turn(monkeypatch, _SlackClient())
    assert said == ["Hey you"] and router.calls[0]["context"] is None
