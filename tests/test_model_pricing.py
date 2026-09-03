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


# --------------------------------------------------- catalog/provider browsing

_CATALOG_WITH_PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter", "api": "https://openrouter.ai/api/v1", "env": ["OPENROUTER_API_KEY"],
        "models": {
            "free-model:free": {"cost": {"input": 0, "output": 0}},
            "paid-model": {"cost": {"input": 5.0, "output": 25.0}},
            "no-cost-info": {},
        },
    },
    "anthropic": {
        "name": "Anthropic", "env": ["ANTHROPIC_API_KEY"],
        # No "api" field — a native-SDK provider, not OpenAI-compatible.
        "models": {"claude-opus-4-7": {"cost": {"input": 5, "output": 25}}},
    },
    "lmstudio": {
        "name": "LM Studio", "api": "http://127.0.0.1:1234/v1", "env": ["LMSTUDIO_API_KEY"],
        "models": {},
    },
}


def test_list_known_providers_excludes_entries_with_no_api_field(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG_WITH_PROVIDERS))

    known = _run(model_pricing.list_known_providers())

    ids = {p["id"] for p in known}
    assert ids == {"openrouter", "lmstudio"}
    assert "anthropic" not in ids


def test_list_known_providers_shape_and_sort_order(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG_WITH_PROVIDERS))

    known = _run(model_pricing.list_known_providers())

    assert [p["name"] for p in known] == ["LM Studio", "OpenRouter"]  # alphabetical by name
    lmstudio = next(p for p in known if p["id"] == "lmstudio")
    assert lmstudio == {"id": "lmstudio", "name": "LM Studio", "api": "http://127.0.0.1:1234/v1", "env": ["LMSTUDIO_API_KEY"]}


def test_list_known_providers_empty_when_catalog_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "does-not-exist.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(raises=ConnectionError("down")))

    assert _run(model_pricing.list_known_providers()) == []


def test_list_models_for_provider_returns_full_catalog_including_unpriced(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG_WITH_PROVIDERS))

    models = _run(model_pricing.list_models_for_provider("openrouter"))

    by_id = {m["id"]: m for m in models}
    assert by_id["free-model:free"]["free"] is True
    assert by_id["paid-model"]["free"] is False
    assert by_id["paid-model"]["input"] == pytest.approx(5.0 / 1_000_000)
    assert by_id["no-cost-info"]["free"] is None
    assert by_id["no-cost-info"]["input"] is None


def test_list_models_for_provider_unknown_id_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(model_pricing, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("httpx.AsyncClient", lambda **k: _FakeAsyncClient(data=_CATALOG_WITH_PROVIDERS))

    assert _run(model_pricing.list_models_for_provider("does-not-exist")) == []
