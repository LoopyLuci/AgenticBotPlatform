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
A transcript can be wrong; the reply says what was heard, so a mistake is visible. Telegram (bot/handlers.py's on_voice),
Discord (bot/platforms/discord_platform.py) and Slack (bot/platforms/slack_platform.py) all run these same two functions
off the same `voice:` block — one pipeline, one set of settings, one set of words; bot/platforms/_voice.py is only how the
two adapters get bytes in and audio out. Tested against local stand-ins and `sapi` on a Windows machine; **not tested
against a real Whisper service, Piper, whisper.cpp, a real Discord server or a real Slack workspace.**
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
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


# ---- what every channel says ----------------------------------------------------------------
# One copy of the wording, so a person who speaks to the bot on Telegram, Discord or Slack is told the
# same thing in the same words. The handlers use these instead of their own literals. NO_DOWNLOAD is the
# platform adapters' extra case: Telegram's own file fetch reports its failures as a Bot API error.
NO_STT = "Voice messages need speech-to-text, which isn't set up (voice.stt in config/backends.yaml)."
TOO_LONG = "That voice message is too long for me to transcribe."
NO_DOWNLOAD = "I couldn't download that audio to listen to it."


def heard(text: str) -> str:
    """The transcript, said out loud first so a mis-heard message is visible before the answer is."""
    return f"Heard: {text}"


def not_transcribed(exc: Exception) -> str:
    return f"I couldn't transcribe that: {exc}"


# What a platform calls audio. Discord sends a voice note as .ogg and an mp3 as .mp3; Slack sends a voice
# message as .m4a and often supplies no mimetype at all, so the file name decides.
AUDIO_SUFFIXES = frozenset({".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".aac", ".flac", ".amr", ".wma"})
AUDIO_MIME_PREFIX = "audio/"
# The standard library guesses ".oga" for audio/ogg; ".ogg" is what a voice note is called everywhere
# else here, including this module's own default, so prefer it.
_PREFERRED_SUFFIX = {"audio/ogg": ".ogg", "audio/oga": ".ogg", "audio/x-ogg": ".ogg", "audio/mp4": ".m4a"}


def is_audio(filename: str = "", mimetype: str = "") -> bool:
    """Whether an attachment handed over by a chat platform is speech rather than a document."""
    if str(mimetype or "").split(";")[0].strip().lower().startswith(AUDIO_MIME_PREFIX):
        return True
    return Path(str(filename or "")).suffix.lower() in AUDIO_SUFFIXES


def audio_name(filename: str = "", mimetype: str = "", *, default: str = "voice.ogg") -> str:
    """A file name with a suffix the speech-to-text engine can recognise. Slack's file objects often carry
    only a name and no extension (and Discord sometimes no name at all), and an engine run as a command
    keys the suffix off the name, so this fills one in from the mimetype and then from the default."""
    name = str(filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if Path(name).suffix:
        return name
    kind = str(mimetype or "").split(";")[0].strip().lower()
    guessed = _PREFERRED_SUFFIX.get(kind) or mimetypes.guess_extension(kind) or ""
    if guessed:
        return (name or Path(default).stem or "voice") + guessed if is_audio(name, mimetype) else default
    return name or default


async def _run(argv: list[str], *, stdin: Optional[bytes] = None) -> tuple[bytes, bytes, int]:
    exe = shutil.which(argv[0]) or (argv[0] if Path(argv[0]).is_file() else None)
    if exe is None:
        raise VoiceError(f"{argv[0]!r} is not installed")
    from bot.agent_runtime import sandbox

    proc = await asyncio.create_subprocess_exec(exe, *argv[1:], stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=sandbox.build_env())
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=COMMAND_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        sandbox.kill(proc)
        raise VoiceError(f"{Path(exe).name} timed out after {int(COMMAND_TIMEOUT_S)}s") from exc
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
        raise VoiceError(str(exc)) from exc
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
        raise VoiceError(f"could not reach the speech-to-text service: {exc}") from exc
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
            raise VoiceError(f"could not reach the text-to-speech service: {exc}") from exc
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
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise VoiceError("Windows speech timed out") from exc
        if proc.returncode != 0 or not target.exists():
            raise VoiceError("Windows speech failed: " + err.decode("utf-8", "replace").strip()[:200])
        return target.read_bytes()
