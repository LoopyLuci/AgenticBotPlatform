"""bot/main.py's sys.excepthook/threading.excepthook replacements — the
one class of error every other logging path in this codebase misses: a
genuinely unhandled exception that unwinds a whole thread (main or
otherwise). Every other error path already flows through logging (a
normal try/except calling logger.exception, or _handle_asyncio_exception
for an asyncio task's own unhandled exception) and therefore already
reaches both logs/bot.log and the Activity tab; this is specifically
about the case that wouldn't otherwise.
"""
from __future__ import annotations

import logging
import threading

from bot import main as bot_main


def _make_exc_info(message: str):
    try:
        raise RuntimeError(message)
    except RuntimeError:
        import sys

        return sys.exc_info()


def test_log_uncaught_exception_logs_via_bot_uncaught_logger(caplog, monkeypatch):
    monkeypatch.setattr(bot_main.sys, "__excepthook__", lambda *a: None)
    exc_type, exc_value, exc_tb = _make_exc_info("boom at the top level")

    with caplog.at_level("CRITICAL", logger="bot.uncaught"):
        bot_main._log_uncaught_exception(exc_type, exc_value, exc_tb)

    assert any("unhandled exception reached the top level" in r.message for r in caplog.records)
    assert any("boom at the top level" in (r.exc_text or "") for r in caplog.records if r.exc_info)


def test_log_uncaught_exception_still_calls_the_real_default_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(bot_main.sys, "__excepthook__", lambda *a: calls.append(a))
    exc_type, exc_value, exc_tb = _make_exc_info("still prints to stderr too")

    bot_main._log_uncaught_exception(exc_type, exc_value, exc_tb)

    assert len(calls) == 1
    assert calls[0][0] is exc_type


def test_log_uncaught_exception_passes_keyboardinterrupt_through_untouched(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(bot_main.sys, "__excepthook__", lambda *a: calls.append(a))
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        import sys

        exc_type, exc_value, exc_tb = sys.exc_info()

    with caplog.at_level("CRITICAL", logger="bot.uncaught"):
        bot_main._log_uncaught_exception(exc_type, exc_value, exc_tb)

    assert len(calls) == 1  # still forwarded to the real default hook
    assert not caplog.records  # but never logged as a crash — it's normal shutdown


def test_log_uncaught_thread_exception_logs_the_thread_name(caplog, monkeypatch):
    monkeypatch.setattr(bot_main.threading, "__excepthook__", lambda args: None)
    exc_type, exc_value, exc_tb = _make_exc_info("boom in a worker thread")
    fake_thread = threading.Thread(name="my-worker-thread")
    args = threading.ExceptHookArgs((exc_type, exc_value, exc_tb, fake_thread))

    with caplog.at_level("CRITICAL", logger="bot.uncaught"):
        bot_main._log_uncaught_thread_exception(args)

    assert any("my-worker-thread" in r.message for r in caplog.records)


def test_setup_logging_installs_both_hooks(monkeypatch):
    # Isolated from the real root logger's handlers/level so this doesn't
    # leak into other tests — mirrors test_activity_log.py's own pattern.
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    old_excepthook = bot_main.sys.excepthook
    old_thread_hook = bot_main.threading.excepthook
    try:
        bot_main.setup_logging()
        assert bot_main.sys.excepthook is bot_main._log_uncaught_exception
        assert bot_main.threading.excepthook is bot_main._log_uncaught_thread_exception
    finally:
        bot_main.sys.excepthook = old_excepthook
        bot_main.threading.excepthook = old_thread_hook
        for h in list(root.handlers):
            if h not in old_handlers:
                root.removeHandler(h)
        root.setLevel(old_level)
        from bot import activity_log

        if activity_log._handler is not None:
            root.removeHandler(activity_log._handler)
        activity_log._handler = None
