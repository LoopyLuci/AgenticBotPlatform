"""bot.models.live_custom_models() — a persistently-unreachable custom
provider (Ollama not running, a typo'd base_url, ...) used to log a fresh
WARNING every time its 300s cache entry expired, forever, with no
backoff — reported live as the same three warnings repeating every
20-30s while the app was under active use. Failures now back off
(300s, 600s, 1200s, ... capped at 1h) and only the FIRST failure in a
streak logs at WARNING; a still-failing retry logs at DEBUG instead.
"""
from __future__ import annotations

import asyncio
import logging

from bot import models as models_module


def _run(coro):
    return asyncio.run(coro)


def _fake_providers(monkeypatch, entry=None):
    from bot import providers as provider_registry

    monkeypatch.setattr(provider_registry, "list_providers", lambda: {"flaky": entry or {"base_url": "http://x"}})
    return provider_registry


def test_a_second_failure_within_backoff_window_does_not_refetch(monkeypatch):
    _fake_providers(monkeypatch)
    monkeypatch.setattr(models_module, "_custom_cache", {})
    calls = {"n": 0}

    async def fake_fetch(name, entry, provider_registry, warn=True):
        calls["n"] += 1
        return None

    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch)

    _run(models_module.live_custom_models())
    _run(models_module.live_custom_models())

    assert calls["n"] == 1  # second call landed inside the (backed-off) cache window


def test_only_the_first_failure_in_a_streak_warns(monkeypatch, caplog):
    _fake_providers(monkeypatch)
    monkeypatch.setattr(models_module, "_custom_cache", {})
    # Force every call past the cache window regardless of backoff, so
    # each of these represents a genuinely new fetch attempt.
    monkeypatch.setattr(models_module, "_CUSTOM_CACHE_TTL_S", 0.0)
    monkeypatch.setattr(models_module, "_CUSTOM_CACHE_MAX_TTL_S", 0.0)

    # Exercise the REAL _fetch_custom_models so its own warn-level logic runs.
    class _FakeAsyncClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw): raise RuntimeError("connection refused")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    from bot import providers as provider_registry
    monkeypatch.setattr(provider_registry, "get_api_key", lambda name: None)

    with caplog.at_level(logging.DEBUG, logger="bot.models"):
        _run(models_module.live_custom_models())
        caplog.clear()
        _run(models_module.live_custom_models())
        second_call_records = list(caplog.records)

    levels = [r.levelno for r in second_call_records if "flaky" in r.getMessage()]
    assert levels == [logging.DEBUG]  # the second-in-a-row failure did NOT warn again


def test_backoff_grows_and_is_capped(monkeypatch):
    _fake_providers(monkeypatch)
    monkeypatch.setattr(models_module, "_custom_cache", {"flaky": {"at": 0.0, "models": None, "failures": 10}})
    monkeypatch.setattr(models_module, "time", __import__("time"))

    async def fake_fetch(name, entry, provider_registry, warn=True):
        return None

    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch)

    # With 10 prior failures, effective TTL should be capped at
    # _CUSTOM_CACHE_MAX_TTL_S, not 300 * 2**10 (~5 days) — the cached
    # (very old, "at": 0.0) entry must still be considered stale enough
    # to eventually retry rather than being backed off forever.
    calls = {"n": 0}

    async def counting_fetch(name, entry, provider_registry, warn=True):
        calls["n"] += 1
        return None

    monkeypatch.setattr(models_module, "_fetch_custom_models", counting_fetch)
    _run(models_module.live_custom_models())
    assert calls["n"] == 1  # "at": 0.0 is older than any capped TTL, so this DOES refetch
