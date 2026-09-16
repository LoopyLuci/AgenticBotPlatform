"""bot/activity_log.py — the ring buffer backing the GUI's Activity tab.
Attaches to the real root logger (same as bot/main.py's setup_logging()
wires it), so every test here resets the module's global handler state
first to stay isolated from whatever other tests already logged.
"""
from __future__ import annotations

import logging

import pytest

from bot import activity_log


@pytest.fixture(autouse=True)
def _reset_handler():
    # Plain Python defaults the root logger to WARNING — bot/main.py's
    # real setup_logging() lowers it to INFO in production, but nothing
    # does that in a bare test process, so an untouched root level here
    # would silently drop every .info() call below before it ever reaches
    # a handler (a logger-level filter, separate from and prior to any
    # handler-level one). Match production for the duration of each test.
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    # A stray _RingBufferHandler left attached by an earlier import of
    # bot.dashboard.server (which subscribes its own copy at import time)
    # would double-deliver every record into a handler this test can't
    # see or reset via activity_log._handler alone — strip any handler of
    # this exact class before each test so only the fresh one below exists.
    for h in list(root.handlers):
        if isinstance(h, activity_log._RingBufferHandler):
            root.removeHandler(h)
    activity_log._handler = None
    yield
    if activity_log._handler is not None:
        logging.getLogger().removeHandler(activity_log._handler)
    activity_log._handler = None
    root.setLevel(old_level)


def test_install_is_idempotent():
    h1 = activity_log.install()
    h2 = activity_log.install()
    assert h1 is h2
    assert logging.getLogger().handlers.count(h1) == 1


def test_recent_captures_a_log_record():
    activity_log.install()
    logging.getLogger("test.module").warning("something happened")
    entries = activity_log.recent()
    assert len(entries) == 1
    assert entries[0]["level"] == "WARNING"
    assert entries[0]["logger"] == "test.module"
    assert entries[0]["message"] == "something happened"


def test_since_id_filters_out_already_seen_entries():
    activity_log.install()
    logging.getLogger("t").info("first")
    first_id = activity_log.recent()[0]["id"]
    logging.getLogger("t").info("second")
    logging.getLogger("t").info("third")
    newer = activity_log.recent(since_id=first_id)
    assert [e["message"] for e in newer] == ["second", "third"]


def test_ring_buffer_caps_at_maxlen():
    handler = activity_log.install()
    handler._buf = handler._buf.__class__(handler._buf, maxlen=3)
    logger = logging.getLogger("t")
    for i in range(5):
        logger.info(str(i))
    entries = activity_log.recent(limit=10)
    assert [e["message"] for e in entries] == ["2", "3", "4"]


def test_subscribe_receives_new_entries_live():
    activity_log.install()
    received = []
    unsubscribe = activity_log.subscribe(received.append)
    logging.getLogger("t").error("boom")
    assert len(received) == 1
    assert received[0].message == "boom"
    unsubscribe()
    logging.getLogger("t").error("after unsubscribe")
    assert len(received) == 1  # unaffected — unsubscribed


def test_a_broken_subscriber_does_not_break_logging():
    activity_log.install()

    def _bad_callback(_entry):
        raise RuntimeError("subscriber bug")

    activity_log.subscribe(_bad_callback)
    logging.getLogger("t").info("still works")  # must not raise
    assert len(activity_log.recent()) == 1


def test_a_subscriber_that_logs_does_not_recurse_infinitely():
    """Confirmed live: bot.dashboard.server's _on_activity_entry falls
    back to logger.warning(...) when called outside a running event loop
    — since this handler sits on the root logger, that warning re-enters
    emit() on the same thread, which used to re-notify subscribers, which
    warned again, forever (a real crash, not hypothetical: exhausts the
    recursion limit or the actual C stack). The record still gets
    buffered either way — only the *live* subscriber notification for a
    record produced from inside another notification is skipped, which is
    exactly the record that would otherwise start the cycle."""
    activity_log.install()
    calls = []

    def _logs_during_its_own_callback(entry):
        calls.append(entry)
        logging.getLogger("t").warning("re-entrant warning from a subscriber")

    activity_log.subscribe(_logs_during_its_own_callback)
    logging.getLogger("t").info("trigger")  # must not raise/recurse

    assert len(calls) == 1  # the re-entrant warning did NOT also notify subscribers
    messages = [e["message"] for e in activity_log.recent()]
    assert messages == ["trigger", "re-entrant warning from a subscriber"]
