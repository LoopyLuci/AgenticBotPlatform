"""model="auto" (native_agent backend only): NativeAgentBackend resolves a real
provider/model lazily, via the model router (bot/model_router.py), on its first turn -
and stays resolved after that. Also covers auto_failover (native_agent.router.*),
turning the existing one-hop static-fallback retry into a bounded, router-driven chain,
and the never-Claude-by-default candidate filter."""
from __future__ import annotations

import asyncio

import pytest

from bot import agent_settings, bot_instances, model_router, providers
from bot.backends.base import BackendError
from bot.backends.custom_model_backend import CustomModelBackend
from bot.router import Router


def _run(coro):
    return asyncio.run(coro)


def _make_instance(backend="native_agent"):
    return bot_instances.create_instance(
        name="worker", platform="telegram", backend=backend,
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[1],
    )


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, responses=None, error=None):
        self._responses = list(responses or [])
        self._error = error
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json})
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._responses.pop(0))


def _install_http(monkeypatch, *, responses=None, error=None):
    fake = _FakeAsyncClient(responses=responses, error=error)
    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: fake)
    return fake


def _reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


# ---- router.py / custom_model_backend.py construction --------------------------------

def test_native_agent_backend_with_no_model_builds_without_raising(temp_db):
    r = Router()
    backend = r._build_backend("native_agent", {"backends": {}})
    assert isinstance(backend, CustomModelBackend)
    assert backend.model_id == "auto"


def test_native_agent_backend_with_literal_auto_builds_without_raising(temp_db):
    r = Router()
    backend = r._build_backend("native_agent", {"backends": {"native_agent": {"model": "AUTO"}}})
    assert isinstance(backend, CustomModelBackend)
    assert backend.model_id == "auto"


def test_custom_model_backend_with_no_model_still_raises(temp_db):
    r = Router()
    with pytest.raises(ValueError, match="needs a model"):
        r._build_backend("custom_model", {"backends": {}})


# ---- auto-select: resolves once, on the first real turn ------------------------------

def test_auto_resolves_on_first_turn_and_stays_resolved(temp_db, monkeypatch, tmp_path):
    providers.set_provider("free_provider", base_url="https://free.example/v1", api_key="sk-free")
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["free_provider/free-model"])
    fake = _install_http(monkeypatch, responses=[_reply("hello from the router's pick"), _reply("still that model")])
    instance_id = _make_instance()

    backend = CustomModelBackend(provider_name=None, model_id="auto", base_url="")
    ctx = {"cwd": str(tmp_path / "ws"), "instance_id": instance_id}
    result = _run(backend.ask("hi", context=ctx))
    assert result.text == "hello from the router's pick"
    assert fake.requests[0]["json"]["model"] == "free-model"
    assert backend._inner.model == "free-model" and backend._inner.transport is not None

    # A second turn must not consult the router again - the resolution sticks.
    monkeypatch.setattr(model_router, "candidate_models", lambda: (_ for _ in ()).throw(AssertionError("should not be called again")))
    result2 = _run(backend.ask("again", context=ctx))
    assert result2.text == "still that model"


def test_auto_with_no_viable_candidate_fails_clearly(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(model_router, "candidate_models", lambda: [])
    instance_id = _make_instance()
    backend = CustomModelBackend(provider_name=None, model_id="auto", base_url="")
    with pytest.raises(BackendError, match="no free model available for automatic routing"):
        _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))


def test_auto_is_refused_when_router_enabled_is_off(temp_db, monkeypatch, tmp_path):
    from bot.config import config

    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {"router": {"enabled": False}}})
    instance_id = _make_instance()
    backend = CustomModelBackend(provider_name=None, model_id="auto", base_url="")
    with pytest.raises(BackendError, match="automatic model routing is turned off"):
        _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))


# ---- auto-failover: bounded, router-driven, beyond the one static fallback -----------

