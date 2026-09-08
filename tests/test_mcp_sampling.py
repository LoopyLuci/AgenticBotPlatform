"""bot/agent_runtime/mcp_client.py's MCP sampling support —
handle_sampling_request() is the real SamplingFnT callback wired into
every ClientSession this module opens; a connected server can ask
BotServer to run a real LLM completion on its behalf. Live-verified
separately (outside pytest) against a real stdio MCP server subprocess
genuinely calling ServerSession.create_message() — these tests cover the
unit-level branches (disabled by default, provider resolution, message
flattening, error handling) with a fake transport, no real `mcp` package
import required (the real types are only imported lazily inside the
function body).
"""
from __future__ import annotations

import asyncio

import pytest

from bot import providers
from bot.agent_runtime import mcp_client
from bot.agent_runtime.transports.base import NormalizedResponse


def _run(coro):
    return asyncio.run(coro)


class _FakeTransport:
    def __init__(self, text="a reply"):
        self.text = text
        self.calls: list[dict] = []

    def user_message(self, text, *, images=None):
        return {"role": "user", "content": text, "images": images}

    async def send(self, *, model, history, tool_schemas, max_tokens, timeout_s, system_prompt=None, effort=None):
        self.calls.append({
            "model": model, "history": history, "tool_schemas": tool_schemas,
            "max_tokens": max_tokens, "system_prompt": system_prompt,
        })
        return NormalizedResponse(text=self.text, assistant_message={})


class _FakeParams:
    def __init__(self, messages, max_tokens=None, system_prompt=None):
        self.messages = messages
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt


class _FakeTextContent:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeImageContent:
    type = "image"

    def __init__(self, data, mime_type):
        self.data = data
        self.mime_type = mime_type


class _FakeMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content


def test_disabled_by_default_returns_error_data(monkeypatch):
    monkeypatch.setattr(mcp_client, "_sampling_config", lambda: {})

    result = _run(mcp_client.handle_sampling_request(None, _FakeParams([_FakeMessage("user", _FakeTextContent("hi"))])))

    # Duck-typed rather than `isinstance(result, mcp.types.ErrorData)` so
    # this file never needs the real `mcp` package importable in this
    # dev shell (only in the pipeline's bundled venv — see this file's
    # own module docstring); handle_sampling_request() only imports
    # `mcp.types` lazily inside its own body.
    assert not hasattr(result, "content")
    assert "disabled" in result.message


def test_enabled_calls_the_resolved_transport_and_returns_text(monkeypatch):
    fake = _FakeTransport(text="the answer is 42")
    monkeypatch.setattr(mcp_client, "_sampling_config", lambda: {"enabled": True})
    monkeypatch.setattr(mcp_client, "_resolve_sampling_transport", lambda cfg: (fake, "some-model"))

    result = _run(mcp_client.handle_sampling_request(
        None, _FakeParams([_FakeMessage("user", _FakeTextContent("what is the answer?"))], max_tokens=200)
    ))

    assert result.content.text == "the answer is 42"
    assert result.model == "some-model"
    assert fake.calls[0]["max_tokens"] == 200
    assert fake.calls[0]["history"][0]["content"] == "what is the answer?"


def test_max_tokens_is_capped_at_max_sampling_tokens(monkeypatch):
    fake = _FakeTransport()
    monkeypatch.setattr(mcp_client, "_sampling_config", lambda: {"enabled": True})
    monkeypatch.setattr(mcp_client, "_resolve_sampling_transport", lambda cfg: (fake, "m"))

    _run(mcp_client.handle_sampling_request(
        None, _FakeParams([_FakeMessage("user", _FakeTextContent("hi"))], max_tokens=999999)
    ))

    assert fake.calls[0]["max_tokens"] == mcp_client.MAX_SAMPLING_TOKENS


def test_assistant_role_messages_are_not_run_through_user_message(monkeypatch):
    fake = _FakeTransport()
    monkeypatch.setattr(mcp_client, "_sampling_config", lambda: {"enabled": True})
    monkeypatch.setattr(mcp_client, "_resolve_sampling_transport", lambda cfg: (fake, "m"))

    _run(mcp_client.handle_sampling_request(None, _FakeParams([
        _FakeMessage("user", _FakeTextContent("question")),
        _FakeMessage("assistant", _FakeTextContent("prior answer")),
    ])))

    history = fake.calls[0]["history"]
    assert history[0]["content"] == "question"
    assert history[1] == {"role": "assistant", "content": "prior answer"}


def test_image_content_is_flattened_and_threaded_through(monkeypatch):
    fake = _FakeTransport()
    monkeypatch.setattr(mcp_client, "_sampling_config", lambda: {"enabled": True})
    monkeypatch.setattr(mcp_client, "_resolve_sampling_transport", lambda cfg: (fake, "m"))

    _run(mcp_client.handle_sampling_request(None, _FakeParams([
        _FakeMessage("user", [_FakeTextContent("what is this"), _FakeImageContent("b64data", "image/png")]),
    ])))

    entry = fake.calls[0]["history"][0]
    assert entry["content"] == "what is this"
    assert entry["images"] == [{"mime_type": "image/png", "data_b64": "b64data"}]


def test_transport_error_returns_error_data_not_a_raise(monkeypatch):
    class _FailingTransport(_FakeTransport):
        async def send(self, **kwargs):
            raise RuntimeError("provider is down")

    monkeypatch.setattr(mcp_client, "_sampling_config", lambda: {"enabled": True})
    monkeypatch.setattr(mcp_client, "_resolve_sampling_transport", lambda cfg: (_FailingTransport(), "m"))

    result = _run(mcp_client.handle_sampling_request(None, _FakeParams([_FakeMessage("user", _FakeTextContent("hi"))])))

    assert not hasattr(result, "content")
    assert "provider is down" in result.message


def test_resolve_sampling_transport_defaults_to_anthropic():
    from bot.agent_runtime.transports.anthropic import AnthropicTransport

    transport, model = mcp_client._resolve_sampling_transport({})

    assert isinstance(transport, AnthropicTransport)
    assert model == mcp_client.DEFAULT_SAMPLING_MODEL


def test_resolve_sampling_transport_uses_a_named_provider(temp_db):
    providers.set_provider("my_ollama", base_url="http://127.0.0.1:11434/v1")

    transport, model = mcp_client._resolve_sampling_transport({"provider": "my_ollama", "model": "llama3.1"})

    from bot.agent_runtime.transports.openai_compatible import OpenAICompatibleTransport

    assert isinstance(transport, OpenAICompatibleTransport)
    assert transport.base_url == "http://127.0.0.1:11434/v1"
    assert model == "llama3.1"


def test_resolve_sampling_transport_raises_for_an_unknown_provider(temp_db):
    with pytest.raises(ValueError, match="not a configured provider"):
        mcp_client._resolve_sampling_transport({"provider": "does_not_exist"})
