"""Phase E of the Native Hermes-parity plan: the "native_agent" backend
family — runtime-identical to "custom_model" (same NativeAgentBackend
loop, same config/providers.yaml registry), differentiated at the
routing/dispatch level so an operator's intent ("this instance is for
spawn_subagent-driven swarm work") is explicit in bot_instances.backend.
"""

from __future__ import annotations

import pytest

from bot import providers
from bot.backends.custom_model_backend import CustomModelBackend
from bot.config import ConfigManager
from bot.models import BACKEND_FAMILY
from bot.router import VALID_BACKENDS, Router


@pytest.fixture(autouse=True)
def _temp_registry(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=path))


def test_native_agent_is_a_valid_backend():
    assert "native_agent" in VALID_BACKENDS


def test_native_agent_shares_the_custom_family():
    assert BACKEND_FAMILY["native_agent"] == "custom"


def test_build_backend_constructs_custom_model_backend_from_provider_registry():
    providers.set_provider("openrouter", base_url="https://openrouter.ai/api/v1", api_key="sk-test")

    backend = Router()._build_backend("native_agent", cfg={}, model_override="openrouter/free-model:free")

    assert isinstance(backend, CustomModelBackend)
    assert backend.provider_name == "openrouter"
    assert backend.model_id == "free-model:free"
    assert backend.base_url == "https://openrouter.ai/api/v1"
    assert backend.api_key == "sk-test"


def test_build_backend_reads_model_from_backends_config_when_no_override():
    providers.set_provider("ollama", base_url="http://127.0.0.1:11434/v1")

    backend = Router()._build_backend(
        "native_agent", cfg={"backends": {"native_agent": {"model": "ollama/llama3.1"}}},
    )

    assert backend.provider_name == "ollama"
    assert backend.model_id == "llama3.1"


def test_build_backend_requires_a_model():
    with pytest.raises(ValueError, match="needs a model"):
        Router()._build_backend("native_agent", cfg={})


def test_build_backend_rejects_unknown_provider():
    with pytest.raises(ValueError, match="no provider named"):
        Router()._build_backend("native_agent", cfg={}, model_override="nope/some-model")
