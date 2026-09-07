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
