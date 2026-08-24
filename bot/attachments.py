"""Safe on-disk storage for chat attachments — inbound (a user's file sent
in via Telegram/Discord/Slack) and outbound (a file the dashboard sends
out through a bot).

Never trust a platform- or client-supplied filename for the actual path:
it can contain path separators, `..` traversal, reserved names, or be
used to collide two different bot instances' files. Every file is stored
under a generated uuid-prefixed, whitelisted-character basename; the
original filename is kept only as display metadata in the `messages`
table, never as a filesystem path.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

ATTACHMENTS_DIR = PROJECT_ROOT / "data" / "attachments"
THUMBS_DIR = ATTACHMENTS_DIR / "thumbs"
_CHUNKS_ROOT = ATTACHMENTS_DIR / "_upload_sessions"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_CHUNK_SIZE = 1024 * 1024  # 1MB
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB — what init tells clients to send per chunk

# In-memory registry of in-progress chunked uploads. Deliberately not
# persisted to the DB: sessions are short-lived (a single active transfer,
# start to finish, within one server run) and this is a personal
# single-process server — losing an in-flight upload on a server restart is
# an acceptable trade-off for not adding a table + migration + cleanup job
# for state nothing else ever needs to query.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_TTL_S = 3600  # abandoned sessions (client vanished mid-upload) reaped lazily on next init


def _safe_name(original_name: str) -> tuple[str, str]:
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    base = Path(original_name or "file").name  # strips any directory components
    base = _UNSAFE.sub("_", base)[:120] or "file"
    return f"{uuid.uuid4().hex}_{base}", (original_name or base)


def safe_store(original_name: str, data: bytes) -> tuple[str, str]:
    """Writes `data` under a generated-safe filename inside
    ATTACHMENTS_DIR. Returns (relative_path, display_name) — relative_path
    is the bare filename to store in the DB and resolve downloads against;
    display_name is the original filename, for showing/downloading-as."""
    fname, display_name = _safe_name(original_name)
    (ATTACHMENTS_DIR / fname).write_bytes(data)
    return fname, display_name


async def safe_store_stream(original_name: str, upload_file: Any, max_bytes: int) -> tuple[str, str]:
    """Streaming counterpart of safe_store() — reads an UploadFile in
    _CHUNK_SIZE pieces and writes each straight to disk instead of
    buffering the whole upload in memory first. A file that crosses
    max_bytes is caught mid-stream (at most one chunk over the limit ever
    sits in memory, not the whole thing) and its partial write is deleted
    before raising, so concurrent uploads from multiple paired devices
    can't each hold a full 25MB in RAM at once."""
    fname, display_name = _safe_name(original_name)
    dest = ATTACHMENTS_DIR / fname
    total = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await upload_file.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"file too large ({max_bytes} bytes max)")
                f.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return fname, display_name


def _reap_stale_sessions() -> None:
    """Called under _SESSIONS_LOCK from create_upload_session() — cheap
    (in-memory dict scan), no need for a background timer."""
    now = time.time()
    stale = [sid for sid, s in _SESSIONS.items() if now - s["created_at"] > _SESSION_TTL_S]
    for sid in stale:
        session = _SESSIONS.pop(sid)
        shutil.rmtree(session["chunk_dir"], ignore_errors=True)


def create_upload_session(
    filename: str,
    total_size: int,
    mime: Optional[str],
    max_bytes: int,
    **context: Any,
) -> dict[str, Any]:
    """Registers a new chunked upload and returns the session id + the
    chunk size the client should send. Raises ValueError if the declared
    size already exceeds the configured ceiling — reject before a single
    byte is transferred, not partway through.

    `context` is opaque here — whatever the caller passes (e.g.
    instance_id/chat_id/text for a platform chat send, or conversation_id
    for a Server Chat send) comes back unchanged from assemble_upload(),
    so this module never needs to know about either caller's shape."""
    if total_size <= 0:
        raise ValueError("total_size must be positive")
    if total_size > max_bytes:
        raise ValueError(f"file too large ({max_bytes} bytes max)")
    session_id = uuid.uuid4().hex
    chunk_dir = _CHUNKS_ROOT / session_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    with _SESSIONS_LOCK:
        _reap_stale_sessions()
        _SESSIONS[session_id] = {
            "filename": filename,
            "mime": mime,
            "total_size": total_size,
            "context": context,
            "chunk_dir": chunk_dir,
            "created_at": time.time(),
        }
    return {"session_id": session_id, "chunk_size": UPLOAD_CHUNK_SIZE}


async def write_chunk(session_id: str, index: int, request: Any) -> None:
    """Streams one chunk of a registered upload straight to its own file on
    disk via the request body stream — never buffers a whole chunk in
    memory first, same principle as safe_store_stream(). Idempotent: a
    client that retries a dropped chunk just overwrites the same file, so
    resuming a stalled transfer only costs re-sending the chunks that never
    landed, not the whole upload."""
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise KeyError(f"unknown or expired upload session {session_id!r}")
    dest = session["chunk_dir"] / f"{index:08d}"
    tmp = dest.with_suffix(".part")
    with open(tmp, "wb") as f:
        async for part in request.stream():
            f.write(part)
    tmp.replace(dest)


def assemble_upload(session_id: str) -> dict[str, Any]:
    """Concatenates a completed session's chunks into the final attachment
    file, in order, streaming chunk-by-chunk rather than loading the whole
    thing into memory — this is the one step that can legitimately touch
    gigabytes of data, so it always runs off the event loop via
    run_in_executor at the call site. Verifies the assembled size matches
    what the client declared at init time before accepting it."""
    with _SESSIONS_LOCK:
        session = _SESSIONS.pop(session_id, None)
    if session is None:
        raise KeyError(f"unknown or expired upload session {session_id!r}")
    chunk_dir: Path = session["chunk_dir"]
    fname, display_name = _safe_name(session["filename"])
    dest = ATTACHMENTS_DIR / fname
    total = 0
    try:
        chunk_files = sorted(chunk_dir.glob("[0-9]" * 8))
        with open(dest, "wb") as out:
            for chunk_path in chunk_files:
                with open(chunk_path, "rb") as cf:
                    while data := cf.read(_CHUNK_SIZE):
                        total += len(data)
                        out.write(data)
        if total != session["total_size"]:
            raise ValueError(
                f"assembled {total} bytes, expected {session['total_size']} — "
                "a chunk is missing, resend the upload"
            )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)
    return {
        "rel_path": fname,
        "display_name": display_name,
        "mime": session["mime"],
        "size": total,
        **session["context"],
    }
