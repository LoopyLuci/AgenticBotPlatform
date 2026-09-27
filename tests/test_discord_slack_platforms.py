"""The Discord and Slack adapters' message handling, through their real event
handlers with fake platform objects: authorization, commands, replies,
chunking, failures, and attachment limits."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.backends.base import BackendError, BackendResult
from bot.platforms import discord_platform, slack_platform


class _Router:
    def __init__(self):
        self.behaviour = lambda text: BackendResult(text=f"echo: {text}")
        self.calls = []

    async def ask(self, text, **kw):
        self.calls.append((text, kw))
        out = self.behaviour(text)
        if isinstance(out, BaseException):
            raise out
        return out


@pytest.fixture
def router(monkeypatch, temp_db):
    r = _Router()
    monkeypatch.setattr(discord_platform, "router", r)
    monkeypatch.setattr(slack_platform, "router", r)

    async def no_push(*a, **k):
        return None

    monkeypatch.setattr(discord_platform.push, "notify_new_message", no_push)
    return r


# ------------------------------------------------------------ discord --

class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Channel:
    def __init__(self):
        self.id = 555
        self.sent = []

    async def send(self, text):
        self.sent.append(text)

    def typing(self):
        return _Typing()


def _dmsg(text, author_id=1, bot=False):
    return SimpleNamespace(content=text, attachments=[], channel=_Channel(),
                           author=SimpleNamespace(id=author_id, bot=bot, __str__=lambda self: f"user{author_id}"))


def _discord():
    inst = discord_platform.DiscordPlatformInstance(7, "dbot", "unused", {1})
    return inst, inst._build_client()


def test_discord_answers_an_allowed_user(router):
    _inst, client = _discord()
    msg = _dmsg("hello")
    asyncio.run(client.on_message(msg))
    assert msg.channel.sent == ["echo: hello"]
    assert router.calls[0][1]["instance_id"] == 7


def test_discord_ignores_bots_and_rejects_strangers(router):
    _inst, client = _discord()
    for msg in (_dmsg("hi", bot=True), _dmsg("hi", author_id=99)):
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent == []
    assert router.calls == []
    from bot import db

    assert db.get_conn().execute("SELECT COUNT(*) FROM audit_log WHERE action='unauthorized_attempt'").fetchone()[0] == 1


def test_discord_splits_long_replies_at_the_limit(router):
    _inst, client = _discord()
    router.behaviour = lambda t: BackendResult(text="x" * 4500)
    msg = _dmsg("long please")
    asyncio.run(client.on_message(msg))
    assert [len(s) for s in msg.channel.sent] == [2000, 2000, 500]


@pytest.mark.parametrize("exc,fragment", [(BackendError("quota"), "Backend failed: quota"),
                                          (RuntimeError("bug"), "Something went wrong")])
def test_discord_always_replies_even_when_the_backend_breaks(router, exc, fragment):
    _inst, client = _discord()
    router.behaviour = lambda t: exc
    msg = _dmsg("hi")
    asyncio.run(client.on_message(msg))
    assert fragment in msg.channel.sent[0]


def test_discord_slash_commands_do_not_reach_the_backend(router):
    _inst, client = _discord()
    msg = _dmsg("/whoami")
    asyncio.run(client.on_message(msg))
    assert router.calls == [] and msg.channel.sent


# ------------------------------------------------------------ slack --

def _slack():
    inst = slack_platform.SlackPlatformInstance(8, "sbot", "xoxb-unused", "xapp-unused", {"U1"})
    handler = {}

    class FakeApp:
        def __init__(self, token):
            pass

        def event(self, name):
            def deco(fn):
                handler[name] = fn
                return fn
            return deco

    import slack_bolt.app.async_app as async_app

    return inst, handler, FakeApp, async_app


def _run_slack(monkeypatch, event):
    inst, handler, FakeApp, async_app = _slack()
    monkeypatch.setattr(async_app, "AsyncApp", FakeApp)
    inst._build_app()
    said = []

    async def say(text):
        said.append(text)

    asyncio.run(handler["message"](event=event, say=say))
    return said


def test_slack_answers_an_allowed_user(router, monkeypatch):
    said = _run_slack(monkeypatch, {"user": "U1", "text": "hi", "channel": "C1"})
    assert said == ["echo: hi"]


@pytest.mark.parametrize("event", [
    {"user": "U2", "text": "hi", "channel": "C1"},
    {"bot_id": "B1", "user": "U1", "text": "hi", "channel": "C1"},
    {"subtype": "message_changed", "user": "U1", "text": "hi", "channel": "C1"},
])
def test_slack_ignores_strangers_bots_and_edits(router, monkeypatch, event):
    assert _run_slack(monkeypatch, event) == []
    assert router.calls == []


def test_slack_always_replies_even_when_the_backend_breaks(router, monkeypatch):
    router.behaviour = lambda t: ValueError("bug")
    said = _run_slack(monkeypatch, {"user": "U1", "text": "hi", "channel": "C1"})
    assert "Something went wrong" in said[0]


class _Stream:
    def __init__(self, status, chunks):
        self.status_code, self._chunks = status, chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _Client:
    def __init__(self, status=200, chunks=(b"ab", b"cd")):
        self.status, self.chunks = status, chunks

    def stream(self, method, url, headers):
        assert headers["Authorization"].startswith("Bearer ")
        return _Stream(self.status, self.chunks)


def test_slack_file_download_is_capped(monkeypatch):
    dl = slack_platform._download_capped
    assert asyncio.run(dl(_Client(), "https://files", "t")) == b"abcd"
    assert asyncio.run(dl(_Client(status=403), "https://files", "t")) is None
    assert asyncio.run(dl(_Client(), "", "t")) is None
    monkeypatch.setattr(slack_platform, "MAX_FILE_BYTES", 3)
    assert asyncio.run(dl(_Client(), "https://files", "t")) is None


# ------------------------------------------------------------ access tiers on every platform --
# Regression: slash-command tiers were enforced only on Telegram. With an admin
# list configured, a non-admin on Discord/Slack/Matrix/WhatsApp could run any
# command (e.g. /backend, /restart) and chat freely.

def _gated_instance(platform, admin, allowed_cmds=None):
    from bot import bot_instances

    # Bypass the token-format validators rather than supply token-shaped strings
    # (see test_platform_supervisor_self_healing.py for why).
    for validators in bot_instances.PLATFORM_TOKEN_VALIDATORS.values():
        for key in list(validators):
            validators[key] = lambda v: (True, "ok")
    creds = {"telegram": {"bot_token": "unused"}, "discord": {"bot_token": "unused"},
             "slack": {"bot_token": "unused", "app_token": "unused"}}[platform]
    overrides = {"slash_access": {"dm_user_commands": allowed_cmds}} if allowed_cmds is not None else {}
    return bot_instances.create_instance(name=f"g-{platform}", platform=platform, backend="api", credentials=creds,
                                         allowed_user_ids=[admin, "U2" if platform == "slack" else 2],
                                         admin_user_ids=[admin], action_overrides=overrides)


@pytest.fixture(autouse=True)
def _restore_validators():
    from bot import bot_instances

    saved = {p: dict(v) for p, v in bot_instances.PLATFORM_TOKEN_VALIDATORS.items()}
    yield
    for p, v in saved.items():
        bot_instances.PLATFORM_TOKEN_VALIDATORS[p].clear()
        bot_instances.PLATFORM_TOKEN_VALIDATORS[p].update(v)


def test_discord_non_admin_cannot_run_admin_commands_or_chat(router):
    iid = _gated_instance("discord", 1)
    inst = discord_platform.DiscordPlatformInstance(iid, "g", "unused", {1, 2})
    client = inst._build_client()
    msg = _dmsg("/backend api", author_id=2)
    asyncio.run(client.on_message(msg))
    assert "not authorized to run /backend" in msg.channel.sent[0]
    assert router.calls == []
    chat = _dmsg("hello", author_id=2)  # plain chat is not tier-gated, same as Telegram
    asyncio.run(client.on_message(chat))
    assert chat.channel.sent == ["echo: hello"]
    msg = _dmsg("/whoami", author_id=2)
    asyncio.run(client.on_message(msg))
    assert "Tier: user" in msg.channel.sent[0]
    admin = _dmsg("hello", author_id=1)
    asyncio.run(client.on_message(admin))
    assert admin.channel.sent == ["echo: hello"]


def test_slack_non_admin_is_gated_by_their_real_user_id(router, monkeypatch):
    iid = _gated_instance("slack", "U1", allowed_cmds=["status"])
    inst, handler, FakeApp, async_app = _slack()
    inst.instance_id, inst.allowed_ids = iid, {"U1", "U2"}
    monkeypatch.setattr(async_app, "AsyncApp", FakeApp)
    inst._build_app()
    said = []

    async def say(text):
        said.append(text)

    asyncio.run(handler["message"](event={"user": "U2", "text": "/backend api", "channel": "D1", "channel_type": "im"}, say=say))
    asyncio.run(handler["message"](event={"user": "U2", "text": "/status", "channel": "D1", "channel_type": "im"}, say=say))
    assert "not authorized to run /backend" in said[0]
    assert "not authorized" not in said[1]  # /status is on this user's list
    assert router.calls == []


def test_operator_surfaces_are_not_gated(temp_db):
    from bot import commands

    iid = _gated_instance("telegram", 1)
    ctx = commands.CmdContext(instance_id=iid, instance_name="g", user_id="terminal", chat_id="terminal", actor="terminal")
    assert commands.access_denied(ctx, "backend") is None
