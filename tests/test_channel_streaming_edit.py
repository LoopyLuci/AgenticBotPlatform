"""Live reply streaming on Matrix, Microsoft Teams and Google Chat (bot/platforms/_stream_edit.py, wired in
matrix_platform.py, teams_platform.py, googlechat_platform.py through _relay.relay). Fake clients only: no network,
no real account. The shared helper itself is tested in test_channel_streaming.py."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bot.agent_runtime.transports.base import StreamEvent
from bot.backends.base import BackendResult
from bot.platforms import _relay, _stream_edit, googlechat_platform as gc, matrix_platform, teams_platform as teams
from bot.platforms._stream_edit import PLACEHOLDER

pytestmark = pytest.mark.usefixtures("temp_db")


def run(coro):
    return asyncio.run(coro)


class _Router:
    def __init__(self):
        self.calls, self.events, self.reply = [], [], "done"

    async def ask(self, text, **kw):
        self.calls.append(kw)
        notify = (kw.get("context") or {}).get("stream_notify")
        for ev in self.events:
            if notify is not None:
                await notify(ev)
        return BackendResult(text=self.reply)


@pytest.fixture
def router(monkeypatch, tmp_path):
    r = _Router()
    monkeypatch.setattr(matrix_platform, "router", r)
    monkeypatch.setattr(_relay, "router", r)

    class NoThrottle(_stream_edit.StreamEditor):
        def __init__(self, *a, **kw):
            kw.setdefault("interval_s", 0)
            super().__init__(*a, **kw)

    monkeypatch.setattr(_stream_edit, "StreamEditor", NoThrottle)
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: None)

    async def no_push(*a, **k):
        return None

    monkeypatch.setattr(matrix_platform.push, "notify_new_message", no_push)
    monkeypatch.setattr(_relay.push, "notify_new_message", no_push)
    monkeypatch.setattr(teams, "_store", lambda: tmp_path / "teams.json")
    return r


EVENTS = [StreamEvent("text", "Hi"), StreamEvent("text", " there")]


# --------------------------------------------------------------------------------------------------- Matrix
class _Sent:
    def __init__(self, event_id):
        self.event_id = event_id


class _MxClient:
    def __init__(self, fail_edit=False):
        self.sent, self.redacted, self.fail_edit = [], [], fail_edit

    async def room_send(self, room_id, message_type, content, **kw):
        if self.fail_edit and "m.new_content" in content:
            return "ErrorResponse: M_LIMIT_EXCEEDED"  # nio returns errors, it does not raise
        self.sent.append(content)
        return _Sent(f"$e{len(self.sent)}")

    async def room_redact(self, room_id, event_id):
        self.redacted.append(event_id)


def _matrix_turn(client):
    from nio import MatrixRoom, RoomMessageText

    inst = matrix_platform.MatrixPlatformInstance(3, "mx", "https://hs.example.org", "@bot:example.org", "tok", "DEV",
                                                  {"@alice:example.org"})
    inst._client = client
    ev = RoomMessageText.from_dict({"event_id": "$1", "sender": "@alice:example.org", "origin_server_ts": 1_000_000,
                                    "type": "m.room.message", "content": {"msgtype": "m.text", "body": "hello"}})
    run(inst._on_message(MatrixRoom(room_id="!r:example.org", own_user_id="@bot:example.org"), ev))


def test_matrix_streams_into_one_message_edited_with_m_replace(router):
    router.events, router.reply = EVENTS, "Hi there!"
    client = _MxClient()
    _matrix_turn(client)
    assert client.sent[0] == {"msgtype": "m.text", "body": PLACEHOLDER}
    edits = client.sent[1:]
    for content in edits:
        assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$e1"}
        assert content["body"] == " * " + content["m.new_content"]["body"]
    texts = [c["m.new_content"]["body"] for c in edits]
    assert texts[0] == "Hi" and texts[-1] == "Hi there!" and len(texts) >= 2
    assert client.redacted == []


def test_matrix_edit_error_redacts_the_draft_and_sends_normally(router):
    router.events, router.reply = EVENTS, "Hi there!"
    client = _MxClient(fail_edit=True)
    _matrix_turn(client)
    assert client.redacted == ["$e1"]
    assert client.sent[-1] == {"msgtype": "m.text", "body": "Hi there!"}


def test_matrix_over_long_reply_falls_back_to_the_chunked_send(router, monkeypatch):
    monkeypatch.setattr(matrix_platform, "MATRIX_MAX_LEN", 20)
    router.events, router.reply = [StreamEvent("text", "x")], "y" * 30
    client = _MxClient()
    _matrix_turn(client)
    assert client.redacted == ["$e1"]
    assert [c["body"] for c in client.sent[-2:]] == ["y" * 20, "y" * 10]


def test_matrix_streaming_off_is_unchanged(router, monkeypatch):
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: {"matrix": False})
    router.events, router.reply = EVENTS, "Hi there!"
    client = _MxClient()
    _matrix_turn(client)
    assert client.sent == [{"msgtype": "m.text", "body": "Hi there!"}] and router.calls[0]["context"] is None


def test_matrix_non_streaming_backend_replies_normally(router):
    router.reply = "plain"
    client = _MxClient()
    _matrix_turn(client)
    assert client.sent == [{"msgtype": "m.text", "body": "plain"}]


# --------------------------------------------------------------------------------------------------- Teams
class _Connector:
    """The Bot Connector's activity endpoints, recording (method, url, body)."""

    def __init__(self, fail_put=False):
        self.calls, self.fail_put = [], fail_put

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, str(request.url), body))
        if request.method == "PUT" and self.fail_put:
            return httpx.Response(429, json={"error": "slow down"})
        if request.method == "POST" and body and body.get("type") == "message":
            return httpx.Response(201, json={"id": "draft-1"})
        return httpx.Response(200, json={"id": "x"})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


