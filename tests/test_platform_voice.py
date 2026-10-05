"""Voice on Discord and Slack, through the real adapters and the real speech pipeline.

Nothing is mocked at the boundary that matters: a local HTTP server speaks the endpoints the
adapters call (each platform's private file URL, fetched with the bot's own token, plus an
OpenAI-compatible /v1/audio/transcriptions and /v1/audio/speech), and the audio is a real WAV
written with the stdlib `wave` module. The assertions are on the bytes that actually crossed the
socket and on the text that actually reached the router.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import shutil
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bot import voice
from bot.backends.base import BackendResult
from bot.platforms import discord_platform, slack_platform

TRANSCRIPT = "what time is it"


def wav_bytes(seconds: float = 0.2, rate: int = 8000, freq: float = 440.0) -> bytes:
    """A real, playable WAV — a short sine tone — written with the stdlib wave module."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(rate * seconds))
        ))
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    """The platforms' file endpoints and the speech engine's, over a real socket."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep pytest's output readable
        pass

    def _reply(self, status: int, body: bytes = b"", ctype: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> bytes:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.service.auth.append((self.path, self.headers.get("Authorization", "")))
        return body

    def do_GET(self):
        svc = self.server.service
        self._record()
        if self.path.startswith("/files/"):        # Discord's attachment CDN url
            self._reply(200, svc.wav, "audio/wav")
        elif self.path == "/private-download":     # Slack's url_private_download
            self._reply(200, svc.wav, "audio/wav")
        elif self.path == "/forbidden":
            self._reply(403, b"file_not_found")
        else:
            self._reply(404, b"no_such_route")

    def do_POST(self):
        svc = self.server.service
        body = self._record()
        if self.path == "/v1/audio/transcriptions":
            svc.transcribed.append(body)
            self._reply(200, json.dumps({"text": svc.transcript}).encode(), "application/json")
        elif self.path == "/v1/audio/speech":
            svc.spoken.append(json.loads(body or b"{}"))
            self._reply(200, svc.reply_audio, "audio/wav")
        else:
            self._reply(404, b"no_such_route")


@pytest.fixture
def service():
    """A real local HTTP server standing in for the file endpoints and the speech engine."""
    svc = SimpleNamespace(
        wav=wav_bytes(), reply_audio=wav_bytes(0.1, freq=660.0), transcript=TRANSCRIPT,
        auth=[], transcribed=[], spoken=[],
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.service = svc
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    svc.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield svc
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def vcfg(monkeypatch):
    """The `voice:` block, without touching the real config/backends.yaml."""
    values: dict = {}
    monkeypatch.setattr(voice, "_cfg", lambda: values)
    return values


@pytest.fixture(autouse=True)
def _clean_allowance():
    """A speech-to-text provider's usage counters are process-wide; forget them between tests."""
    from bot.agent_runtime import usage_limits

    usage_limits._blocked_until.clear()
    usage_limits._reported.clear()
    yield
    usage_limits._blocked_until.clear()
    usage_limits._reported.clear()


@pytest.fixture
def router(monkeypatch, temp_db, tmp_path):
    """Records what each platform routed to the backend, and keeps stored files out of the real data/."""
    from bot import attachments

    asked: list[tuple[str, dict]] = []

    async def ask(text, **kw):
        asked.append((text, kw))
        return BackendResult(text=f"echo: {text}")

    r = SimpleNamespace(calls=asked, ask=ask)
    monkeypatch.setattr(discord_platform, "router", r)
    monkeypatch.setattr(slack_platform, "router", r)
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", tmp_path / "attachments")

    async def no_push(*a, **k):
        return None

    monkeypatch.setattr(discord_platform.push, "notify_new_message", no_push)
    monkeypatch.setattr(slack_platform.push, "notify_new_message", no_push)
    return r


def _speech_config(vcfg, service, **extra):
    """Speech to text and text to speech, both pointed at the local stand-in."""
    vcfg["stt"] = {"engine": "openai_compatible", "base_url": f"{service.base}/v1", "model": "whisper-large-v3"}
    vcfg["tts"] = {"engine": "openai_compatible", "base_url": f"{service.base}/v1", "model": "tts-1", "voice": "alloy", "format": "wav"}
    vcfg.update(extra)


# ------------------------------------------------------------- discord --