def test_auto_failover_off_by_default_only_tries_the_static_fallback(temp_db, monkeypatch, tmp_path):
    providers.set_provider("primary_provider", base_url="https://primary.example/v1", api_key="sk-p")
    providers.set_provider("fallback_provider", base_url="https://fallback.example/v1", api_key="sk-f")
    providers.set_provider("router_provider", base_url="https://router.example/v1", api_key="sk-r")
    fake = _install_http(monkeypatch, error=RuntimeError("boom"))
    # router.auto_failover defaults to False - a router candidate must never be tried even
    # if _resolve_auto_transport would otherwise find one.
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["router_provider/router-model"])
    instance_id = _make_instance()
    agent_settings.set_settings(instance_id, fallback_provider="fallback_provider", fallback_model="fallback-model")
    backend = CustomModelBackend(provider_name="primary_provider", model_id="primary-model", base_url="https://primary.example/v1")
    with pytest.raises(BackendError, match="boom"):
        _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))
    # Only the primary, then the one static fallback - never the router candidate.
    assert len(fake.requests) == 2


def test_auto_failover_on_tries_router_candidates_after_the_static_fallback(temp_db, monkeypatch, tmp_path):
    from bot.config import config

    providers.set_provider("primary_provider", base_url="https://primary.example/v1", api_key="sk-p")
    providers.set_provider("router_provider", base_url="https://router.example/v1", api_key="sk-r")
    monkeypatch.setattr(config, "_data", {**config._data,
                                            "native_agent": {"router": {"auto_failover": True, "max_failover_hops": 2}}})
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["router_provider/router-model"])
    fake = _FakeAsyncClient(error=RuntimeError("primary down"))
    calls = {"n": 0}

    async def _post(self, url, json=None, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("primary down")
        return _FakeResponse(_reply("answered by the router's failover pick"))
    monkeypatch.setattr(_FakeAsyncClient, "post", _post)
    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: fake)

    instance_id = _make_instance()   # no fallback_provider/fallback_model configured
    backend = CustomModelBackend(provider_name="primary_provider", model_id="primary-model", base_url="https://primary.example/v1")
    result = _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))
    assert result.text == "answered by the router's failover pick"
    assert calls["n"] == 2


def test_auto_failover_never_retries_the_same_candidate_twice(temp_db, monkeypatch, tmp_path):
    from bot.config import config

    providers.set_provider("primary_provider", base_url="https://primary.example/v1", api_key="sk-p")
    monkeypatch.setattr(config, "_data", {**config._data,
                                            "native_agent": {"router": {"auto_failover": True, "max_failover_hops": 3}}})
    # The router's only candidate IS the (already-failed) primary - failover must not loop on it.
    monkeypatch.setattr(model_router, "candidate_models", lambda: ["primary_provider/primary-model"])
    _install_http(monkeypatch, error=RuntimeError("down everywhere"))
    instance_id = _make_instance()
    backend = CustomModelBackend(provider_name="primary_provider", model_id="primary-model", base_url="https://primary.example/v1")
    with pytest.raises(BackendError, match="no free model available for automatic routing|down everywhere"):
        _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws"), "instance_id": instance_id}))


# ---- never Claude by default ----------------------------------------------------------

def test_candidate_models_excludes_anthropic_from_the_implicit_free_search(monkeypatch):
    from bot import model_catalog

    fake_results = [
        {"provider": "anthropic", "model": "claude-haiku-4-5"},
        {"provider": "openrouter", "model": "qwen/qwen3-8b:free"},
    ]
    monkeypatch.setattr(model_catalog, "search", lambda **kw: fake_results)
    monkeypatch.setattr(model_router, "_cfg", lambda: {})
    out = model_router.candidate_models()
    assert out == ["openrouter/qwen/qwen3-8b:free"]


def test_candidate_models_does_not_filter_an_explicit_list(monkeypatch):
    monkeypatch.setattr(model_router, "_cfg", lambda: {"candidates": ["anthropic/claude-haiku-4-5"]})
    assert model_router.candidate_models() == ["anthropic/claude-haiku-4-5"]
