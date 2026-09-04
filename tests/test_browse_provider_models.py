"""bot.models.browse_provider_models() — the Models page's own data
source: unlike live_custom_models()/custom_models_with_pricing(), this
does NOT filter out disabled models (the whole point is showing them so
they can be re-enabled), and combines models.dev's static catalog (via
a configured provider's stored catalog_id) with a live /models fetch.
"""

from __future__ import annotations

import asyncio

from bot import db
from bot import models as models_module
from bot import providers


def _run(coro):
    return asyncio.run(coro)


def _configure_provider(monkeypatch, name, entry):
    monkeypatch.setattr(providers, "list_providers", lambda: {name: entry})
    monkeypatch.setattr(providers, "get_provider", lambda n: entry if n == name else None)


def test_unconfigured_provider_returns_empty(temp_db):
    assert _run(models_module.browse_provider_models("nope")) == []


def test_combines_static_catalog_with_toggle_state(temp_db, monkeypatch):
    _configure_provider(monkeypatch, "openrouter", {"base_url": "https://openrouter.ai/api/v1", "catalog_id": "openrouter"})

    async def fake_list_models_for_provider(catalog_id, refresh=False):
        assert catalog_id == "openrouter"
        return [
            {"id": "free-model:free", "free": True, "input": 0.0, "output": 0.0},
            {"id": "paid-model", "free": False, "input": 1e-6, "output": 2e-6},
        ]

    async def fake_fetch_custom_models(name, entry, registry):
        return None  # live endpoint unreachable — static catalog still shows

    monkeypatch.setattr("bot.model_pricing.list_models_for_provider", fake_list_models_for_provider)
    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch_custom_models)
    db.set_model_toggle("openrouter", "paid-model", False)

    result = _run(models_module.browse_provider_models("openrouter"))

    by_id = {m["id"]: m for m in result}
    assert by_id["free-model:free"]["enabled"] is True
    assert by_id["paid-model"]["enabled"] is False
    # Free-first ordering.
    assert result[0]["id"] == "free-model:free"


def test_paid_model_defaults_disabled_without_explicit_toggle(temp_db, monkeypatch):
    _configure_provider(monkeypatch, "openrouter", {"base_url": "https://openrouter.ai/api/v1", "catalog_id": "openrouter"})

    async def fake_list_models_for_provider(catalog_id, refresh=False):
        return [
            {"id": "free-model:free", "free": True, "input": 0.0, "output": 0.0},
            {"id": "paid-model", "free": False, "input": 1e-6, "output": 2e-6},
            {"id": "unpriced-model", "free": None, "input": None, "output": None},
        ]

    async def fake_fetch_custom_models(name, entry, registry):
        return None

    monkeypatch.setattr("bot.model_pricing.list_models_for_provider", fake_list_models_for_provider)
    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch_custom_models)

    result = _run(models_module.browse_provider_models("openrouter"))

    by_id = {m["id"]: m for m in result}
    assert by_id["free-model:free"]["enabled"] is True
    assert by_id["paid-model"]["enabled"] is False
    assert by_id["unpriced-model"]["enabled"] is False


def test_live_only_models_appear_for_provider_with_no_catalog_id(temp_db, monkeypatch):
    _configure_provider(monkeypatch, "my_local", {"base_url": "http://127.0.0.1:11434/v1"})

    async def fake_fetch_custom_models(name, entry, registry):
        return ["llama3.1", "mistral"]

    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch_custom_models)

    result = _run(models_module.browse_provider_models("my_local"))

    ids = {m["id"] for m in result}
    assert ids == {"llama3.1", "mistral"}


def test_local_endpoint_unpriced_model_treated_as_free(temp_db, monkeypatch):
    _configure_provider(monkeypatch, "my_local", {"base_url": "http://127.0.0.1:11434/v1"})

    async def fake_fetch_custom_models(name, entry, registry):
        return ["llama3.1"]

    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch_custom_models)

    result = _run(models_module.browse_provider_models("my_local"))

    assert result[0]["free"] is True


def test_non_local_endpoint_unpriced_model_is_unpriced_not_free(temp_db, monkeypatch):
    _configure_provider(monkeypatch, "cloud_thing", {"base_url": "https://api.example.com/v1"})

    async def fake_fetch_custom_models(name, entry, registry):
        return ["mystery-model"]

    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch_custom_models)

    result = _run(models_module.browse_provider_models("cloud_thing"))

    assert result[0]["free"] is None


def test_sort_order_free_then_unpriced_then_paid():
    entries = [
        {"id": "z-paid", "free": False},
        {"id": "a-free", "free": True},
        {"id": "m-unpriced", "free": None},
        {"id": "b-free", "free": True},
    ]
    sorted_entries = models_module._sort_browse_entries(entries)
    assert [e["id"] for e in sorted_entries] == ["a-free", "b-free", "m-unpriced", "z-paid"]


def test_is_local_base_url():
    assert models_module._is_local_base_url("http://127.0.0.1:11434/v1") is True
    assert models_module._is_local_base_url("http://localhost:1234/v1") is True
    assert models_module._is_local_base_url("http://192.168.1.50:11434/v1") is True
    assert models_module._is_local_base_url("https://openrouter.ai/api/v1") is False
    assert models_module._is_local_base_url("not a url") is False