TEAMS_URL = "https://smba.example.test/amer/v3/conversations/a:c1/activities"


@pytest.fixture
def teams_token(monkeypatch):
    async def token(instance, client):
        return "tok"

    monkeypatch.setattr(teams, "_token", token)


def _teams_turn(connector):
    instance = {"id": 5, "name": "tm", "credentials": {}, "allowed_user_ids": []}
    act = {"type": "message", "id": "act-1", "serviceUrl": "https://smba.example.test/amer/",
           "from": {"id": "29:a", "aadObjectId": "oid"}, "conversation": {"id": "a:c1"}, "text": "hello"}
    run(teams.deliver(instance, act, client=connector.client()))
    return [c for c in connector.calls if c[2] != {"type": "typing"}]


def test_teams_streams_into_one_activity_updated_with_put(router, teams_token):
    router.events, router.reply = EVENTS, "Hi there!"
    calls = _teams_turn(_Connector())
    assert calls[0] == ("POST", f"{TEAMS_URL}/act-1", {"type": "message", "text": PLACEHOLDER, "textFormat": "markdown"})
    puts = calls[1:]
    assert all(c[0] == "PUT" and c[1] == f"{TEAMS_URL}/draft-1" for c in puts)
    assert puts[0][2]["text"] == "Hi" and puts[-1][2]["text"] == "Hi there!" and len(puts) >= 2


def test_teams_edit_error_deletes_the_draft_and_sends_normally(router, teams_token):
    router.events, router.reply = EVENTS, "Hi there!"
    calls = _teams_turn(_Connector(fail_put=True))
    assert ("DELETE", f"{TEAMS_URL}/draft-1", None) in calls
    assert calls[-1][0] == "POST" and calls[-1][2]["text"] == "Hi there!"


def test_teams_over_long_reply_falls_back_to_the_split_send(router, teams_token):
    router.events, router.reply = [StreamEvent("text", "x")], "y" * (teams.MAX_TEXT + 10)
    calls = _teams_turn(_Connector())
    assert any(c[0] == "DELETE" for c in calls)
    sent = [c[2]["text"] for c in calls if c[0] == "POST" and c[2]["text"] != PLACEHOLDER]
    assert len(sent) == 2 and "".join(sent) == "y" * (teams.MAX_TEXT + 10)