class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Channel:
    def __init__(self):
        self.id = 555
        self.sent: list[str] = []
        self.files: list[tuple[str, bytes]] = []

    async def send(self, text=None, *, file=None):
        if file is not None:
            self.files.append((file.filename, file.fp.read()))
        else:
            self.sent.append(text)

    def typing(self):
        return _Typing()


def _dvoice(service, *, filename="note.wav", mime="audio/wav", caption="", author_id=1):
    att = SimpleNamespace(filename=filename, content_type=mime, size=len(service.wav),
                          url=f"{service.base}/files/{filename}")
    return SimpleNamespace(content=caption, attachments=[att], channel=_Channel(),
                           author=SimpleNamespace(id=author_id, bot=False, __str__=lambda self: f"user{author_id}"))


def _discord():
    inst = discord_platform.DiscordPlatformInstance(7, "dbot", "discord-token", {1})
    return inst, inst._build_client()


def test_a_discord_voice_message_is_fetched_transcribed_then_asked(router, service, vcfg):
    """The whole turn over real HTTP: the attachment comes off Discord's CDN with the bot's own token,
    the configured engine transcribes it, and the transcript reaches the backend as ordinary text."""
    _speech_config(vcfg, service)
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert [text for text, _ in router.calls] == [TRANSCRIPT]
    assert msg.channel.sent == [f"Heard: {TRANSCRIPT}", f"echo: {TRANSCRIPT}"]
    assert router.calls[0][1]["instance_id"] == 7


def test_discord_sends_the_real_audio_with_the_right_name_to_the_engine(router, service, vcfg):
    """The bytes fetched are the WAV itself, offered under the attachment's own name and the bot's token."""
    _speech_config(vcfg, service)
    _inst, client = _discord()
    asyncio.run(client.on_message(_dvoice(service, filename="voice-note.ogg", mime="audio/ogg")))
    assert [(path, auth) for path, auth in service.auth if path.startswith("/files/")] == [
        ("/files/voice-note.ogg", "Bot discord-token"),
    ]
    body = service.transcribed[0]
    assert b'filename="voice-note.ogg"' in body and service.wav in body and b"whisper-large-v3" in body


def test_discord_answers_by_voice_when_voice_replies_are_on(router, service, vcfg):
    """voice.reply_with_voice: the same text-to-speech Telegram speaks, sent back as an audio file."""
    _speech_config(vcfg, service, reply_with_voice=True)
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert msg.channel.files == [("reply.wav", service.reply_audio)]
    assert service.spoken[0]["input"] == f"echo: {TRANSCRIPT}"


def test_discord_stays_text_only_when_voice_replies_are_off(router, service, vcfg):
    _speech_config(vcfg, service, reply_with_voice=False)
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert msg.channel.files == [] and service.spoken == []


def test_discord_never_sends_audio_to_an_engine_nobody_configured(router, service, vcfg):
    """No speech-to-text engine: the same words Telegram gives, and the audio stays on Discord."""
    vcfg["stt"] = {"engine": "off"}
    vcfg["tts"] = {"engine": "openai_compatible", "base_url": f"{service.base}/v1", "format": "wav"}
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert msg.channel.sent == [voice.NO_STT]
    assert service.auth == [] and service.transcribed == [] and router.calls == []


def test_discord_ignores_a_voice_message_over_the_size_limit(router, service, vcfg):
    """Past voice.max_seconds the recording is not even fetched and nothing is transcribed."""
    from bot import db

    _speech_config(vcfg, service, max_seconds=0.1)  # a 800-byte ceiling; the WAV is ~3 kB
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert msg.channel.sent == [voice.TOO_LONG]
    assert service.auth == [] and service.transcribed == [] and router.calls == []
    assert db.get_conn().execute("SELECT COUNT(*) FROM audit_log WHERE action='voice_message'").fetchone()[0] == 0


def test_the_size_limit_holds_even_when_discord_understates_the_file(router, service, vcfg):
    """Discord's own size is only a hint; the ceiling is enforced while the bytes stream in too."""
    _speech_config(vcfg, service, max_seconds=0.1)
    _inst, client = _discord()
    msg = _dvoice(service)
    msg.attachments[0].size = 1
    asyncio.run(client.on_message(msg))
    assert msg.channel.sent == [voice.NO_DOWNLOAD] and service.transcribed == []


