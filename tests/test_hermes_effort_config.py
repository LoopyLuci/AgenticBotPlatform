"""Hermes's own real, already-built reasoning_effort ladder (confirmed
against the installed Hermes Agent source) — bot/hermes_config.py just
reads/writes the two config keys (delegation.reasoning_effort for
children, agent.reasoning_effort for the manager/orchestrator itself),
exactly like it already does for delegation.provider/model.
"""
from __future__ import annotations

from bot import hermes_config


def test_delegation_reasoning_effort_round_trips(tmp_path, temp_db):
    home = str(tmp_path / "hermes-home")
    hermes_config.set_delegation_config(reasoning_effort="low", hermes_home=home)
    assert hermes_config.read_delegation_config(hermes_home=home)["reasoning_effort"] == "low"


def test_delegation_reasoning_effort_is_independent_of_provider_model(tmp_path, temp_db):
    home = str(tmp_path / "hermes-home")
    hermes_config.set_delegation_config(provider="openrouter", model="some/model", hermes_home=home)
    hermes_config.set_delegation_config(reasoning_effort="xhigh", hermes_home=home)
    cfg = hermes_config.read_delegation_config(hermes_home=home)
    assert cfg["provider"] == "openrouter"
    assert cfg["model"] == "some/model"
    assert cfg["reasoning_effort"] == "xhigh"


def test_agent_config_empty_when_never_set(tmp_path):
    home = str(tmp_path / "hermes-home")
    assert hermes_config.read_agent_config(hermes_home=home) == {}


def test_agent_config_reasoning_effort_round_trips(tmp_path, temp_db):
    home = str(tmp_path / "hermes-home")
    hermes_config.set_agent_config(reasoning_effort="max", hermes_home=home)
    assert hermes_config.read_agent_config(hermes_home=home) == {"reasoning_effort": "max"}


def test_agent_config_is_a_distinct_section_from_delegation(tmp_path, temp_db):
    """The manager's own effort (agent.reasoning_effort) must never be
    conflated with its children's (delegation.reasoning_effort) — they
    are two separate config keys in two separate sections."""
    home = str(tmp_path / "hermes-home")
    hermes_config.set_agent_config(reasoning_effort="minimal", hermes_home=home)
    hermes_config.set_delegation_config(reasoning_effort="ultra", hermes_home=home)

    assert hermes_config.read_agent_config(hermes_home=home)["reasoning_effort"] == "minimal"
    assert hermes_config.read_delegation_config(hermes_home=home)["reasoning_effort"] == "ultra"


def test_set_agent_config_with_no_changes_is_a_no_op_read(tmp_path):
    home = str(tmp_path / "hermes-home")
    result = hermes_config.set_agent_config(hermes_home=home)
    assert result == {}


def test_set_agent_config_preserves_other_top_level_sections(tmp_path, temp_db):
    """Round-trip write must never disturb unrelated sections of the
    user's real config.yaml — matches this module's existing
    comment-preserving discipline for the delegation section."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n  openrouter:\n    api_key: sk-test\n", encoding="utf-8"
    )

    hermes_config.set_agent_config(reasoning_effort="high", hermes_home=str(home))

    raw = (home / "config.yaml").read_text(encoding="utf-8")
    assert "openrouter" in raw
    assert "sk-test" in raw
    assert "reasoning_effort" in raw
