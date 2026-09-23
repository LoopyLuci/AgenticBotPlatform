"""Live SSH session monitor + recorder, backing the SSH Toolkit GUI's "watch
a connection" feature: structured events (command started, each output
line, periodic CPU/mem, exit code) - never raw video or screen-share bytes -
broadcast live over the dashboard's existing /api/ws socket
(`type: "ssh_session_event"`, same live-events channel job_tool_event/
chat_message already use) so a GUI can show every action an agent or user
takes over an SSH connection as it happens, plus an optional recording
(bot/db.py's ssh_session_recordings/ssh_session_events) so that exact
sequence can be replayed later.

In-memory session table only (a "session" here is a running-or-recently-
finished watched command, not something that needs to survive a server
restart) - recordings are the durable, replayable artifact and live in the
database like everything else this app persists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from bot import db, ssh_toolkit

logger = logging.getLogger("bot.ssh_session_monitor")

METRICS_INTERVAL_S = 3.0


@dataclass
class _Session:
    id: str
    connection_name: str
    command: str
    task: Optional[asyncio.Task] = None
    metrics_task: Optional[asyncio.Task] = None
    recording_id: Optional[int] = None
    recording_paused: bool = False
    status: str = "running"  # running|finished|stopped
    seq: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None


_sessions: dict[str, _Session] = {}
_next_id = 0


def _broadcast(payload: dict) -> None:
    # Lazy import: bot.dashboard.server imports bot.ssh_session_monitor's route
    # handlers' modules at request time, not the other way around at import time -
    # a top-level import here would be circular.
    from bot.dashboard.server import _broadcast_soon

    _broadcast_soon(payload)


def _emit(session: _Session, event: dict) -> None:
    session.seq += 1
    full_event = {**event, "session_id": session.id, "seq": session.seq}
    # Nested under "event", not flattened, on purpose - event["type"] (start/
    # stdout/stderr/exit/metric) and the envelope's own "type" (always
    # "ssh_session_event") would otherwise collide in the same dict literal,
    # with the LAST one written silently winning - the exact trap
    # job_tool_event's own broadcast (server.py's _on_job_tool_event) already
    # avoids by nesting its event the same way. Confirmed live: flattening
    # this meant every browser client's dispatcher never saw "ssh_session_event"
    # at all - only the inner type ever reached the wire - so the live feed
    # stayed empty despite every event correctly recording to the database.
    _broadcast({"type": "ssh_session_event", "session_id": session.id, "seq": session.seq, "event": event})
    if session.recording_id is not None and not session.recording_paused:
        db.log_ssh_session_event(session.recording_id, full_event["type"], full_event)


async def start_session(connection_name: str, command: str) -> str:
    global _next_id
    _next_id += 1
    session_id = f"s{_next_id}"
    session = _Session(id=session_id, connection_name=connection_name, command=command)
    _sessions[session_id] = session

    async def _run() -> None:
        try:
            async for event in ssh_toolkit.stream_command(connection_name, command):
                _emit(session, event)
                if event["type"] == "exit":
                    session.exit_code = event.get("code")
        except ssh_toolkit.SshToolkitError as exc:
            _emit(session, {"type": "note", "ts": time.time(), "text": f"error: {exc}"})
        except asyncio.CancelledError:
            raise
        finally:
            session.status = "finished" if session.status == "running" else session.status
            session.finished_at = time.time()
            if session.metrics_task:
                session.metrics_task.cancel()

    async def _metrics_loop() -> None:
        while True:
            await asyncio.sleep(METRICS_INTERVAL_S)
            metrics = await ssh_toolkit.probe_metrics(connection_name)
            if metrics:
                _emit(session, {"type": "metric", "ts": time.time(), **metrics})

    session.task = asyncio.create_task(_run())
    session.metrics_task = asyncio.create_task(_metrics_loop())
    _broadcast({
        "type": "ssh_session_started", "session_id": session_id,
        "connection": connection_name, "command": command,
    })
    return session_id


def get_session(session_id: str) -> Optional[_Session]:
    return _sessions.get(session_id)


def list_sessions() -> list[dict]:
    return [
        {
            "session_id": s.id, "connection": s.connection_name, "command": s.command,
            "status": s.status, "recording_id": s.recording_id, "recording_paused": s.recording_paused,
            "started_at": s.started_at, "finished_at": s.finished_at, "exit_code": s.exit_code,
        }
        for s in _sessions.values()
    ]


async def stop_session(session_id: str) -> None:
    session = _sessions.get(session_id)
    if session is None:
        raise ssh_toolkit.SshToolkitError(f"no such session {session_id!r}")
    if session.task and not session.task.done():
        session.task.cancel()
    if session.metrics_task:
        session.metrics_task.cancel()
    session.status = "stopped"
    session.finished_at = time.time()
    _broadcast({"type": "ssh_session_stopped", "session_id": session_id})


def start_recording(session_id: str) -> int:
    session = _sessions.get(session_id)
    if session is None:
        raise ssh_toolkit.SshToolkitError(f"no such session {session_id!r}")
    recording_id = db.create_ssh_recording(session.connection_name, session.command)
    session.recording_id = recording_id
    session.recording_paused = False
    _broadcast({
        "type": "ssh_recording_state", "session_id": session_id,
        "recording_id": recording_id, "state": "recording",
    })
    return recording_id


def _require_active_recording(session_id: str) -> _Session:
    session = _sessions.get(session_id)
    if session is None:
        raise ssh_toolkit.SshToolkitError(f"no such session {session_id!r}")
    if session.recording_id is None:
        raise ssh_toolkit.SshToolkitError(f"session {session_id!r} has no active recording")
    return session


def pause_recording(session_id: str) -> None:
    session = _require_active_recording(session_id)
    session.recording_paused = True
    db.set_ssh_recording_status(session.recording_id, "paused")
    _broadcast({
        "type": "ssh_recording_state", "session_id": session_id,
        "recording_id": session.recording_id, "state": "paused",
    })


def resume_recording(session_id: str) -> None:
    session = _require_active_recording(session_id)
    session.recording_paused = False
    db.set_ssh_recording_status(session.recording_id, "recording")
    _broadcast({
        "type": "ssh_recording_state", "session_id": session_id,
        "recording_id": session.recording_id, "state": "recording",
    })


def stop_recording(session_id: str) -> int:
    session = _require_active_recording(session_id)
    recording_id = session.recording_id
    db.set_ssh_recording_status(recording_id, "stopped")
    session.recording_id = None
    session.recording_paused = False
    _broadcast({
        "type": "ssh_recording_state", "session_id": session_id,
        "recording_id": recording_id, "state": "stopped",
    })
    return recording_id


def list_recordings() -> list[dict]:
    return [dict(r) for r in db.list_ssh_recordings()]


def get_recording(recording_id: int) -> dict:
    row = db.get_ssh_recording(recording_id)
    if row is None:
        raise ssh_toolkit.SshToolkitError(f"no such recording {recording_id}")
    events = []
    for r in db.list_ssh_session_events(recording_id):
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        events.append(d)
    return {**dict(row), "events": events}


def delete_recording(recording_id: int) -> None:
    db.delete_ssh_recording(recording_id)