def test_a_discord_attachment_the_bot_cannot_download_is_reported(router, service, vcfg):
    _speech_config(vcfg, service)
    _inst, client = _discord()
    msg = _dvoice(service)
    msg.attachments[0].url = f"{service.base}/forbidden"
    asyncio.run(client.on_message(msg))
    assert msg.channel.sent == [voice.NO_DOWNLOAD] and service.transcribed == [] and router.calls == []


def test_a_failed_transcription_is_reported_and_the_backend_is_left_alone(router, service, vcfg):
    _speech_config(vcfg, service)
    service.transcript = ""
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert "no speech was recognised" in msg.channel.sent[0] and router.calls == []


def test_a_spoken_slash_command_is_answered_like_a_typed_one(router, service, vcfg):
    """Command dispatch happens before the router, and a command's own reply is never spoken back."""
    _speech_config(vcfg, service, reply_with_voice=True)
    service.transcript = "/whoami"
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert router.calls == [] and msg.channel.files == [] and service.spoken == []
    assert msg.channel.sent[1] != f"echo: {service.transcript}"


def test_a_non_audio_discord_attachment_is_still_just_a_file(router, service, vcfg):
    """A PDF keeps the existing behaviour: stored, logged, never sent to a speech engine."""
    _speech_config(vcfg, service)
    from bot import db

    _inst, client = _discord()
    msg = _dvoice(service, filename="report.pdf", mime="application/pdf")
    msg.attachments[0].size = 9

    async def read():
        return bytearray(b"%PDF-1.4\n")

    msg.attachments[0].read = read
    asyncio.run(client.on_message(msg))
    assert router.calls == [] and service.transcribed == []
    rows = db.get_conn().execute(
        "SELECT attachment_name FROM messages WHERE platform='discord' AND direction='in'").fetchall()
    assert [r["attachment_name"] for r in rows] == ["report.pdf"]


def test_a_discord_voice_message_is_still_gated_by_the_allow_list(router, service, vcfg):
    _speech_config(vcfg, service)
    _inst, client = _discord()
    msg = _dvoice(service, author_id=99)
    asyncio.run(client.on_message(msg))
    assert msg.channel.sent == [] and service.auth == [] and router.calls == []


def test_a_transcribed_voice_message_is_audited(router, service, vcfg):
    from bot import db

    _speech_config(vcfg, service)
    _inst, client = _discord()
    asyncio.run(client.on_message(_dvoice(service)))
    row = db.get_conn().execute("SELECT actor, detail FROM audit_log WHERE action='voice_message'").fetchone()
    assert row["actor"] == "1" and f"{len(service.wav)} bytes" in row["detail"]


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="Windows speech is only on Windows")
def test_windows_speech_can_answer_a_discord_voice_message(router, service, vcfg):
    """Windows' own speech — the engine the Telegram voice tests use — producing the real WAV that
    goes back to Discord."""
    vcfg["stt"] = {"engine": "openai_compatible", "base_url": f"{service.base}/v1", "model": "whisper-large-v3"}
    vcfg["tts"] = {"engine": "sapi"}
    vcfg["reply_with_voice"] = True
    _inst, client = _discord()
    msg = _dvoice(service)
    asyncio.run(client.on_message(msg))
    assert len(msg.channel.files) == 1 and msg.channel.files[0][0] == "reply.wav"
    audio = msg.channel.files[0][1]
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE" and len(audio) > 2000


# --------------------------------------------------------------- slack --

class _SlackClient:
    """Stands in for slack_bolt's WebClient and records what was uploaded."""

    def __init__(self):
        self.uploads: list[tuple[str, str, bytes]] = []

    async def files_upload_v2(self, **kw):
        kw["file"].seek(0)
        self.uploads.append((kw.get("channel"), kw.get("filename"), kw["file"].read()))
        return {"ok": True}


def _slack_voice_event(service, *, name="voice-message", filetype="m4a", mimetype=None, size=None,
                       caption="", url=None):
    f = {"name": name, "filetype": filetype, "url_private_download": url or f"{service.base}/private-download"}
    if mimetype:
        f["mimetype"] = mimetype
    if size is not None:
        f["size"] = size
    return {"user": "U1", "channel": "C1", "channel_type": "im", "subtype": "file_share",
            "text": caption, "files": [f]}


