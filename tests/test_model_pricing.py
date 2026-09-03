"""bot/model_pricing.py — real per-model pricing for the custom_model/
native_agent family, sourced from models.dev's public catalog (a real
external endpoint, confirmed live during design: a flat {provider_id:
{models: {model_id: {"cost": {"input": $/1M, "output": $/1M}}}}} shape).
Faked at the httpx boundary here so these stay fast/offline, matching
test_hermes_model_discovery.py's established pattern for this same kind
of live-then-cache-fallback function.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot import model_pricing


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_memory_cache():
    model_pricing._memory_cache["data"] = None
    model_pricing._memory_cache["fetched_at"] = 0.0
    yield
    model_pricing._memory_cache["data"] = None
    model_pricing._memory_cache["fetched_at"] = 0.0


class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, data=None, raises=None):
        self._data = data
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if self._raises:
            raise self._raises
        return _FakeResponse(self._data)


_CATALOG = {
    "openrouter": {
        "models": {
            "free-model:free": {"cost": {"input": 0, "output": 0}},
            "paid-model": {"cost": {"input": 5.0, "output": 25.0}},
            "no-cost-info": {},
        }
    }
}


def test_live_fetch_marks_free_and_paid_correctly(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG))

    free_pricing, source = _run(model_pricing.get_pricing("openrouter", "free-model:free"))
    assert source == "live"
    assert free_pricing == {"free": True, "input": 0.0, "output": 0.0}

    paid_pricing, source = _run(model_pricing.get_pricing("openrouter", "paid-model"))
    assert source == "live"
    assert paid_pricing["free"] is False
    # Converted from $/million to $/token.
    assert paid_pricing["input"] == pytest.approx(5.0 / 1_000_000)
    assert paid_pricing["output"] == pytest.approx(25.0 / 1_000_000)


def test_unknown_model_or_provider_returns_none_pricing(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG))

    pricing, source = _run(model_pricing.get_pricing("openrouter", "does-not-exist"))
    assert pricing is None
    assert source == "live"

    pricing, source = _run(model_pricing.get_pricing("no-such-provider", "x"))
    assert pricing is None


def test_model_with_no_cost_field_returns_none_pricing(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG))

    pricing, source = _run(model_pricing.get_pricing("openrouter", "no-cost-info"))
    assert pricing is None
    assert source == "live"


def test_live_fetch_writes_disk_cache_and_fetch_failure_falls_back_to_it(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(model_pricing, "CACHE_PATH", cache_path)
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG))

    _run(model_pricing.get_pricing("openrouter", "paid-model"))
    assert cache_path.is_file()
    assert json.loads(cache_path.read_text(encoding="utf-8")) == _CATALOG

    # A later call, with the live fetch now failing and the in-memory
    # cache cleared, must fall back to the disk cache rather than
    # returning nothing.
    model_pricing._memory_cache["data"] = None
    model_pricing._memory_cache["fetched_at"] = 0.0
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(raises=ConnectionError("down")))

    pricing, source = _run(model_pricing.get_pricing("openrouter", "paid-model"))
    assert source == "cache_fallback"
    assert pricing["free"] is False


def test_unavailable_when_no_live_and_no_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "does-not-exist.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(raises=ConnectionError("down")))

    pricing, source = _run(model_pricing.get_pricing("openrouter", "paid-model"))
    assert pricing is None
    assert source == "unavailable"


def test_memory_cache_avoids_refetch_within_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    calls = []

    def _factory(**k):
        calls.append(1)
        return _FakeAsyncClient(data=_CATALOG)

    monkeypatch.setattr("httpx.AsyncClient", _factory)

    _run(model_pricing.get_pricing("openrouter", "paid-model"))
    _run(model_pricing.get_pricing("openrouter", "free-model:free"))
    assert len(calls) == 1


def test_refresh_forces_a_new_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    calls = []

    def _factory(**k):
        calls.append(1)
        return _FakeAsyncClient(data=_CATALOG)

    monkeypatch.setattr("httpx.AsyncClient", _factory)

    _run(model_pricing.get_pricing("openrouter", "paid-model"))
    _run(model_pricing.get_pricing("openrouter", "paid-model", refresh=True))
    assert len(calls) == 2
