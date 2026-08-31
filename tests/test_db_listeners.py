"""db.on_message_logged/on_job_changed — the hook the dashboard's WebSocket
broadcaster (bot/dashboard/server.py) uses to learn a new chat message or
job status change was just written, without db.py importing anything from
the dashboard layer. See db.py's comment on the listener lists for why."""

from bot import db


def test_log_message_notifies_listeners(temp_db, monkeypatch):
    monkeypatch.setattr(db, "_message_listeners", [])
    seen = []
    db.on_message_logged(seen.append)

    message_id = db.log_message(chat_id="123", direction="in", source="telegram", text="hi")

    assert seen == [message_id]


def test_a_failing_message_listener_does_not_break_logging(temp_db, monkeypatch):
    monkeypatch.setattr(db, "_message_listeners", [])
    db.on_message_logged(lambda _id: (_ for _ in ()).throw(RuntimeError("boom")))

    message_id = db.log_message(chat_id="123", direction="in", source="telegram", text="hi")

    assert db.get_message(message_id) is not None


def test_create_job_notifies_listeners(temp_db, monkeypatch):
    monkeypatch.setattr(db, "_job_listeners", [])
    seen = []
    db.on_job_changed(seen.append)

    job_id = db.create_job(action_type="ask", backend="api", user_id=1, prompt="hello")

    assert seen == [job_id]


def test_mark_job_running_and_done_notify_listeners(temp_db, monkeypatch):
    monkeypatch.setattr(db, "_job_listeners", [])
    job_id = db.create_job(action_type="ask", backend="api", user_id=1, prompt="hello")
    seen = []
    db.on_job_changed(seen.append)

    db.mark_job_running(job_id, backend="api")
    db.mark_job_done(job_id, status="success", result="done")

    assert seen == [job_id, job_id]
