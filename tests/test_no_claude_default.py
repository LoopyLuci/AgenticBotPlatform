"""Nothing picks Claude/Anthropic implicitly: no provider means the router's local-first choice, an unknown provider is an
error that names it, and an explicitly named "anthropic" still works."""
from __future__ import annotations

import asyncio

import pytest

from abp_run import core
from bot import model_router
from bot.agent_runtime import moa
from bot.agent_runtime.transports.anthropic import AnthropicTransport
from bot.backends.base import BackendError


def _nothing(monkeypatch):
    from bot.capacity import routing

    monkeypatch.setattr(routing, "local_candidates", lambda: [])
    monkeypatch.setattr(model_router, "candidate_models", lambda: [])


def test_default_refs_prefers_local_and_never_invents_claude(monkeypatch):
    from bot.capacity import routing

    monkeypatch.setattr(routing, "local_candidates", lambda: ["modelmistress/a"])
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["openrouter/b:free", "modelmistress/a"])
    assert model_router.default_refs() == ["modelmistress/a", "openrouter/b:free"]


def test_default_refs_errors_when_nothing_is_available(monkeypatch):
    _nothing(monkeypatch)
    with pytest.raises(BackendError, match="Claude"):
        model_router.default_refs()
    with pytest.raises(BackendError):
        model_router.default_transport()


def test_moa_no_provider_uses_router_pick(monkeypatch):
    fake = object()
    monkeypatch.setattr(model_router, "default_transport", lambda: (fake, "local-m", "x/local-m"))
    assert moa._resolve(None, "whatever") == (fake, "local-m")


def test_moa_no_provider_and_no_model_available_errors(monkeypatch):
    _nothing(monkeypatch)
    with pytest.raises(BackendError):
        moa._resolve(None, "m")


def test_moa_unknown_provider_is_an_error_naming_it(monkeypatch):
    from bot import providers

    monkeypatch.setattr(providers, "get_provider", lambda name: None)
    with pytest.raises(BackendError, match="nonesuch"):
        moa._resolve("nonesuch", "m")


def test_moa_explicit_anthropic_is_still_claude():
    transport, model = moa._resolve("anthropic", "claude-sonnet-5")
    assert isinstance(transport, AnthropicTransport) and model == "claude-sonnet-5"


def test_mcp_sampling_named_provider_needs_a_model(monkeypatch):
    from bot import providers
    from bot.agent_runtime import mcp_client

    monkeypatch.setattr(providers, "get_provider", lambda name: {"base_url": "http://127.0.0.1:1/v1"})
    with pytest.raises(ValueError, match="model"):
        mcp_client._resolve_sampling_transport({"provider": "p"})
    with pytest.raises(ValueError, match="nonesuch"):
        monkeypatch.setattr(providers, "get_provider", lambda name: None)
        mcp_client._resolve_sampling_transport({"provider": "nonesuch", "model": "m"})


def test_split_model_unknown_provider_never_becomes_anthropic(monkeypatch):
    monkeypatch.setattr(core, "_providers", lambda: {"ollama"})
    with pytest.raises(core.RunError, match="ghost"):
        core.split_model("ghost/model")
    with pytest.raises(core.RunError, match="ghost"):
        core.split_model("ghost/model", default_provider="")
    assert core.split_model("ollama/m") == ("ollama", "m")
    assert core.split_model("anthropic/claude-x") == ("anthropic", "claude-x")
    with pytest.raises(core.RunError):
        core.transport_for("", "m")
    assert isinstance(core.transport_for("anthropic", "claude-x"), AnthropicTransport)


def test_ui_customize_fallback_is_local_not_claude(monkeypatch):
    from bot import providers, ui_customize

    monkeypatch.setattr(providers, "list_providers", lambda: {})
    monkeypatch.setattr(model_router, "default_refs", lambda: ["modelmistress/qwen"])
    assert asyncio.run(ui_customize._candidate_free_models()) == [("modelmistress", "qwen")]
    _nothing(monkeypatch)


def test_scripted_and_explicit_models_never_touch_default_selection(monkeypatch, tmp_path):
    from abp_acp import __main__ as acp

    def boom(*a, **k):
        raise AssertionError("default selection must not run for an explicit model")

    monkeypatch.setattr(model_router, "default_refs", boom)
    monkeypatch.setattr(model_router, "default_transport", boom)
    script = tmp_path / "s.json"
    script.write_text('[{"say": "ok"}]', encoding="utf-8")
    assert acp.make_runner(f"scripted:{script}", None) is not None
    assert acp.make_runner("anthropic/claude-x", None) is not None
