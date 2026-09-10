"""Knowledge Modules (bot/support_bot/knowledge_modules.py,
bot/support_bot/module_manifest.py) — the static intent-grouping registry
and its runtime enabled/disabled state, Phase 1 of the Support Bot NLU
next-generation upgrade plan.
"""
from __future__ import annotations

from bot.support_bot import knowledge_modules, module_manifest
from bot.support_bot.training_data import EXAMPLES


def test_every_training_data_intent_is_in_exactly_one_module():
    training_intents = {intent for _, intent in EXAMPLES}
    registry_intents = knowledge_modules.all_intents()

    missing = training_intents - registry_intents
    assert not missing, f"intent(s) in training_data.py but not assigned to a Knowledge Module: {sorted(missing)}"

    stale = registry_intents - training_intents
    assert not stale, f"intent(s) in a Knowledge Module that no longer exist in training_data.py: {sorted(stale)}"


def test_no_intent_appears_in_more_than_one_module():
    seen: dict[str, str] = {}
    for spec in knowledge_modules.MODULE_REGISTRY.values():
        for intent in spec.intents:
            assert intent not in seen, f"intent {intent!r} is in both {seen[intent]!r} and {spec.module_id!r}"
            seen[intent] = spec.module_id


def test_core_status_is_not_unloadable():
    assert knowledge_modules.MODULE_REGISTRY["core_status"].unloadable is False


def test_module_for_intent_and_intents_for_module_round_trip():
    assert knowledge_modules.module_for_intent("bot_create") == "bots"
    assert knowledge_modules.module_for_intent("estop_engage") == "estop"
    assert knowledge_modules.module_for_intent("not_a_real_intent") is None
    assert "bot_create" in knowledge_modules.intents_for_module("bots")
    assert knowledge_modules.intents_for_module("not_a_real_module") == ()


def test_manifest_defaults_every_module_to_enabled(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = module_manifest.load_manifest(path=path)
    assert set(manifest.keys()) == set(knowledge_modules.all_module_ids())
    assert all(entry["enabled"] for entry in manifest.values())
    assert all(entry["version"] is None for entry in manifest.values())


def test_set_enabled_persists_and_round_trips(tmp_path):
    path = tmp_path / "manifest.json"
    module_manifest.set_enabled("bots", False, path=path)
    assert module_manifest.is_enabled("bots", path=path) is False
    assert "bots" not in module_manifest.enabled_module_ids(path=path)

    module_manifest.set_enabled("bots", True, path=path)
    assert module_manifest.is_enabled("bots", path=path) is True
    assert "bots" in module_manifest.enabled_module_ids(path=path)


def test_core_status_cannot_be_disabled(tmp_path):
    path = tmp_path / "manifest.json"
    import pytest

    with pytest.raises(ValueError):
        module_manifest.set_enabled("core_status", False, path=path)
    assert module_manifest.is_enabled("core_status", path=path) is True


def test_set_enabled_rejects_unknown_module(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        module_manifest.set_enabled("not_a_real_module", False, path=tmp_path / "manifest.json")


def test_set_version_persists(tmp_path):
    path = tmp_path / "manifest.json"
    module_manifest.set_version("bots", "sha256:abc123", path=path)
    manifest = module_manifest.load_manifest(path=path)
    assert manifest["bots"]["version"] == "sha256:abc123"
    # every other module is untouched
    assert manifest["mcp"]["version"] is None


def test_manifest_survives_being_reloaded_from_a_partial_file(tmp_path):
    """A manifest written before a new module existed in the registry
    must not crash load_manifest() — the new module just gets the usual
    enabled-by-default treatment."""
    path = tmp_path / "manifest.json"
    module_manifest._save_raw({"bots": {"enabled": False, "version": "v1", "updated_at": "x"}}, path)
    manifest = module_manifest.load_manifest(path=path)
    assert manifest["bots"]["enabled"] is False
    assert manifest["mcp"]["enabled"] is True  # never written, defaults applied


def test_add_support_bot_phrase_stores_module_id(temp_db):
    from bot import db

    phrase_id = db.add_support_bot_phrase("test phrase", "bot_create", module_id="bots")
    rows = db.list_support_bot_phrases()
    row = next(r for r in rows if r["id"] == phrase_id)
    assert row["module_id"] == "bots"


def test_add_support_bot_phrase_module_id_is_optional(temp_db):
    from bot import db

    phrase_id = db.add_support_bot_phrase("another phrase", "help")
    rows = db.list_support_bot_phrases()
    row = next(r for r in rows if r["id"] == phrase_id)
    assert row["module_id"] is None
