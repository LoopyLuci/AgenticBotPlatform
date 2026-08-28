"""bot/providers.py — the named custom-model-provider registry backing the
"custom_model" backend (any OpenAI-compatible endpoint, local or cloud).
Exercises real file reads/writes against a temp config/providers.yaml,
not mocks, since the atomic-write/reload path is exactly what a bad edit
must never be able to crash — the same standard config.py's own tests
hold ConfigManager to.
"""

from __future__ import annotations

import pytest

from bot import providers
from bot.config import ConfigManager


@pytest.fixture(autouse=True)
def _temp_registry(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=path))


def test_empty_registry_has_no_providers():
    assert providers.list_providers() == {}
    assert providers.get_provider("anything") is None


def test_set_and_list_provider():
    providers.set_provider("local_ollama", "http://127.0.0.1:11434/v1")
    listed = providers.list_providers()
    assert listed["local_ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert listed["local_ollama"]["protocol"] == "openai"


def test_set_provider_rejects_empty_name():
    with pytest.raises(ValueError):
        providers.set_provider("", "http://x")


def test_set_provider_rejects_slash_in_name():
    with pytest.raises(ValueError):
        providers.set_provider("a/b", "http://x")


def test_set_provider_rejects_empty_base_url():
    with pytest.raises(ValueError):
        providers.set_provider("name", "")


def test_delete_provider():
    providers.set_provider("temp", "http://x")
    assert providers.delete_provider("temp") is True
    assert providers.get_provider("temp") is None


def test_delete_missing_provider_returns_false():
    assert providers.delete_provider("nope") is False


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret123")
    providers.set_provider("withkey", "http://x", api_key_env="TEST_PROVIDER_KEY")
    assert providers.get_api_key("withkey") == "secret123"


def test_api_key_inline():
    providers.set_provider("withinlinekey", "http://x", api_key="inline-secret")
    assert providers.get_api_key("withinlinekey") == "inline-secret"


def test_api_key_none_when_unconfigured():
    providers.set_provider("nokey", "http://x")
    assert providers.get_api_key("nokey") is None


def test_persists_to_disk():
    providers.set_provider("persisted", "http://x")
    # Fresh read straight from disk (bypassing the in-memory cache) to
    # prove this was actually written, not just held in memory.
    raw = providers._manager.read_raw()
    assert "persisted" in raw["providers"]


def test_parse_model_ref():
    assert providers.parse_model_ref("local_ollama/llama3.1") == ("local_ollama", "llama3.1")


@pytest.mark.parametrize("bad_ref", ["no-slash-here", "/missing-provider", "missing-model/", ""])
def test_parse_model_ref_rejects_bad_shape(bad_ref):
    with pytest.raises(ValueError):
        providers.parse_model_ref(bad_ref)
