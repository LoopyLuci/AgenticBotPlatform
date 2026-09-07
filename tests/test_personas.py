"""bot/personas.py — the persona-preset registry. Mostly static data, but
worth a quick sanity check per preset (matches the shape list_personas()/
the dashboard's picker relies on) so a future edit can't silently leave a
preset malformed.
"""

from __future__ import annotations

from bot import personas


def test_every_preset_has_the_expected_shape():
    for key, preset in personas.PERSONA_PRESETS.items():
        assert isinstance(preset.get("label"), str) and preset["label"]
        assert isinstance(preset.get("icon"), str) and preset["icon"]
        assert isinstance(preset.get("description"), str) and preset["description"]
        assert isinstance(preset.get("instructions"), str)


def test_enthusiastic_preset_is_model_agnostic_instructions_not_a_code_path():
    """The whole point: this personality has to work by injecting real
    instructions text (custom_instructions), not by depending on any one
    model's own built-in tone — confirmed live against two different
    free models behind the same bot instance."""
    preset = personas.PERSONA_PRESETS["enthusiastic"]
    assert "kaomoji" in preset["instructions"].lower()
    assert preset["instructions"].strip()


def test_is_known_and_list_personas_include_enthusiastic():
    assert personas.is_known("enthusiastic")
    ids = {p["id"] for p in personas.list_personas()}
    assert "enthusiastic" in ids


def test_auto_orchestrator_preset_mentions_both_native_and_hermes_paths():
    """Must cover both real dispatch paths' actual mechanism — spawn_subagent's
    per-task overrides (native) and configure_delegation-before-delegate_task
    (Hermes, since delegate_task itself has no per-call override, confirmed
    against its real source) — not just one of them."""
    instructions = personas.PERSONA_PRESETS["auto_orchestrator"]["instructions"]
    assert "spawn_subagent" in instructions
    assert "configure_delegation" in instructions
    assert "delegate_task" in instructions


def test_is_known_includes_auto_orchestrator():
    assert personas.is_known("auto_orchestrator")
    ids = {p["id"] for p in personas.list_personas()}
    assert "auto_orchestrator" in ids


class TestIsManagerLike:
    def test_manager_and_auto_orchestrator_are_manager_like(self):
        assert personas.is_manager_like("manager")
        assert personas.is_manager_like("auto_orchestrator")

    def test_other_personas_are_not(self):
        assert not personas.is_manager_like("assistant")
        assert not personas.is_manager_like("coder")
        assert not personas.is_manager_like(None)
