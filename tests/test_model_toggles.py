"""bot.db's model_toggles table + bot.models' filtering — sparse
per-model on/off state for the custom_model/native_agent provider
families. Only an explicit override is ever stored (absence means
enabled); live_custom_models() is the one choke point every model
picker in the app flows through, so filtering there is what makes
toggles actually respected everywhere, not just displayed.
"""

from __future__ import annotations

import asyncio

from bot import db
from bot import models as models_module


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- db.py


def test_set_and_list_model_toggle(temp_db):
    db.set_model_toggle("openrouter", "free-model:free", True)
    db.set_model_toggle("openrouter", "paid-model", False)

    rows = db.list_model_toggles("openrouter")

    by_id = {r["model_id"]: bool(r["enabled"]) for r in rows}
    assert by_id == {"free-model:free": True, "paid-model": False}


def test_set_model_toggle_upserts(temp_db):
    db.set_model_toggle("ollama", "llama3.1", False)
    db.set_model_toggle("ollama", "llama3.1", True)

    rows = db.list_model_toggles("ollama")
    assert len(rows) == 1
    assert bool(rows[0]["enabled"]) is True


def test_disabled_model_ids_only_returns_explicitly_disabled(temp_db):
    db.set_model_toggle("openrouter", "a", True)
    db.set_model_toggle("openrouter", "b", False)
    db.set_model_toggle("openrouter", "c", False)

    assert db.disabled_model_ids("openrouter") == {"b", "c"}


def test_disabled_model_ids_scoped_per_provider(temp_db):
    db.set_model_toggle("providerA", "x", False)
    db.set_model_toggle("providerB", "x", False)

    assert db.disabled_model_ids("providerA") == {"x"}
    assert db.disabled_model_ids("providerB") == {"x"}
    assert db.disabled_model_ids("providerC") == set()


def test_list_model_toggles_no_filter_returns_everything(temp_db):
    db.set_model_toggle("p1", "a", False)
    db.set_model_toggle("p2", "b", False)

    assert len(db.list_model_toggles()) == 2


# ------------------------------------------------------- models.py filtering


def _patch_live_custom_models_source(monkeypatch, models_by_provider):
    from bot import providers as provider_registry

    monkeypatch.setattr(provider_registry, "list_providers", lambda: {
        name: {"base_url": f"http://x/{name}"} for name in models_by_provider
    })

    async def fake_fetch(name, entry, registry):
        return models_by_provider.get(name)

    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch)
    models_module._custom_cache.clear()


def test_live_custom_models_drops_disabled_ids(temp_db, monkeypatch):
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["a", "b", "c"]})
    db.set_model_toggle("openrouter", "b", False)

    grouped = _run(models_module.live_custom_models())

    assert grouped == {"openrouter": ["a", "c"]}


def test_live_custom_models_omits_provider_left_with_nothing(temp_db, monkeypatch):
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["only-model"]})
    db.set_model_toggle("openrouter", "only-model", False)

    grouped = _run(models_module.live_custom_models())

    assert grouped is None


def test_live_custom_models_unaffected_when_nothing_disabled(temp_db, monkeypatch):
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["a", "b"]})

    grouped = _run(models_module.live_custom_models())

    assert grouped == {"openrouter": ["a", "b"]}


def test_toggle_takes_effect_immediately_even_with_warm_cache(temp_db, monkeypatch):
    # The cache stores the UNFILTERED fetch result — a toggle flipped
    # after the cache is warm must still apply without needing the cache
    # itself invalidated.
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["a", "b"]})
    _run(models_module.live_custom_models())  # warms the cache

    db.set_model_toggle("openrouter", "b", False)
    grouped = _run(models_module.live_custom_models())

    assert grouped == {"openrouter": ["a"]}
