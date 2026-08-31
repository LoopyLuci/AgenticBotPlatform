"""bot/dashboard/server.py's db.on_message_logged/on_job_changed wiring —
the two callbacks that turn a new chat message or job status change into a
'chat_message'/'job_update' broadcast over /api/ws, replacing Android's
(and eventually any client's) polling with a real push. Exercised directly
against the module's own callback functions rather than a live WebSocket —
matching this project's existing convention of driving async code via
asyncio.run() inside a plain `def test_...()` (see tests/test_router_
backend_shutdown.py), since this repo doesn't use pytest.mark.asyncio."""

from __future__ import annotations

import asyncio

from bot import db
from bot.dashboard import server


def test_on_message_logged_broadcasts_the_new_message(temp_db, monkeypatch):
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(server._manager, "broadcast", fake_broadcast)

    async def run():
        # db.log_message() itself calls server._on_message_logged() — it's
        # registered as a listener at module import time (see
        # bot/dashboard/server.py's db.on_message_logged(...) call) — so
        # nothing further needs to be triggered manually here.
        db.log_message(chat_id="123", direction="in", source="telegram", text="hi", instance_id=7)
        await asyncio.sleep(0)  # let the scheduled broadcast task actually run

    asyncio.run(run())

    assert len(broadcasts) == 1
    assert broadcasts[0]["type"] == "chat_message"
    assert broadcasts[0]["instance_id"] == 7
    assert broadcasts[0]["message"]["text"] == "hi"


def test_on_job_changed_broadcasts_the_updated_job(temp_db, monkeypatch):
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(server._manager, "broadcast", fake_broadcast)

    async def run():
        db.create_job(action_type="ask", backend="api", user_id=1, prompt="hello")
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(broadcasts) == 1
    assert broadcasts[0]["type"] == "job_update"
    assert broadcasts[0]["job"]["status"] == "queued"


def test_broadcast_soon_outside_an_event_loop_does_not_raise():
    # A callback firing with no running loop (shouldn't happen in
    # practice — see _broadcast_soon's own docstring) degrades to a
    # logged warning instead of propagating and breaking the write that
    # triggered it.
    server._broadcast_soon({"type": "chat_message"})
