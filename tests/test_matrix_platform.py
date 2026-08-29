"""bot/platforms/matrix_platform.py — the Matrix chat adapter's message/
invite/media handling. Exercises real matrix-nio event objects (built via
their own from_dict(), the same construction path a real /sync response
uses) against a fake AsyncClient stub, not a live homeserver — the actual
network sync loop (sync_forever) isn't something a unit test should be
driving, matching this project's existing "Discord/Slack gateway
connections aren't unit-tested either" precedent. What's tested here is
everything this module actually decides on its own: allowlist
enforcement, ignoring its own messages and pre-connect backlog, command
dispatch vs. router.ask, and invite auto-join.
"""

from __future__ import annotations

import asyncio

import pytest
from nio import InviteMemberEvent, MatrixRoom, RoomMessageText

from bot import attachments, db
from bot.platforms.matrix_platform import MatrixPlatformInstance


def _run(coro):
    return asyncio.run(coro)


def _room(room_id: str = "!room:example.org") -> MatrixRoom:
    return MatrixRoom(room_id=room_id, own_user_id="@bot:example.org")


def _message_event(sender: str, body: str, ts: int = 1_000_000) -> RoomMessageText:
    return RoomMessageText.from_dict({
        "event_id": "$1", "sender": sender, "origin_server_ts": ts,
        "type": "m.room.message", "content": {"msgtype": "m.text", "body": body},
    })


def _invite_event(sender: str, state_key: str, membership: str = "invite") -> InviteMemberEvent:
    return InviteMemberEvent.from_dict({
        "event_id": "$2", "sender": sender, "origin_server_ts": 1_000_000,
        "type": "m.room.member", "state_key": state_key,
        "content": {"membership": membership}, "prev_content": {"membership": "leave"},
    })


class _FakeClient:
    def __init__(self):
        self.sent: list[dict] = []
        self.joined: list[str] = []

    async def room_send(self, room_id, message_type, content, **kwargs):
        self.sent.append({"room_id": room_id, "content": content})

    async def join(self, room_id):
        self.joined.append(room_id)
        from nio import JoinResponse

        return JoinResponse(room_id=room_id)


def _instance(allowed=("@alice:example.org",), started_at_ms=0) -> MatrixPlatformInstance:
    inst = MatrixPlatformInstance(
        instance_id=1, name="matrix-test", homeserver="https://example.org",
        user_id="@bot:example.org", access_token="tok", device_id="",
        allowed_ids=set(allowed),
    )
    inst._client = _FakeClient()
    inst._started_at_ms = started_at_ms
    return inst


def test_allowed_message_dispatches_to_router(monkeypatch, temp_db):
    inst = _instance()

    async def fake_ask(text, **kwargs):
        class R:
            pass

        r = R()
        r.text = f"you said: {text}"
        return r

    monkeypatch.setattr("bot.platforms.matrix_platform.router.ask", fake_ask)
    event = _message_event("@alice:example.org", "hello there")
    _run(inst._on_message(_room(), event))
    assert inst._client.sent[-1]["content"]["body"] == "you said: hello there"


def test_unauthorized_sender_is_rejected(temp_db):
    inst = _instance(allowed=("@alice:example.org",))
    event = _message_event("@mallory:evil.org", "let me in")
    _run(inst._on_message(_room(), event))
    assert inst._client.sent == []
    audits = db.get_conn().execute(
        "SELECT * FROM audit_log WHERE action='unauthorized_attempt'"
    ).fetchall()
    assert len(audits) == 1
    assert "mallory" in audits[0]["detail"]


def test_own_messages_are_ignored(temp_db):
    inst = _instance()
    event = _message_event("@bot:example.org", "echo of myself")
    _run(inst._on_message(_room(), event))
    assert inst._client.sent == []


def test_backlog_before_connect_is_ignored(temp_db):
    inst = _instance(started_at_ms=2_000_000)
    event = _message_event("@alice:example.org", "old message", ts=1_000_000)
    _run(inst._on_message(_room(), event))
    assert inst._client.sent == []


def test_command_is_dispatched_instead_of_router(monkeypatch, temp_db):
    inst = _instance()

    async def fail_ask(*a, **k):
        raise AssertionError("router.ask should not be called for a slash command")

    monkeypatch.setattr("bot.platforms.matrix_platform.router.ask", fail_ask)
    event = _message_event("@alice:example.org", "/help")
    _run(inst._on_message(_room(), event))
    assert inst._client.sent, "expected a reply from the command handler"


def test_invite_for_this_bot_is_accepted(temp_db):
    inst = _instance()
    event = _invite_event(sender="@alice:example.org", state_key="@bot:example.org")
    _run(inst._on_invite(_room(), event))
    assert inst._client.joined == ["!room:example.org"]


def test_invite_for_someone_else_is_ignored(temp_db):
    inst = _instance()
    event = _invite_event(sender="@alice:example.org", state_key="@someone-else:example.org")
    _run(inst._on_invite(_room(), event))
    assert inst._client.joined == []


def test_media_from_unauthorized_sender_is_ignored(monkeypatch, tmp_path, temp_db):
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", attach_dir)
    inst = _instance(allowed=("@alice:example.org",))

    class _Event:
        sender = "@mallory:evil.org"
        server_timestamp = 1_000_000
        url = "mxc://example.org/abc"
        body = "photo.png"

    _run(inst._on_media(_room(), _Event()))
    assert list(attach_dir.iterdir()) == []


def test_media_download_and_log(monkeypatch, tmp_path, temp_db):
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir()
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", attach_dir)
    inst = _instance()

    class _DownloadResp:
        body = b"fake-png-bytes"
        content_type = "image/png"
        filename = "photo.png"

    async def fake_download(mxc, filename):
        return _DownloadResp()

    inst._client.download = fake_download

    class _Event:
        sender = "@alice:example.org"
        server_timestamp = 1_000_000
        url = "mxc://example.org/abc"
        body = "photo.png"

    _run(inst._on_media(_room(), _Event()))
    rows = db.get_conn().execute(
        "SELECT * FROM messages WHERE platform='matrix' AND attachment_name='photo.png'"
    ).fetchall()
    assert len(rows) == 1