def _run_slack(monkeypatch, event, instance_id=8):
    """Drive the real Slack event handler, with only slack_bolt's App object replaced. Returns what was
    said plus the WebClient the reply would have been uploaded through."""
    inst = slack_platform.SlackPlatformInstance(instance_id, "sbot", "xoxb-slack-token", "xapp-unused", {"U1"})
    handlers: dict = {}

    class FakeApp:
        def __init__(self, token):
            self.client = _SlackClient()

        def event(self, name):
            def deco(fn):
                handlers[name] = fn
                return fn
            return deco

    import slack_bolt.app.async_app as async_app

    monkeypatch.setattr(async_app, "AsyncApp", FakeApp)
    app = inst._build_app()
    said: list[str] = []

    async def say(text):
        said.append(text)

    asyncio.run(handlers["message"](event=event, say=say, client=app.client))
    return said, app.client


def test_a_slack_voice_message_is_fetched_transcribed_then_asked(router, service, vcfg, monkeypatch):
    """Same turn as Discord: fetched from url_private with the bot token, transcribed, asked as text."""
    _speech_config(vcfg, service)
    said, _client = _run_slack(monkeypatch, _slack_voice_event(service))
    assert [text for text, _ in router.calls] == [TRANSCRIPT]
    assert said == [f"Heard: {TRANSCRIPT}", f"echo: {TRANSCRIPT}"]
    assert router.calls[0][1]["instance_id"] == 8


def test_slack_fetches_the_file_with_the_bot_token_and_the_real_wav(router, service, vcfg, monkeypatch):
    _speech_config(vcfg, service)
    _run_slack(monkeypatch, _slack_voice_event(service, mimetype="audio/mp4"))
    assert [(path, auth) for path, auth in service.auth if path == "/private-download"] == [
        ("/private-download", "Bearer xoxb-slack-token"),
    ]
    # Slack sends a voice message as .m4a with no suffix on the name; the filetype supplies it, so a
    # command engine that keys off the extension still gets a usable one.
    body = service.transcribed[0]
    assert b'filename="voice-message.m4a"' in body and service.wav in body


def test_slack_falls_back_to_url_private_when_the_download_link_answers_nothing(router, service, vcfg, monkeypatch):
    _speech_config(vcfg, service)
    event = _slack_voice_event(service, url=f"{service.base}/forbidden")
    event["files"][0].pop("url_private_download")
    event["files"][0]["url_private"] = f"{service.base}/private-download"
    said, _client = _run_slack(monkeypatch, event)
    assert said[0] == f"Heard: {TRANSCRIPT}"


def test_slack_answers_by_voice_when_voice_replies_are_on(router, service, vcfg, monkeypatch):
    """voice.reply_with_voice: the spoken answer is uploaded as a file, after the text reply."""
    _speech_config(vcfg, service, reply_with_voice=True)
    _said, client = _run_slack(monkeypatch, _slack_voice_event(service))
    assert client.uploads == [("C1", "reply.wav", service.reply_audio)]
    assert service.spoken[0]["input"] == f"echo: {TRANSCRIPT}"


def test_slack_never_sends_audio_to_an_engine_nobody_configured(router, service, vcfg, monkeypatch):
    vcfg["stt"] = {"engine": "off"}
    vcfg["tts"] = {"engine": "openai_compatible", "base_url": f"{service.base}/v1", "format": "wav"}
    said, client = _run_slack(monkeypatch, _slack_voice_event(service))
    assert said == [voice.NO_STT]
    assert service.auth == [] and service.transcribed == [] and router.calls == []
    assert client.uploads == []


def test_slack_ignores_a_voice_message_over_the_size_limit(router, service, vcfg, monkeypatch):
    _speech_config(vcfg, service, max_seconds=0.1)  # a 800-byte ceiling; the WAV is ~3 kB
    said, client = _run_slack(monkeypatch, _slack_voice_event(service, size=10_000_000))
    assert said == [voice.TOO_LONG]
    assert service.auth == [] and service.transcribed == [] and router.calls == []
    assert client.uploads == []


def test_the_slack_size_limit_holds_even_when_slack_understates_the_file(router, service, vcfg, monkeypatch):
    _speech_config(vcfg, service, max_seconds=0.1)
    said, _client = _run_slack(monkeypatch, _slack_voice_event(service, size=1))
    assert said == [voice.NO_DOWNLOAD] and service.transcribed == []


def test_a_slack_file_the_bot_cannot_download_is_reported(router, service, vcfg, monkeypatch):
    _speech_config(vcfg, service)
    said, _client = _run_slack(monkeypatch, _slack_voice_event(service, url=f"{service.base}/forbidden"))
    assert said == [voice.NO_DOWNLOAD] and service.transcribed == [] and router.calls == []


