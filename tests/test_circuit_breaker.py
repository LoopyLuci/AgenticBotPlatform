"""Router's per-instance circuit breaker: a bot instance whose backend
fails CIRCUIT_OPEN_THRESHOLD times in a row must stop being retried
immediately (no more hammering a backend already known to be failing)
until CIRCUIT_COOLDOWN_S has passed, then get exactly one trial call.

Uses plain asyncio.run() rather than pytest-asyncio (not a project
dependency) — Router.ask() is the only async surface under test here.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from bot.backends.base import BackendError, BackendResult
from bot.config import config
from bot.router import CIRCUIT_OPEN_THRESHOLD, CircuitOpenError, Router, _CircuitState


@pytest.fixture
def router_with_failing_backend(monkeypatch, temp_db):
    monkeypatch.setattr(config, "_data", {"default_backend": "cli", "action_overrides": {}, "timeouts": {}})
    monkeypatch.setattr("bot.setup_wizard.check_backend_ready", lambda name: (True, ""))

    r = Router()

    class _FailingBackend:
        async def ask(self, prompt, *, context=None, timeout_s=30):
            raise BackendError("simulated backend failure")

    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, model_override=None: _FailingBackend())
    return r


def _ask(router, **kw):
    return asyncio.run(router.ask("hi", **kw))


def test_breaker_stays_closed_below_threshold(router_with_failing_backend):
    r = router_with_failing_backend
    for _ in range(CIRCUIT_OPEN_THRESHOLD - 1):
        with pytest.raises(BackendError):
            _ask(r, instance_id=1)
    status = r.circuit_status(1)
    assert status["open"] is False
    assert status["consecutive_failures"] == CIRCUIT_OPEN_THRESHOLD - 1


def test_breaker_opens_at_threshold_and_rejects_without_calling_the_backend(router_with_failing_backend, monkeypatch):
    r = router_with_failing_backend
    for _ in range(CIRCUIT_OPEN_THRESHOLD):
        with pytest.raises(BackendError):
            _ask(r, instance_id=1)
    assert r.circuit_status(1)["open"] is True

    # Once open, further calls must fail fast as CircuitOpenError without
    # ever reaching the (still-failing, but irrelevant now) backend.
    called = {"n": 0}

    class _ShouldNeverBeCalled:
        async def ask(self, *a, **kw):
            called["n"] += 1
            raise BackendError("should not have been reached")

    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, model_override=None: _ShouldNeverBeCalled())
    with pytest.raises(CircuitOpenError):
        _ask(r, instance_id=1)
    assert called["n"] == 0


def test_a_success_resets_the_breaker(router_with_failing_backend, monkeypatch):
    r = router_with_failing_backend
    for _ in range(CIRCUIT_OPEN_THRESHOLD - 1):
        with pytest.raises(BackendError):
            _ask(r, instance_id=1)
    assert r.circuit_status(1)["consecutive_failures"] == CIRCUIT_OPEN_THRESHOLD - 1

    class _WorkingBackend:
        async def ask(self, prompt, *, context=None, timeout_s=30):
            return BackendResult(text="ok", tokens=1, raw={})

    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, model_override=None: _WorkingBackend())
    result = _ask(r, instance_id=1)
    assert result.text == "ok"
    assert r.circuit_status(1) == {"open": False, "consecutive_failures": 0}


def test_a_different_instance_is_unaffected(router_with_failing_backend):
    r = router_with_failing_backend
    for _ in range(CIRCUIT_OPEN_THRESHOLD):
        with pytest.raises(BackendError):
            _ask(r, instance_id=1)
    assert r.circuit_status(1)["open"] is True
    assert r.circuit_status(2) == {"open": False, "consecutive_failures": 0}


def test_manual_reset_closes_an_open_breaker(router_with_failing_backend):
    r = router_with_failing_backend
    r._circuits[1] = _CircuitState(consecutive_failures=CIRCUIT_OPEN_THRESHOLD, opened_at=time.monotonic())
    assert r.circuit_status(1)["open"] is True
    r.reset_circuit(1)
    assert r.circuit_status(1) == {"open": False, "consecutive_failures": 0}


def test_cooldown_elapsed_allows_one_half_open_trial(router_with_failing_backend, monkeypatch):
    from bot.router import CIRCUIT_COOLDOWN_S

    r = router_with_failing_backend
    # Force the breaker into an already-open state whose cooldown has
    # already elapsed. time.monotonic()'s zero-point is platform/uptime
    # dependent (can be small on a freshly-booted CI runner) — offsetting
    # from the current value, not hardcoding 0.0, is what makes "far
    # enough in the past" true on every machine, not just this one.
    r._circuits[1] = _CircuitState(
        consecutive_failures=CIRCUIT_OPEN_THRESHOLD,
        opened_at=time.monotonic() - CIRCUIT_COOLDOWN_S - 10,
    )

    class _WorkingBackend:
        async def ask(self, prompt, *, context=None, timeout_s=30):
            return BackendResult(text="recovered", tokens=1, raw={})

    monkeypatch.setattr(r, "_get_backend", lambda name, cfg, model_override=None: _WorkingBackend())
    result = _ask(r, instance_id=1)
    assert result.text == "recovered"
    assert r.circuit_status(1) == {"open": False, "consecutive_failures": 0}
