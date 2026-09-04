"""bot.db's model_toggles table + bot.models' filtering — per-model
on/off state for the custom_model/native_agent provider families.

Only an EXPLICIT override is ever stored in model_toggles (sparse by
design). Absence of a row falls back to a free/paid-based default,
computed by bot.models._resolve_effective_enabled(): a local endpoint's
models default enabled (free by convention), and any other model
defaults enabled only when models.dev actually reports it as free —
paid or unpriced models default DISABLED, so a freshly-configured
provider with hundreds of models (a real API key added) doesn't
silently expose all of them with no explicit opt-in.

live_custom_models() is the one choke point every model picker in the
app flows through, so filtering there is what makes toggles — and the
free/paid default — actually respected everywhere, not just displayed.
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


def test_bulk_set_model_toggles_upserts_many_in_one_call(temp_db):
    db.bulk_set_model_toggles("openrouter", {"a": False, "b": False, "c": True})

    rows = db.list_model_toggles("openrouter")
    by_id = {r["model_id"]: bool(r["enabled"]) for r in rows}
    assert by_id == {"a": False, "b": False, "c": True}


def test_bulk_set_model_toggles_overwrites_existing_rows(temp_db):
    db.set_model_toggle("openrouter", "a", True)

    db.bulk_set_model_toggles("openrouter", {"a": False})

    assert db.disabled_model_ids("openrouter") == {"a"}


def test_bulk_set_model_toggles_noop_on_empty_updates(temp_db):
    db.bulk_set_model_toggles("openrouter", {})

    assert db.list_model_toggles("openrouter") == []


# ------------------------------------------------------- models.py filtering


def _patch_live_custom_models_source(monkeypatch, models_by_provider, *, base_urls=None):
    from bot import providers as provider_registry

    base_urls = base_urls or {}
    monkeypatch.setattr(provider_registry, "list_providers", lambda: {
        name: {"base_url": base_urls.get(name, f"http://example-{name}.invalid")}
        for name in models_by_provider
    })

    async def fake_fetch(name, entry, registry):
        return models_by_provider.get(name)

    monkeypatch.setattr(models_module, "_fetch_custom_models", fake_fetch)
    models_module._custom_cache.clear()


def _patch_pricing(monkeypatch, free_model_ids):
    """Fake bot.model_pricing.get_pricing: any model id in `free_model_ids`
    is reported free; everything else comes back "unavailable" (None),
    matching the real behavior for a model models.dev doesn't know about."""
    from bot import model_pricing

    async def fake_get_pricing(provider, model_id, refresh=False):
        if model_id in free_model_ids:
            return {"free": True, "input": 0.0, "output": 0.0}, "live"
        return None, "unavailable"

    monkeypatch.setattr(model_pricing, "get_pricing", fake_get_pricing)


def test_live_custom_models_drops_disabled_ids(temp_db, monkeypatch):
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["a", "b", "c"]})
    _patch_pricing(monkeypatch, {"a", "b", "c"})  # all free by pricing, so only the explicit override removes "b"
    db.set_model_toggle("openrouter", "b", False)

    grouped = _run(models_module.live_custom_models())

    assert grouped == {"openrouter": ["a", "c"]}


def test_live_custom_models_omits_provider_left_with_nothing(temp_db, monkeypatch):
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["only-model"]})
    _patch_pricing(monkeypatch, {"only-model"})
    db.set_model_toggle("openrouter", "only-model", False)

    grouped = _run(models_module.live_custom_models())

    assert grouped is None


def test_live_custom_models_free_models_enabled_by_default(temp_db, monkeypatch):
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["a", "b"]})
    _patch_pricing(monkeypatch, {"a", "b"})  # both genuinely free, no explicit override needed

    grouped = _run(models_module.live_custom_models())

    assert grouped == {"openrouter": ["a", "b"]}


def test_live_custom_models_paid_models_disabled_by_default(temp_db, monkeypatch):
    # Neither model is reported free and neither has an explicit
    # override — both must default OFF, closing the gap where adding a
    # real API key would otherwise silently expose every paid model.
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["gpt-5", "claude-tier-1"]})
    _patch_pricing(monkeypatch, set())

    grouped = _run(models_module.live_custom_models())

    assert grouped is None


def test_live_custom_models_explicit_enable_overrides_paid_default(temp_db, monkeypatch):
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["gpt-5", "claude-tier-1"]})
    _patch_pricing(monkeypatch, set())
    db.set_model_toggle("openrouter", "gpt-5", True)

    grouped = _run(models_module.live_custom_models())

    assert grouped == {"openrouter": ["gpt-5"]}


def test_live_custom_models_local_endpoint_models_default_enabled(temp_db, monkeypatch):
    # A loopback/private base_url is treated as free by convention (no
    # real per-token cost) — no pricing call should even be needed.
    _patch_live_custom_models_source(
        monkeypatch, {"local_ollama": ["llama3.1"]}, base_urls={"local_ollama": "http://127.0.0.1:11434/v1"},
    )
    _patch_pricing(monkeypatch, set())

    grouped = _run(models_module.live_custom_models())

    assert grouped == {"local_ollama": ["llama3.1"]}


def test_toggle_takes_effect_immediately_even_with_warm_cache(temp_db, monkeypatch):
    # The cache stores the UNFILTERED fetch result — a toggle flipped
    # after the cache is warm must still apply without needing the cache
    # itself invalidated.
    _patch_live_custom_models_source(monkeypatch, {"openrouter": ["a", "b"]})
    _patch_pricing(monkeypatch, {"a", "b"})
    _run(models_module.live_custom_models())  # warms the cache

    db.set_model_toggle("openrouter", "b", False)
    grouped = _run(models_module.live_custom_models())

    assert grouped == {"openrouter": ["a"]}