def test_a_slack_voice_message_is_still_gated_by_the_allow_list(router, service, vcfg, monkeypatch):
    _speech_config(vcfg, service)
    event = _slack_voice_event(service)
    event["user"] = "U2"
    said, _client = _run_slack(monkeypatch, event)
    assert said == [] and service.auth == [] and router.calls == []


def test_a_slack_non_audio_file_is_still_just_an_attachment(router, service, vcfg, monkeypatch):
    """A PDF keeps the existing behaviour: stored and logged, never sent to a speech engine."""
    from bot import db

    _speech_config(vcfg, service)
    said, _client = _run_slack(monkeypatch, _slack_voice_event(service, name="quarterly.pdf", filetype="pdf",
                                                               mimetype="application/pdf"))
    assert said == [] and service.transcribed == [] and router.calls == []
    rows = db.get_conn().execute(
        "SELECT attachment_name FROM messages WHERE platform='slack' AND direction='in'").fetchall()
    assert [r["attachment_name"] for r in rows] == ["quarterly.pdf"]


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="Windows speech is only on Windows")
def test_windows_speech_can_answer_a_slack_voice_message(router, service, vcfg, monkeypatch):
    vcfg["stt"] = {"engine": "openai_compatible", "base_url": f"{service.base}/v1", "model": "whisper-large-v3"}
    vcfg["tts"] = {"engine": "sapi"}
    vcfg["reply_with_voice"] = True
    _said, client = _run_slack(monkeypatch, _slack_voice_event(service))
    assert len(client.uploads) == 1 and client.uploads[0][1] == "reply.wav"
    audio = client.uploads[0][2]
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE" and len(audio) > 2000


def test_a_signed_url_is_never_written_to_the_log(router, service, vcfg, caplog):
    """Discord's attachment urls are signed; a failure log keeps the query string out of the log file."""
    import logging

    _speech_config(vcfg, service)
    _inst, client = _discord()
    msg = _dvoice(service)
    msg.attachments[0].url = f"{service.base}/forbidden?ex=SIGNATURE&is=USER-ID"
    with caplog.at_level(logging.WARNING, logger="bot.platforms.voice"):
        asyncio.run(client.on_message(msg))
    assert msg.channel.sent == [voice.NO_DOWNLOAD]
    assert "SIGNATURE" not in caplog.text and "USER-ID" not in caplog.text
    assert "/forbidden" in caplog.text


# ------------------------------------------------ the shared plumbing --

@pytest.mark.parametrize("filename,mimetype,expected", [
    ("note.wav", "", True),                       # Discord voice note, no type
    ("voice.ogg", "audio/ogg", True),             # Telegram-shaped name on another platform
    ("memo", "audio/mp4", True),                  # Slack voice message: no suffix, type only
    ("photo.png", "image/png", False),
    ("report.pdf", "application/pdf", False),
    ("archive", "", False),
])
def test_what_counts_as_speech(filename, mimetype, expected):
    assert voice.is_audio(filename, mimetype) is expected


@pytest.mark.parametrize("filename,mimetype,expected", [
    ("note.wav", "", "note.wav"),
    ("memo", "audio/mp4", "memo.m4a"),             # what Slack calls a voice message
    ("memo", "audio/ogg", "memo.ogg"),
    ("/tmp/abp/memo", "audio/ogg", "memo.ogg"),    # a path from a command engine, not a bare name
    ("", "audio/wav", "voice.wav"),                # Discord sometimes supplies no name at all
    ("memo", "", "memo"),                          # nothing to go on: the name is left alone
    ("report.pdf", "", "report.pdf"),              # not speech: keep the name as it is
])
def test_the_name_the_engine_is_given_keeps_a_usable_suffix(filename, mimetype, expected):
    assert voice.audio_name(filename, mimetype) == expected


def test_a_slack_voice_message_with_no_mimetype_still_arrives_as_audio(router, service, vcfg, monkeypatch):
    """Slack's file object often carries no mimetype at all; the filetype supplies the name's suffix and
    the engine is still handed a content type it can act on."""
    _speech_config(vcfg, service)
    _run_slack(monkeypatch, _slack_voice_event(service))
    body = service.transcribed[0]
    assert b'filename="voice-message.m4a"' in body and b"Content-Type: audio/mp4" in body
