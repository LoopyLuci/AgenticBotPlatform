"""bot/ssh_session_monitor.py - the SSH Toolkit's live session monitor and
recorder. Real end-to-end calls into the real PowerShell toolkit (not
mocked, same convention as tests/test_abp_cli.py's ssh_toolkit_home tests),
isolated from the real ~/.ssh/config via ABP_SSH_TOOLKIT_HOME. The target
host is deliberately unreachable (127.0.0.1:1, matching
test_ssh_run_command_over_a_real_local_loopback's own convention) - this
suite verifies the EVENT PIPELINE's shape and the recording lifecycle, not
that a real remote command succeeds.
"""

from __future__ import annotations

import asyncio

import pytest

from bot import ssh_session_monitor, ssh_toolkit


@pytest.fixture
def ssh_toolkit_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_SSH_TOOLKIT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def broadcasts(monkeypatch):
    """Captures every payload ssh_session_monitor._broadcast() would have
    sent to /api/ws, without needing a real live socket - this is what
    caught a real bug live (not in this suite, since it only asserted DB
    recording correctness before): _emit()'s broadcast call used to build
    its payload as {"type": "ssh_session_event", **full_event}, where
    full_event already had its OWN "type" key (start/stdout/stderr/exit/
    metric) - the later key in that dict literal silently wins, so the
    envelope's "ssh_session_event" type never actually reached the wire and
    every browser client's dispatcher never matched it. Fixed by nesting the
    real event under "event" instead of flattening - assert that shape here
    so this can never silently regress again."""
    captured: list[dict] = []
    monkeypatch.setattr(ssh_session_monitor, "_broadcast", captured.append)
    return captured


@pytest.fixture
def _clean_sessions():
    ssh_session_monitor._sessions.clear()
    ssh_session_monitor._next_id = 0
    yield
    ssh_session_monitor._sessions.clear()


async def _wait_until(predicate, *, timeout=15.0, interval=0.1):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return
        await asyncio.sleep(interval)
        elapsed += interval
    assert predicate()


def test_stream_command_yields_start_and_exit_events(ssh_toolkit_home):
    async def _run():
        events = []
        async for event in ssh_toolkit.stream_command("cli-test-run", "echo hi", timeout=10.0):
            events.append(event)
        return events

    # No connection named "cli-test-run" is registered - `ssh` itself will fail to
    # resolve/connect, but the event pipeline must still produce a clean start/exit
    # pair rather than raising or hanging.
    events = asyncio.run(_run())
    assert events[0]["type"] == "start"
    assert events[0]["command"] == "echo hi"
    assert events[-1]["type"] == "exit"
    assert events[-1]["code"] not in (0, None)


def test_broadcast_envelope_nests_the_real_event_under_event_key(temp_db, ssh_toolkit_home, _clean_sessions, broadcasts):
    async def _run():
        session_id = await ssh_session_monitor.start_session("cli-test-run", "echo hi")
        await _wait_until(lambda: ssh_session_monitor.get_session(session_id).status == "finished")

    asyncio.run(_run())
    session_events = [b for b in broadcasts if b.get("type") == "ssh_session_event"]
    assert session_events, "no ssh_session_event broadcasts captured at all"
    for payload in session_events:
        # The envelope's own type must survive - it must NOT be silently
        # overwritten by the nested event's own type (start/stdout/exit/...).
        assert payload["type"] == "ssh_session_event"
        assert "event" in payload and "type" in payload["event"]
    assert session_events[0]["event"]["type"] == "start"
    assert session_events[-1]["event"]["type"] == "exit"


def test_session_lifecycle_and_recording(temp_db, ssh_toolkit_home, _clean_sessions, broadcasts):
    async def _run():
        session_id = await ssh_session_monitor.start_session("cli-test-run", "echo hi")
        assert session_id in [s["session_id"] for s in ssh_session_monitor.list_sessions()]

        recording_id = ssh_session_monitor.start_recording(session_id)
        assert recording_id > 0

        await _wait_until(lambda: ssh_session_monitor.get_session(session_id).status == "finished")

        stopped_id = ssh_session_monitor.stop_recording(session_id)
        assert stopped_id == recording_id
        return recording_id

    recording_id = asyncio.run(_run())

    recordings = ssh_session_monitor.list_recordings()
    assert any(r["id"] == recording_id for r in recordings)
    row = next(r for r in recordings if r["id"] == recording_id)
    assert row["status"] == "stopped"
    assert row["event_count"] >= 2  # at least start + exit

    full = ssh_session_monitor.get_recording(recording_id)
    types = [e["event_type"] for e in full["events"]]
    assert types[0] == "start"
    assert types[-1] == "exit"

    ssh_session_monitor.delete_recording(recording_id)
    assert not any(r["id"] == recording_id for r in ssh_session_monitor.list_recordings())


def test_pause_resume_recording_skips_events_while_paused(temp_db, ssh_toolkit_home, _clean_sessions):
    async def _run():
        session_id = await ssh_session_monitor.start_session("cli-test-run", "echo hi")
        recording_id = ssh_session_monitor.start_recording(session_id)
        ssh_session_monitor.pause_recording(session_id)
        assert ssh_session_monitor.get_session(session_id).recording_paused is True

        await _wait_until(lambda: ssh_session_monitor.get_session(session_id).status == "finished")
        # The exit event happened while paused, so it must NOT have been persisted.
        events_while_paused = ssh_session_monitor.get_recording(recording_id)["events"]

        ssh_session_monitor.resume_recording(session_id)
        assert ssh_session_monitor.get_session(session_id).recording_paused is False
        ssh_session_monitor.stop_recording(session_id)
        return recording_id, events_while_paused

    recording_id, events_while_paused = asyncio.run(_run())
    assert all(e["event_type"] != "exit" for e in events_while_paused)


def test_stop_session_marks_status_stopped(temp_db, ssh_toolkit_home, _clean_sessions):
    async def _run():
        session_id = await ssh_session_monitor.start_session("cli-test-run", "sleep 5")
        await ssh_session_monitor.stop_session(session_id)
        return session_id

    session_id = asyncio.run(_run())
    assert ssh_session_monitor.get_session(session_id).status == "stopped"


def test_stop_session_unknown_raises(temp_db, ssh_toolkit_home, _clean_sessions):
    async def _run():
        with pytest.raises(ssh_toolkit.SshToolkitError):
            await ssh_session_monitor.stop_session("no-such-session")

    asyncio.run(_run())