def test_teams_streaming_off_is_unchanged(router, teams_token, monkeypatch):
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: {"teams": False})
    router.events, router.reply = EVENTS, "Hi there!"
    calls = _teams_turn(_Connector())
    assert [(c[0], c[2]["text"]) for c in calls] == [("POST", "Hi there!")] and router.calls[0]["context"] is None


# ----------------------------------------------------------------------------------------------- Google Chat
class _ChatApi:
    def __init__(self, fail_patch=False):
        self.calls, self.fail_patch = [], fail_patch

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, str(request.url), body))
        if request.method == "PATCH" and self.fail_patch:
            return httpx.Response(429, json={})
        if request.method == "POST":
            return httpx.Response(200, json={"name": "spaces/AAA/messages/m1"})
        return httpx.Response(200, json={})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


CHAT = "https://chat.googleapis.com/v1"
THREAD_Q = "?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def _chat_turn(api, monkeypatch):
    class Tokens:
        async def get(self, key, factory):
            return "tok"

    monkeypatch.setattr(gc, "_tokens", Tokens())
    monkeypatch.setattr(gc, "service_account", lambda inst: {"client_email": "bot@example.test"})
    instance = {"id": 6, "name": "gc", "credentials": {}, "allowed_user_ids": []}
    event = {"space": "spaces/AAA", "thread": "spaces/AAA/threads/T1", "email": "a@example.com", "user": "users/1",
             "text": "hello", "display_name": "Alice"}
    run(gc.deliver(instance, event, client=api.client()))
    return api.calls


def test_google_chat_streams_into_one_message_patched_with_update_mask(router, monkeypatch):
    router.events, router.reply = EVENTS, "**Hi** there!"
    calls = _chat_turn(_ChatApi(), monkeypatch)
    assert calls[0] == ("POST", f"{CHAT}/spaces/AAA/messages{THREAD_Q}",
                        {"text": PLACEHOLDER, "thread": {"name": "spaces/AAA/threads/T1"}})
    patches = calls[1:]
    assert all(c[0] == "PATCH" and c[1] == f"{CHAT}/spaces/AAA/messages/m1?updateMask=text" for c in patches)
    assert patches[0][2] == {"text": "Hi"} and patches[-1][2] == {"text": "*Hi* there!"} and len(patches) >= 2


def test_google_chat_edit_error_deletes_the_draft_and_sends_normally(router, monkeypatch):
    router.events, router.reply = EVENTS, "Hi there!"
    calls = _chat_turn(_ChatApi(fail_patch=True), monkeypatch)
    assert ("DELETE", f"{CHAT}/spaces/AAA/messages/m1", None) in calls
    assert calls[-1][0] == "POST" and calls[-1][2]["text"] == "Hi there!"


def test_google_chat_over_long_reply_falls_back_to_the_split_send(router, monkeypatch):
    router.events, router.reply = [StreamEvent("text", "x")], "y" * (gc.MAX_TEXT + 10)
    calls = _chat_turn(_ChatApi(), monkeypatch)
    assert any(c[0] == "DELETE" for c in calls)
    sent = [c[2]["text"] for c in calls if c[0] == "POST" and c[2]["text"] != PLACEHOLDER]
    assert len(sent) == 2 and "".join(sent) == "y" * (gc.MAX_TEXT + 10)


def test_google_chat_streaming_off_is_unchanged(router, monkeypatch):
    monkeypatch.setattr(_stream_edit, "_cfg", lambda: {"googlechat": False})
    router.events, router.reply = EVENTS, "Hi there!"
    calls = _chat_turn(_ChatApi(), monkeypatch)
    assert [(c[0], c[2]["text"]) for c in calls] == [("POST", "Hi there!")] and router.calls[0]["context"] is None


def test_google_chat_non_streaming_backend_replies_normally(router, monkeypatch):
    calls = _chat_turn(_ChatApi(), monkeypatch)  # no stream events: no placeholder, the reply goes out whole
    assert [c[0] for c in calls] == ["POST"] and calls[0][2]["text"] == "done"
