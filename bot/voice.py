"""Speech to text and text to speech (roadmap P7): voice messages in, a spoken reply out.

Everything is off until configured (`voice:` in config/backends.yaml). There is no bundled model; ABP calls what you point it at:

    voice:
      stt:                                  # speech -> text
        engine: openai_compatible           # off | openai_compatible | command
        base_url: https://api.groq.com/openai/v1
        api_key_env: GROQ_API_KEY
        model: whisper-large-v3
        language: ""                        # blank = detect
        # engine: command  ->  command: ["whisper-cli", "-m", "models/ggml-base.bin", "-f", "{input}", "-otxt", "-of", "{output}"]
      tts:                                  # text -> speech
        engine: off                         # off | openai_compatible | sapi (Windows) | command
        base_url: https://api.openai.com/v1
        api_key_env: OPENAI_API_KEY
        model: tts-1
        voice: alloy
        format: opus                        # opus (plays as a Telegram voice note) | mp3 | wav
        # engine: command  ->  command: ["piper", "--model", "en_US.onnx", "--output_file", "{output}"], text on standard input
      reply_with_voice: false               # answer a voice message with a voice message too
      max_seconds: 300                      # ignore audio longer than this (by file size, a rough guide)

* `openai_compatible` speaks the `/audio/transcriptions` and `/audio/speech` API that OpenAI, Groq (whose free tier
  includes Whisper) and many local servers (faster-whisper-server, LocalAI) offer. Speech-to-text calls are counted
  against the model's allowance like any other (usage_limits.py) and a used-up allowance is reported plainly.
* `command` runs a program of yours without a shell, with `{input}` / `{output}` replaced by temporary file paths; use
  it for whisper.cpp, Piper, espeak and the like. Input audio is passed as received (Telegram voice notes are Ogg/Opus);
  a program that needs WAV needs its own conversion step (for example a small script that calls ffmpeg first).
* `sapi` uses Windows' built-in speech (Windows PowerShell's System.Speech) and produces WAV.

Voice messages are treated exactly like typed text once transcribed: same allow-list, same permissions, same approvals.
A transcript can be wrong; the reply says what was heard, so a mistake is visible. Tested with fakes, and `sapi` on a
Windows machine; **not tested against a real Whisper service, Piper or whisper.cpp.**
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("bot.voice")

MAX_BYTES_PER_SECOND = 8_000            # a rough ceiling for compressed speech, used to turn max_seconds into a size limit
COMMAND_TIMEOUT_S = 120.0
MAX_TTS_CHARS = 3000


class VoiceError(Exception):
    pass


def _cfg() -> dict:
    try:
        from bot.config import config

        return (config.current.get("voice")) or {}
    except Exception:  # noqa: BLE001
        return {}


def stt_enabled() -> bool:
    return str((_cfg().get("stt") or {}).get("engine", "off")).lower() not in ("off", "", "none")


def tts_enabled() -> bool:
    return str((_cfg().get("tts") or {}).get("engine", "off")).lower() not in ("off", "", "none")


def reply_with_voice() -> bool:
    return bool(_cfg().get("reply_with_voice", False)) and tts_enabled()


def max_bytes() -> int:
    try:
        return int(float(_cfg().get("max_seconds", 300)) * MAX_BYTES_PER_SECOND)
    except (TypeError, ValueError):
        return 300 * MAX_BYTES_PER_SECOND


def _key(section: dict) -> str:
    env = str(section.get("api_key_env") or "")
    return os.environ.get(env, "") if env else str(section.get("api_key") or "")


async def _run(argv: list[str], *, stdin: Optional[bytes] = None) -> tuple[bytes, bytes, int]:
    exe = shutil.which(argv[0]) or (argv[0] if Path(argv[0]).is_file() else None)
    if exe is None:
        raise VoiceError(f"{argv[0]!r} is not installed")
    from bot.agent_runtime import sandbox

    proc = await asyncio.create_subprocess_exec(exe, *argv[1:], stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=sandbox.build_env())
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=COMMAND_TIMEOUT_S)
    except asyncio.TimeoutError:
        sandbox.kill(proc)
        raise VoiceError(f"{Path(exe).name} timed out after {int(COMMAND_TIMEOUT_S)}s")
    return out, err, proc.returncode or 0


# ---- speech to text ---------------------------------------------------------------------------------
async def transcribe(audio: bytes, mime: str = "audio/ogg", *, filename: str = "voice.ogg", client: Optional[httpx.AsyncClient] = None) -> str:
    cfg = _cfg().get("stt") or {}
    engine = str(cfg.get("engine", "off")).lower()
    if engine in ("off", "", "none"):
        raise VoiceError("speech to text is not configured (voice.stt.engine)")
    if not audio:
        raise VoiceError("the audio is empty")
    if len(audio) > max_bytes():
        raise VoiceError(f"that voice message is longer than the {int(_cfg().get('max_seconds', 300))} second limit")
    if engine == "openai_compatible":
        return await _stt_http(cfg, audio, mime, filename, client)
    if engine == "command":
        return await _stt_command(cfg, audio, Path(filename).suffix or ".ogg")
    raise VoiceError(f"unknown speech-to-text engine {engine!r}")


async def _stt_http(cfg: dict, audio: bytes, mime: str, filename: str, client: Optional[httpx.AsyncClient]) -> str:
    from bot.agent_runtime import usage_limits

    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "whisper-1")
    if not base:
        raise VoiceError("voice.stt.base_url is not set")
    from urllib.parse import urlparse

    provider = (urlparse(base).hostname or base).lower()
    try:
        await usage_limits.before_call(provider, model, 0)
    except usage_limits.RateLimited as exc:
        raise VoiceError(str(exc))
    headers = {"Authorization": f"Bearer {_key(cfg)}"} if _key(cfg) else {}
    data = {"model": model, "response_format": "json"}
    if cfg.get("language"):
        data["language"] = str(cfg["language"])
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    status = 0
    try:
        r = await client.post(f"{base}/audio/transcriptions", headers=headers, data=data, files={"file": (filename, audio, mime)})
        status = r.status_code
        usage_limits.observe_headers(provider, model, dict(r.headers))
        if status == 429:
            usage_limits.note_rate_limited(provider, model, usage_limits.parse_headers(dict(r.headers)).get("retry_after_s"))
            raise VoiceError("the speech-to-text service says its rate limit is used up; try again shortly")
        if status >= 300:
            raise VoiceError(f"the speech-to-text service answered {status}: {r.text[:200]}")
        text = str((r.json() or {}).get("text", "")).strip()
    except httpx.HTTPError as exc:
        status = status or 599
        raise VoiceError(f"could not reach the speech-to-text service: {exc}")
    finally:
        usage_limits.record(provider, model, tokens=0, status=status or 599)
        if own:
            await client.aclose()
    if not text:
        raise VoiceError("no speech was recognised")
    return text


async def _stt_command(cfg: dict, audio: bytes, suffix: str) -> str:
    command = cfg.get("command")
    if not isinstance(command, list) or not command:
        raise VoiceError("voice.stt.command must be a list such as [\"whisper-cli\", \"-f\", \"{input}\"]")
    with tempfile.TemporaryDirectory(prefix="abp-stt-") as tmp:
        source = Path(tmp) / f"in{suffix}"
        source.write_bytes(audio)
        target = Path(tmp) / "out"
        argv = [str(a).replace("{input}", str(source)).replace("{output}", str(target)) for a in command]
        out, err, code = await _run(argv)
        if code != 0:
            raise VoiceError(f"{Path(argv[0]).name} failed: {(err or out).decode('utf-8', 'replace').strip()[:200]}")
        written = target.with_suffix(".txt") if target.with_suffix(".txt").exists() else target
        text = (written.read_text(encoding="utf-8", errors="replace") if written.exists() else out.decode("utf-8", "replace")).strip()
    if not text:
        raise VoiceError("no speech was recognised")
    return text


# ---- text to speech --------------------------------------------------------------------------------------
async def synthesize(text: str, *, client: Optional[httpx.AsyncClient] = None) -> tuple[bytes, str, str]:
    """(audio, mime type, file extension) for `text`."""
    cfg = _cfg().get("tts") or {}
    engine = str(cfg.get("engine", "off")).lower()
    text = (text or "").strip()[:MAX_TTS_CHARS]
    if engine in ("off", "", "none"):
        raise VoiceError("text to speech is not configured (voice.tts.engine)")
    if not text:
        raise VoiceError("there is nothing to say")
    if engine == "openai_compatible":
        fmt = str(cfg.get("format", "opus")).lower()
        own = client is None
        client = client or httpx.AsyncClient(timeout=60)
        try:
            headers = {"Authorization": f"Bearer {_key(cfg)}"} if _key(cfg) else {}
            r = await client.post(f"{str(cfg.get('base_url') or '').rstrip('/')}/audio/speech", headers=headers,
                                  json={"model": cfg.get("model", "tts-1"), "input": text, "voice": cfg.get("voice", "alloy"), "response_format": fmt})
            if r.status_code >= 300:
                raise VoiceError(f"the text-to-speech service answered {r.status_code}: {r.text[:200]}")
            return r.content, {"opus": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav"}.get(fmt, "application/octet-stream"), {"opus": "ogg"}.get(fmt, fmt)
        except httpx.HTTPError as exc:
            raise VoiceError(f"could not reach the text-to-speech service: {exc}")
        finally:
            if own:
                await client.aclose()
    if engine == "sapi":
        return await _tts_sapi(text, str(cfg.get("voice") or "")), "audio/wav", "wav"
    if engine == "command":
        command = cfg.get("command")
        if not isinstance(command, list) or not command:
            raise VoiceError("voice.tts.command must be a list such as [\"piper\", \"--output_file\", \"{output}\"]")
        with tempfile.TemporaryDirectory(prefix="abp-tts-") as tmp:
            target = Path(tmp) / "speech.wav"
            argv = [str(a).replace("{output}", str(target)) for a in command]
            out, err, code = await _run(argv, stdin=text.encode("utf-8"))
            if code != 0 or not target.exists():
                raise VoiceError(f"{Path(argv[0]).name} failed: {(err or out).decode('utf-8', 'replace').strip()[:200]}")
            return target.read_bytes(), "audio/wav", "wav"
    raise VoiceError(f"unknown text-to-speech engine {engine!r}")


_SAPI = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:ABP_TTS_VOICE) { try { $s.SelectVoice($env:ABP_TTS_VOICE) } catch {} }
$s.SetOutputToWaveFile($env:ABP_TTS_OUT)
$s.Speak([Console]::In.ReadToEnd())
$s.Dispose()
"""


async def _tts_sapi(text: str, voice: str) -> bytes:
    if os.name != "nt":
        raise VoiceError("the sapi engine is only available on Windows")
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if exe is None:
        raise VoiceError("Windows PowerShell was not found")
    with tempfile.TemporaryDirectory(prefix="abp-sapi-") as tmp:
        target = Path(tmp) / "speech.wav"
        env = {**os.environ, "ABP_TTS_OUT": str(target), "ABP_TTS_VOICE": voice}
        proc = await asyncio.create_subprocess_exec(exe, "-NoProfile", "-NonInteractive", "-Command", _SAPI, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            _, err = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=COMMAND_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise VoiceError("Windows speech timed out")
        if proc.returncode != 0 or not target.exists():
            raise VoiceError("Windows speech failed: " + err.decode("utf-8", "replace").strip()[:200])
        return target.read_bytes()
