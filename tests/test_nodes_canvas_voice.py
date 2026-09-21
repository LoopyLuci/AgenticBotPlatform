"""Nodes (paired phones as tools), the canvas, and voice - roadmap P7."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bot import canvas, db, nodes, voice
from bot.agent_runtime import taint, tools, toolspec
from bot.dashboard.server import build_app

pytestmark = pytest.mark.usefixtures("temp_db")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_nodes():
    from bot.agent_runtime import usage_limits

    usage_limits._blocked_until.clear()
    usage_limits._reported.clear()
    nodes._queues.clear()
    nodes._waiting.clear()
    nodes._pending.clear()
    nodes._last_seen.clear()
    yield


# ---- nodes: the protocol ---------------------------------------------------------------------------------------------
def test_a_device_registers_only_capabilities_that_exist_and_starts_with_none_allowed():
    kept = nodes.register(7, "Pixel", ["camera.snap", "location.get", "rm.everything"])
    assert kept == ["camera.snap", "location.get"]
    listing = nodes.listing()
    assert listing[0]["capabilities"] == {"camera.snap": "deny", "location.get": "deny"} and listing[0]["online"]
    assert nodes.resolve("pixel") == 7 and nodes.resolve("7") == 7
    with pytest.raises(nodes.NodeError):
        nodes.resolve("nobody")


def test_consent_is_validated_and_audited():
    nodes.register(7, "Pixel", ["camera.snap"])
    nodes.set_consent(7, "camera.snap", "device")
    assert nodes.consent(7, "camera.snap") == "device"
    with pytest.raises(nodes.NodeError, match="unknown capability"):
        nodes.set_consent(7, "root.shell", "allow")
    with pytest.raises(nodes.NodeError, match="mode must be"):
        nodes.set_consent(7, "camera.snap", "always")
    assert db.get_conn().execute("SELECT COUNT(*) c FROM audit_log WHERE action='node_consent'").fetchone()["c"] == 1


def answer_when_polled(device_id, *, ok=True, data=None, error="", capture=None):
    async def device():
        cmds = await nodes.poll(device_id, 5)
        if capture is not None:
            capture.extend(cmds)
        for c in cmds:
            nodes.submit_result(device_id, c["id"], ok, data, error)

    return device()


def test_a_command_reaches_the_device_and_its_answer_comes_back():
    nodes.register(7, "Pixel", ["location.get"])
    nodes.set_consent(7, "location.get", "allow")
    seen = []

    async def scenario():
        device = asyncio.create_task(answer_when_polled(7, data={"lat": 40.7, "lon": -74.0}, capture=seen))
        result = await nodes.invoke(7, "location.get", {"accuracy": "fine"}, timeout_s=5)
        await device
        return result

    assert run(scenario()) == {"lat": 40.7, "lon": -74.0}
    assert seen[0]["capability"] == "location.get" and seen[0]["args"] == {"accuracy": "fine", "consent": "allow"}
    assert db.get_conn().execute("SELECT COUNT(*) c FROM audit_log WHERE action='node_invoke'").fetchone()["c"] == 1


def test_the_device_is_told_when_it_must_ask_its_own_user():
    nodes.register(7, "Pixel", ["camera.snap"])
    nodes.set_consent(7, "camera.snap", "device")
    seen = []

    async def scenario():
        device = asyncio.create_task(answer_when_polled(7, ok=False, error="the user declined", capture=seen))
        with pytest.raises(nodes.NodeError, match="the user declined"):
            await nodes.invoke(7, "camera.snap", timeout_s=5)
        await device

    run(scenario())
    assert seen[0]["args"]["consent"] == "device"


def test_nothing_is_sent_without_consent_or_to_an_offline_or_unsupporting_device():
    nodes.register(7, "Pixel", ["camera.snap"])
    with pytest.raises(nodes.NodeError, match="not allowed on this device"):
        run(nodes.invoke(7, "camera.snap"))
    nodes.set_consent(7, "camera.snap", "allow")
    with pytest.raises(nodes.NodeError, match="does not offer"):
        run(nodes.invoke(7, "location.get"))
    nodes._last_seen[7] = time.time() - 10_000
    with pytest.raises(nodes.NodeError, match="not connected"):
        run(nodes.invoke(7, "camera.snap"))
    assert not nodes._queues.get(7)


def test_a_silent_device_times_out_and_leaves_no_command_behind():
    nodes.register(7, "Pixel", ["location.get"])
    nodes.set_consent(7, "location.get", "allow")
    with pytest.raises(nodes.NodeError, match="did not answer"):
        run(nodes.invoke(7, "location.get", timeout_s=0.2))
    assert not nodes._queues.get(7) and not nodes._pending


def test_only_the_device_that_was_asked_can_answer():
    nodes.register(7, "A", ["location.get"])
    nodes.register(8, "B", ["location.get"])
    nodes.set_consent(7, "location.get", "allow")

    async def scenario():
        task = asyncio.create_task(nodes.invoke(7, "location.get", timeout_s=3))
        await asyncio.sleep(0.1)
        cmd_id = next(iter(nodes._pending))
        assert nodes.submit_result(8, cmd_id, True, {"lat": 0}) is False, "another device's answer is refused"
        assert nodes.submit_result(7, cmd_id, True, {"lat": 1}) is True
        assert nodes.submit_result(7, cmd_id, True, {"lat": 2}) is False, "and a second answer is ignored"
        return await task

    assert run(scenario()) == {"lat": 1}


def test_an_image_is_stored_as_an_attachment_and_text_is_returned_as_text():
    text, info = nodes.store_result({"image_b64": base64.b64encode(b"\x89PNG fake").decode(), "mime": "image/png"})
    assert "saved at" in text and "untrusted" in text and info["path"].endswith(".png")
    with pytest.raises(nodes.NodeError, match="could not be decoded"):
        nodes.store_result({"image_b64": "@@@not base64@@@"})
    assert '"lat": 1' in nodes.store_result({"lat": 1, "consent": "allow"})[0] and "consent" not in nodes.store_result({"lat": 1, "consent": "allow"})[0]


def test_the_agent_tool_taints_the_session_and_reports_refusals_plainly():
    nodes.register(7, "Pixel", ["location.get"])
    assert tools.is_dangerous("node_invoke") and not tools.is_dangerous("node_list")
    with pytest.raises(Exception, match="not allowed on this device"):
        run(tools.execute_tool("node_invoke", {"device": "Pixel", "capability": "location.get"}, workspace=None, instance_id=1))
    nodes.set_consent(7, "location.get", "allow")

    async def scenario():
        token = toolspec.session_var.set("s-node")
        try:
            device = asyncio.create_task(answer_when_polled(7, data={"lat": 1}))
            out = await tools.execute_tool("node_invoke", {"device": "Pixel", "capability": "location.get"}, workspace=None, instance_id=1)
            await device
            return out
        finally:
            toolspec.session_var.reset(token)

    assert '"lat": 1' in run(scenario()) and taint.sources("s-node") == ["node:7"]
    assert json.loads(run(tools.execute_tool("node_list", {}, workspace=None, instance_id=1)))[0]["name"] == "Pixel"


# ---- nodes: over HTTP ---------------------------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


OWNER = {"X-Dashboard-Token": "test-token"}


def test_the_node_http_calls_and_who_may_make_them(client):
    device_id, key = db.create_api_key("phone", permission_tier="standard")
    phone = {"X-Dashboard-Token": key}
    assert client.post("/api/nodes/register", json={"name": "P", "capabilities": ["location.get"]}, headers=OWNER).status_code == 403, "the owner token is not a device"
    assert client.post("/api/nodes/register", json={"name": "P", "capabilities": ["location.get", "x"]}, headers=phone).json()["capabilities"] == ["location.get"]
    assert client.get("/api/nodes/poll?wait=0", headers=phone).json() == {"commands": []}
    assert client.get("/api/nodes/poll?wait=0").status_code == 401
    assert client.put(f"/api/nodes/{device_id}/consent", json={"capability": "location.get", "mode": "allow"}, headers=phone).status_code in (401, 403), "a device cannot grant itself consent"
    assert client.put(f"/api/nodes/{device_id}/consent", json={"capability": "location.get", "mode": "allow"}, headers=OWNER).status_code == 200
    assert client.put(f"/api/nodes/{device_id}/consent", json={"capability": "location.get", "mode": "bogus"}, headers=OWNER).status_code == 400
    assert client.put("/api/nodes/999/consent", json={"capability": "location.get", "mode": "allow"}, headers=OWNER).status_code == 404
    listed = client.get("/api/nodes", headers=OWNER).json()
    assert listed["nodes"][0]["capabilities"] == {"location.get": "allow"} and "camera.snap" in listed["capabilities"]
    assert client.post("/api/nodes/result", json={"id": "nope", "ok": True}, headers=phone).status_code == 404


def test_a_command_travels_through_the_real_http_endpoints(app_server):
    """The agent side invokes; a stand-in phone long-polls and answers over HTTP, as the Android app would."""
    base, owner_key, phone_key, device_id = app_server
    phone = {"X-Dashboard-Token": phone_key}
    with httpx.Client(base_url=base, timeout=30) as http:
        http.post("/api/nodes/register", json={"name": "P", "capabilities": ["location.get"]}, headers=phone)
        http.put(f"/api/nodes/{device_id}/consent", json={"capability": "location.get", "mode": "allow"}, headers={"X-Dashboard-Token": owner_key})

        def phone_loop():
            with httpx.Client(base_url=base, timeout=30) as p:
                got = p.get("/api/nodes/poll?wait=10", headers=phone).json()["commands"]
                for c in got:
                    p.post("/api/nodes/result", json={"id": c["id"], "ok": True, "data": {"lat": 51.5, "lon": -0.12}}, headers=phone)

        t = threading.Thread(target=phone_loop)
        t.start()
        time.sleep(0.5)
        result = asyncio.run_coroutine_threadsafe(nodes.invoke(device_id, "location.get", timeout_s=15), app_server_loop["loop"]).result(timeout=20)
        t.join()
    assert result == {"lat": 51.5, "lon": -0.12}


app_server_loop: dict = {}


@pytest.fixture
def app_server(monkeypatch, temp_db):
    import socket

    import uvicorn

    monkeypatch.setenv("DASHBOARD_TOKEN", "owner-token")
    device_id, key = db.create_api_key("phone", permission_tier="standard")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    # nodes.py keeps its queues in the server's own event loop; find it so the agent side can be driven there.
    app_server_loop["loop"] = server.servers[0].get_loop()
    yield f"http://127.0.0.1:{port}", "owner-token", key, device_id
    server.should_exit = True
    thread.join(timeout=10)


# ---- canvas ---------------------------------------------------------------------------------------------------------------------
def test_a_canvas_is_versioned_validated_and_redacted(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "server-secret-value-123")
    assert canvas.update("status", "<h1>Hello</h1>", title="Status") == {"name": "status", "version": 1}
    assert canvas.update("status", "<h1>Hello again server-secret-value-123</h1>")["version"] == 2
    assert "server-secret-value-123" not in canvas.read("status") and "[secret:MY_API_KEY]" in canvas.read("status")
    assert canvas.info("status")["title"] == "Status" and [c["name"] for c in canvas.listing()] == ["status"]
    for bad in ("Bad Name", "../x", "", "a" * 60):
        with pytest.raises(canvas.CanvasError):
            canvas.update(bad, "<p>x</p>")
    with pytest.raises(canvas.CanvasError, match="empty"):
        canvas.update("x", "   ")
    with pytest.raises(canvas.CanvasError, match="limited to"):
        canvas.update("big", "x" * 600_000)
    assert canvas.remove("status") and not canvas.remove("status") and canvas.info("status") is None


def test_signed_addresses_expire_and_are_bound_to_one_canvas():
    sig = canvas.sign("a", now=1000)
    assert canvas.verify("a", sig, now=1100) and not canvas.verify("b", sig, now=1100) and not canvas.verify("a", sig, now=1000 + canvas.SIG_TTL_S + 5)
    assert not canvas.verify("a", "garbage") and not canvas.verify("a", "9999999999.deadbeef")


def test_the_canvas_is_served_in_a_sandbox_and_only_with_a_valid_signature(client):
    canvas.update("board", "<h1>Progress</h1><script>fetch('/api/bots')</script>", title="Board")
    assert client.post("/api/canvas/board/link").status_code == 401
    link = client.post("/api/canvas/board/link", headers=OWNER).json()["url"]
    view = client.get(link)
    assert view.status_code == 200 and 'sandbox="allow-scripts"' in view.text and "Board" in view.text and "allow-same-origin" not in view.text
    sig = link.split("sig=")[1]
    page = client.get(f"/canvas/board?sig={sig}")
    assert page.status_code == 200 and "Progress" in page.text
    csp = page.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp and "connect-src 'none'" in csp and "default-src 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/canvas/board").status_code == 403 and client.get("/canvas/board?sig=forged").status_code == 403
    assert client.get(f"/canvas/other?sig={sig}").status_code in (403, 404)
    assert client.get("/canvas/nope/view?sig=x").status_code in (403, 404)
    version = client.get(f"/canvas/board/version?sig={sig}").json()
    assert version["version"] == 1 and version["src"].startswith("/canvas/board?sig=") and canvas.verify("board", version["sig"])
    canvas.update("board", "<h1>Progress 2</h1>")
    assert client.get(f"/canvas/board/version?sig={sig}").json()["version"] == 2
    assert client.get("/api/canvas", headers=OWNER).json()["canvases"][0]["name"] == "board"
    assert client.post("/api/canvas/nope/link", headers=OWNER).status_code == 404


def test_the_agent_draws_a_canvas_with_a_tool():
    out = run(tools.execute_tool("canvas_update", {"name": "results", "html": "<table><tr><td>1</td></tr></table>", "title": "R"}, workspace=None, instance_id=1))
    assert "version 1" in out and "/canvas/results/view" in out
    with pytest.raises(Exception, match="lowercase"):
        run(tools.execute_tool("canvas_update", {"name": "Bad Name", "html": "<p>x</p>"}, workspace=None, instance_id=1))
    assert not tools.is_dangerous("canvas_update")


# ---- voice -----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def vcfg(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(voice, "_cfg", lambda: values)
    return values


def test_voice_is_off_until_configured(vcfg):
    assert not voice.stt_enabled() and not voice.tts_enabled() and not voice.reply_with_voice()
    with pytest.raises(voice.VoiceError, match="not configured"):
        run(voice.transcribe(b"audio"))
    with pytest.raises(voice.VoiceError, match="not configured"):
        run(voice.synthesize("hi"))
    vcfg["reply_with_voice"] = True
    assert not voice.reply_with_voice(), "a spoken reply needs a text-to-speech engine"


def stt_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_transcription_goes_to_the_service_and_is_counted_against_its_allowance(vcfg, monkeypatch):
    from bot.agent_runtime import usage_limits

    monkeypatch.setenv("GROQ_KEY_FOR_TEST", "placeholder-key")
    vcfg["stt"] = {"engine": "openai_compatible", "base_url": "https://stt.example/v1", "api_key_env": "GROQ_KEY_FOR_TEST", "model": "whisper-large-v3", "language": "en"}
    seen = {}

    def handler(request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers.get("authorization"), request.content
        return httpx.Response(200, json={"text": "  turn the lights on  "})

    assert run(voice.transcribe(b"OggS...", "audio/ogg", client=stt_client(handler))) == "turn the lights on"
    assert seen["url"] == "https://stt.example/v1/audio/transcriptions" and seen["auth"] == "Bearer placeholder-key"
    assert b"whisper-large-v3" in seen["body"] and b'name="language"' in seen["body"] and b"OggS..." in seen["body"]
    row = usage_limits.report(1)[0]
    assert (row["provider"], row["model"], row["calls"]) == ("stt.example", "whisper-large-v3", 1)


def test_transcription_problems_are_reported_plainly(vcfg):
    vcfg["stt"] = {"engine": "openai_compatible", "base_url": "https://stt.example/v1", "model": "m"}
    with pytest.raises(voice.VoiceError, match="rate limit is used up"):
        run(voice.transcribe(b"x" * 10, client=stt_client(lambda r: httpx.Response(429, headers={"retry-after": "30"}, json={}))))
    from bot.agent_runtime import usage_limits

    usage_limits._blocked_until.clear()                       # the 429 above told the module to wait; forget that for the next case
    with pytest.raises(voice.VoiceError, match="answered 500"):
        run(voice.transcribe(b"x" * 10, client=stt_client(lambda r: httpx.Response(500, text="oops"))))
    with pytest.raises(voice.VoiceError, match="no speech"):
        run(voice.transcribe(b"x" * 10, client=stt_client(lambda r: httpx.Response(200, json={"text": ""}))))
    with pytest.raises(voice.VoiceError, match="empty"):
        run(voice.transcribe(b""))
    vcfg["max_seconds"] = 1
    with pytest.raises(voice.VoiceError, match="longer than"):
        run(voice.transcribe(b"x" * 20_000))


def test_a_used_up_allowance_stops_the_call_before_it_is_made(vcfg, monkeypatch):
    from bot.agent_runtime import usage_limits

    vcfg["stt"] = {"engine": "openai_compatible", "base_url": "https://stt.example/v1", "model": "m"}
    usage_limits.note_rate_limited("stt.example", "m", 600)
    called = []
    with pytest.raises(voice.VoiceError, match="asked us to wait"):
        run(voice.transcribe(b"x" * 10, client=stt_client(lambda r: called.append(1) or httpx.Response(200, json={"text": "x"}))))
    assert not called
    usage_limits._blocked_until.clear()


PY_STT = "import sys,pathlib\np=pathlib.Path(sys.argv[1]); out=pathlib.Path(sys.argv[2]); out.with_suffix('.txt').write_text('heard '+str(p.stat().st_size)+' bytes')\n"


def test_a_command_can_do_the_transcription(vcfg, tmp_path):
    script = tmp_path / "stt.py"
    script.write_text(PY_STT)
    vcfg["stt"] = {"engine": "command", "command": [sys.executable, str(script), "{input}", "{output}"]}
    assert run(voice.transcribe(b"12345", "audio/ogg")) == "heard 5 bytes"
    vcfg["stt"] = {"engine": "command", "command": [sys.executable, "-c", "import sys; sys.stderr.write('model missing'); sys.exit(3)"]}
    with pytest.raises(voice.VoiceError, match="model missing"):
        run(voice.transcribe(b"1"))
    vcfg["stt"] = {"engine": "command", "command": ["definitely-not-installed-xyz"]}
    with pytest.raises(voice.VoiceError, match="not installed"):
        run(voice.transcribe(b"1"))
    vcfg["stt"] = {"engine": "command"}
    with pytest.raises(voice.VoiceError, match="must be a list"):
        run(voice.transcribe(b"1"))


def test_speech_can_come_from_a_service_or_a_command(vcfg, tmp_path):
    vcfg["tts"] = {"engine": "openai_compatible", "base_url": "https://tts.example/v1", "model": "tts-1", "voice": "alloy", "format": "opus"}
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b"OggS-audio")

    audio, mime, ext = run(voice.synthesize("Hello there", client=stt_client(handler)))
    assert (audio, mime, ext) == (b"OggS-audio", "audio/ogg", "ogg") and seen["input"] == "Hello there" and seen["response_format"] == "opus"
    script = tmp_path / "tts.py"
    script.write_text("import sys\nopen(sys.argv[1],'wb').write(b'RIFF'+sys.stdin.read().encode())\n")
    vcfg["tts"] = {"engine": "command", "command": [sys.executable, str(script), "{output}"]}
    assert run(voice.synthesize("say this")) == (b"RIFFsay this", "audio/wav", "wav")
    vcfg["tts"] = {"engine": "openai_compatible", "base_url": "https://tts.example/v1"}
    with pytest.raises(voice.VoiceError, match="answered 401"):
        run(voice.synthesize("x", client=stt_client(lambda r: httpx.Response(401, text="no"))))
    with pytest.raises(voice.VoiceError, match="nothing to say"):
        run(voice.synthesize("   "))


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="Windows speech is only on Windows")
def test_windows_speech_produces_a_real_wave_file(vcfg):
    vcfg["tts"] = {"engine": "sapi"}
    audio, mime, ext = run(voice.synthesize("Testing one two three."))
    assert mime == "audio/wav" and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE" and len(audio) > 2000


def test_a_voice_message_is_transcribed_then_handled_as_text(vcfg, monkeypatch):
    """The Telegram handler: transcribe, say what was heard, then ask the agent exactly as if it had been typed."""
    from types import SimpleNamespace

    from bot import handlers

    vcfg["stt"] = {"engine": "openai_compatible", "base_url": "https://stt.example/v1", "model": "m"}
    replies, asked = [], []

    class Msg:
        voice = SimpleNamespace(file_id="f1", file_size=100, mime_type="audio/ogg")
        audio = None
        chat = SimpleNamespace(send_action=lambda *a: asyncio.sleep(0))

        async def reply_text(self, text):
            replies.append(text)

    class File:
        async def download_as_bytearray(self):
            return bytearray(b"OggS")

    class Bot:
        async def get_file(self, fid):
            return File()

    async def fake_transcribe(data, mime, **kw):
        return "what time is it"

    async def fake_ask(update, context, text, **kw):
        asked.append(text)

    monkeypatch.setattr(voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(handlers, "_handle_ask", fake_ask)
    update = SimpleNamespace(message=Msg(), effective_user=SimpleNamespace(id=1, username="u"), effective_chat=SimpleNamespace(id=1))
    context = SimpleNamespace(bot=Bot(), bot_data={"allowed_ids": {1}, "instance_id": 1}, user_data={})
    run(handlers.on_voice.__wrapped__(update, context))
    assert replies == ["Heard: what time is it"] and asked == ["what time is it"]
    vcfg["stt"] = {"engine": "off"}
    replies.clear()
    run(handlers.on_voice.__wrapped__(update, context))
    assert "isn't set up" in replies[0]
