"""HermesGatewayBackend.ask()'s retry path — confirmed live via a real
Telegram message that a persisted desktop_session_key can outlive the
actual Hermes session it names (its gateway process died; Hermes's
session store is in-memory/lazy, confirmed against the real gateway
source, so nothing survives for a new process to recognize). The old
retry blindly reused the same stale key and failed identically twice —
this locks in the fix: detecting "session not found" and forcing a
fresh session on the retry.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.backends.base import BackendError
from bot.backends.hermes_gateway_backend import HermesGatewayBackend


def _run(coro):
    return asyncio.run(coro)


class _FakeAskOnce:
    """Records every call's session_key and returns queued outcomes in
    order — either a normal (text, session_id, created) result or an
    exception to raise."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    async def __call__(self, prompt, timeout_s, session_key, instance_id, job_id, action_type):
        self.calls.append(session_key)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _backend_with_faked_internals(monkeypatch, ask_once):
    backend = HermesGatewayBackend()
    monkeypatch.setattr(backend, "_ask_once", ask_once)

    torn_down = {"count": 0}

    async def fake_teardown():
        torn_down["count"] += 1

    monkeypatch.setattr(backend, "_teardown_connection", fake_teardown)
    return backend, torn_down


def test_stale_session_not_found_forces_a_fresh_session_on_retry(monkeypatch):
    fake_ask_once = _FakeAskOnce([
        BackendError("hermes prompt.background error: {'code': 4001, 'message': 'session not found'}"),
        ("real reply", "brand-new-session-id", True),
    ])
    backend, torn_down = _backend_with_faked_internals(monkeypatch, fake_ask_once)

    result = _run(backend.ask(
        "hello", context={"instance_id": 43, "desktop_session_key": "stale-session-id"},
    ))

    assert torn_down["count"] == 1
    # First call used the (bad) persisted key; the retry deliberately
    # dropped it to None so _ask_once creates a genuinely new session
    # instead of failing against the same dead one a second time.
    assert fake_ask_once.calls == ["stale-session-id", None]
    assert result.text == "real reply"
    assert result.raw == {"desktop_session_key": "brand-new-session-id"}


def test_unrelated_connection_error_still_retries_with_the_same_session_key(monkeypatch):
    """A real transient failure (dropped WS, timeout) is a completely
    different situation from a stale session — the existing key is
    still perfectly valid, so the retry must keep using it."""
    fake_ask_once = _FakeAskOnce([
        BackendError("failed to connect to hermes gateway: connection reset"),
        ("real reply", "same-session-id", False),
    ])
    backend, torn_down = _backend_with_faked_internals(monkeypatch, fake_ask_once)

    result = _run(backend.ask(
        "hello", context={"instance_id": 43, "desktop_session_key": "same-session-id"},
    ))

    assert fake_ask_once.calls == ["same-session-id", "same-session-id"]
    assert result.text == "real reply"


def test_stale_session_error_with_no_prior_key_is_a_no_op_change(monkeypatch):
    """No session_key was ever passed in the first place (e.g. no linked
    bot instance) — nothing to clear, retry behaves exactly as before."""
    fake_ask_once = _FakeAskOnce([
        BackendError("hermes prompt.background error: {'code': 4001, 'message': 'session not found'}"),
        ("reply", None, False),
    ])
    backend, torn_down = _backend_with_faked_internals(monkeypatch, fake_ask_once)

    result = _run(backend.ask("hello", context={}))

    assert fake_ask_once.calls == [None, None]
    assert result.text == "reply"


def test_both_attempts_failing_raises_a_clear_combined_error(monkeypatch):
    fake_ask_once = _FakeAskOnce([
        BackendError("hermes prompt.background error: {'code': 4001, 'message': 'session not found'}"),
        BackendError("still broken"),
    ])
    backend, _ = _backend_with_faked_internals(monkeypatch, fake_ask_once)

    with pytest.raises(BackendError, match="failed after retry"):
        _run(backend.ask("hello", context={"instance_id": 43, "desktop_session_key": "stale"}))
