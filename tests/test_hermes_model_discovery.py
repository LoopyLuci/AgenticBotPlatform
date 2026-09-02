"""Phase 1 of the Hermes-swarm plan: live model/pricing discovery.

Covers three layers, each faked at its own outbound boundary so these
stay fast/offline while exercising real parsing logic:
  - HermesGatewayBackend.fetch_model_options() — a real GET call shape
    (headers, params) against a faked httpx client.
  - bot.models.hermes_models_with_pricing() — the live-then-disk-cache-
    fallback resolution, against a faked Router.get_backend_for_instance.
  - bot.commands.instance_model_groups()/instance_model_page() — real
    free-classification threaded through into the picker payload shape.
"""

from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, commands, models
from bot.backends.base import BackendError
from bot.backends.hermes_gateway_backend import HermesGatewayBackend


@pytest.fixture(autouse=True)
def _clear_hermes_gateway_cache():
    # Module-level, per-instance-id cache — temp_db resets autoincrement
    # each test, so instance ids are reused across tests and would
    # otherwise silently serve a previous test's cached payload.
    models._hermes_gateway_cache.clear()
    yield
    models._hermes_gateway_cache.clear()


def _run(coro):
    return asyncio.run(coro)


def _create_hermes_gateway_instance(model=None):
    return bot_instances.create_instance(
        name="hermes-worker", platform="telegram", backend="hermes_gateway",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False, model=model,
    )


class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._response


_SAMPLE_PAYLOAD = {
    "providers": [
        {
            "provider": "openrouter",
            "models": ["meta-llama/llama-3.1-8b-instruct:free", "anthropic/claude-opus-4.8"],
            "pricing": {
                "meta-llama/llama-3.1-8b-instruct:free": {"input": "free", "output": "free", "free": True},
                "anthropic/claude-opus-4.8": {"input": "$15.00", "output": "$75.00", "free": False},
            },
        },
    ],
}


def test_fetch_model_options_sends_header_auth_and_refresh_param(monkeypatch):
    backend = HermesGatewayBackend()
    backend._token = "tok123"

    async def _fake_ensure_connected():
        return None

    fake_client = _FakeAsyncClient(_FakeResponse(_SAMPLE_PAYLOAD))
    monkeypatch.setattr(backend, "_ensure_connected", _fake_ensure_connected)
    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=None: fake_client)

    payload = _run(backend.fetch_model_options(refresh=True))

    assert payload == _SAMPLE_PAYLOAD
    assert fake_client.calls[0]["headers"] == {"X-Hermes-Session-Token": "tok123"}
    assert fake_client.calls[0]["params"] == {"refresh": "true"}


def test_fetch_model_options_raises_backend_error_on_http_failure(monkeypatch):
    backend = HermesGatewayBackend()

    async def _fake_ensure_connected():
        return None

    fake_client = _FakeAsyncClient(_FakeResponse({}, status=500))
    monkeypatch.setattr(backend, "_ensure_connected", _fake_ensure_connected)
    monkeypatch.setattr("httpx.AsyncClient", lambda timeout=None: fake_client)

    try:
        _run(backend.fetch_model_options())
        assert False, "expected BackendError"
    except BackendError:
        pass


def test_hermes_models_with_pricing_uses_live_gateway_when_available(temp_db, monkeypatch):
    instance_id = _create_hermes_gateway_instance()

    fake_backend = HermesGatewayBackend()

    async def _fake_fetch(refresh=False):
        return _SAMPLE_PAYLOAD

    monkeypatch.setattr(fake_backend, "fetch_model_options", _fake_fetch)

    from bot.router import router

    monkeypatch.setattr(router, "get_backend_for_instance", lambda iid: fake_backend)

    grouped, source = _run(models.hermes_models_with_pricing(instance_id))

    assert source == "live"
    assert grouped["openrouter"]
    free_ids = {e["id"] for e in grouped["openrouter"] if e["free"]}
    assert free_ids == {"meta-llama/llama-3.1-8b-instruct:free"}
    paid = [e for e in grouped["openrouter"] if e["id"] == "anthropic/claude-opus-4.8"][0]
    assert paid["free"] is False
    assert paid["input"] == "$15.00"


def test_hermes_models_with_pricing_falls_back_to_disk_cache(temp_db, monkeypatch):
    instance_id = _create_hermes_gateway_instance()

    from bot.router import router

    # Not a HermesGatewayBackend at all (or the live call failed) -> None.
    monkeypatch.setattr(router, "get_backend_for_instance", lambda iid: None)
    monkeypatch.setattr(models, "live_hermes_models", lambda: {"openrouter": ["some-model:free", "some-model-paid"]})

    grouped, source = _run(models.hermes_models_with_pricing(instance_id))

    assert source == "cache_fallback"
    ids_by_free = {e["id"]: e["free"] for e in grouped["openrouter"]}
    assert ids_by_free == {"some-model:free": True, "some-model-paid": False}


def test_hermes_models_with_pricing_unavailable_when_nothing_works(temp_db, monkeypatch):
    instance_id = _create_hermes_gateway_instance()

    from bot.router import router

    monkeypatch.setattr(router, "get_backend_for_instance", lambda iid: None)
    monkeypatch.setattr(models, "live_hermes_models", lambda: None)

    grouped, source = _run(models.hermes_models_with_pricing(instance_id))

    assert grouped == {}
    assert source == "unavailable"


def test_instance_model_groups_uses_real_free_classification(temp_db, monkeypatch):
    instance_id = _create_hermes_gateway_instance()

    async def _fake_pricing(iid, refresh=False):
        return (
            {
                "openrouter": [
                    {"id": "paid-model", "free": False, "input": "$1", "output": "$2"},
                    {"id": "free-model", "free": True, "input": None, "output": None},
                ],
            },
            "live",
        )

    monkeypatch.setattr(models, "hermes_models_with_pricing", _fake_pricing)

    groups = _run(commands.instance_model_groups("hermes_gateway", instance_id=instance_id))

    assert len(groups) == 1
    group = groups[0]
    assert group["provider"] == "openrouter"
    assert group["pricing_source"] == "live"
    assert group["free_ids"] == {"free-model"}
    # Free model sorted first, matching the existing regex-based ordering contract.
    assert group["models"] == ["free-model", "paid-model"]


def test_instance_model_page_free_count_uses_real_free_ids(temp_db, monkeypatch):
    instance_id = _create_hermes_gateway_instance()

    async def _fake_groups(backend, instance_id=None):
        return [
            {"provider": "openrouter", "models": ["free-model", "paid-model"], "free_ids": {"free-model"}},
            {"provider": "another", "models": ["m1"], "free_ids": set()},
        ]

    monkeypatch.setattr(commands, "instance_model_groups", _fake_groups)

    page = _run(commands.instance_model_page(instance_id, provider=None, page=0))

    assert page["mode"] == "providers"
    by_name = {p["name"]: p for p in page["providers"]}
    assert by_name["openrouter"]["free_count"] == 1
    assert by_name["another"]["free_count"] == 0
