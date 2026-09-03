"""bot.models.custom_models_with_pricing() — closes the real gap where
the custom_model/native_agent family had zero pricing/free data
(live_custom_models() only ever returned bare model ids). Fakes
live_custom_models() and bot.model_pricing.get_pricing() directly since
each already has its own dedicated, more detailed test coverage
(test_model_pricing.py) — this only needs to prove the two are combined
correctly.
"""

from __future__ import annotations

import asyncio

from bot import models as models_module


def _run(coro):
    return asyncio.run(coro)


def test_combines_model_ids_with_pricing(monkeypatch):
    async def fake_live_custom_models():
        return {"openrouter": ["free-model:free", "paid-model", "unpriced-model"]}

    async def fake_get_pricing(provider, model_id, refresh=False):
        if model_id == "free-model:free":
            return {"free": True, "input": 0.0, "output": 0.0}, "live"
        if model_id == "paid-model":
            return {"free": False, "input": 1e-6, "output": 2e-6}, "live"
        return None, "live"

    monkeypatch.setattr(models_module, "live_custom_models", fake_live_custom_models)
    from bot import model_pricing

    monkeypatch.setattr(model_pricing, "get_pricing", fake_get_pricing)

    grouped, source = _run(models_module.custom_models_with_pricing())

    assert source == "live"
    entries = {e["id"]: e for e in grouped["openrouter"]}
    assert entries["free-model:free"]["free"] is True
    assert entries["paid-model"]["free"] is False
    assert entries["unpriced-model"] == {"id": "unpriced-model", "free": False, "input": None, "output": None}


def test_unavailable_when_no_custom_models_configured(monkeypatch):
    async def fake_live_custom_models():
        return None

    monkeypatch.setattr(models_module, "live_custom_models", fake_live_custom_models)

    grouped, source = _run(models_module.custom_models_with_pricing())

    assert grouped == {}
    assert source == "unavailable"


def test_source_reflects_worst_of_the_per_model_lookups(monkeypatch):
    async def fake_live_custom_models():
        return {"ollama": ["model-a", "model-b"]}

    async def fake_get_pricing(provider, model_id, refresh=False):
        if model_id == "model-a":
            return {"free": True, "input": 0.0, "output": 0.0}, "live"
        return None, "unavailable"

    monkeypatch.setattr(models_module, "live_custom_models", fake_live_custom_models)
    from bot import model_pricing

    monkeypatch.setattr(model_pricing, "get_pricing", fake_get_pricing)

    _, source = _run(models_module.custom_models_with_pricing())

    assert source == "unavailable"
