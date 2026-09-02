"""Phase 5 of the Hermes-swarm plan: per-instance HERMES_HOME isolation.

Verifies the two correctness properties that make "isolated" mean
something real instead of just a config label: (1) two hermes_gateway
instances with distinct hermes_home values get distinct Backend objects
(their own cache slot, own port, own HERMES_HOME env var to pass to the
spawned process) instead of silently sharing one, and (2) delegation
config reads/writes for an isolated instance never touch the shared
default file another (non-isolated) instance uses.
"""

from __future__ import annotations

import asyncio

from bot import bot_instances, hermes_config
from bot.backends.hermes_gateway_backend import HermesGatewayBackend
from bot.config import config
from bot.router import Router


def _run(coro):
    return asyncio.run(coro)


def _create_hermes_instance(name, hermes_home=None):
    return bot_instances.create_instance(
        name=name, platform="telegram", backend="hermes_gateway",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
        allowed_user_ids=[111], enabled=False, hermes_home=hermes_home,
    )


def test_isolated_instances_get_distinct_backend_objects_and_ports(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"backends": {}, "action_overrides": {}, "timeouts": {}})
    r = Router()

    a_id = _create_hermes_instance("worker-a", hermes_home="/tmp/hermes-a")
    b_id = _create_hermes_instance("worker-b", hermes_home="/tmp/hermes-b")

    backend_a = r.get_backend_for_instance(a_id)
    backend_b = r.get_backend_for_instance(b_id)

    assert isinstance(backend_a, HermesGatewayBackend)
    assert isinstance(backend_b, HermesGatewayBackend)
    assert backend_a is not backend_b
    assert backend_a.hermes_home == "/tmp/hermes-a"
    assert backend_b.hermes_home == "/tmp/hermes-b"
    assert backend_a.port != backend_b.port


def test_isolated_instance_port_is_deterministic_across_lookups(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"backends": {}, "action_overrides": {}, "timeouts": {}})
    r = Router()
    a_id = _create_hermes_instance("worker-a", hermes_home="/tmp/hermes-a")

    first = r.get_backend_for_instance(a_id)
    second = r.get_backend_for_instance(a_id)

    assert first is second  # same cache slot
    assert first.port == second.port


def test_non_isolated_instance_shares_the_default_backend_slot(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"backends": {}, "action_overrides": {}, "timeouts": {}})
    r = Router()
    a_id = _create_hermes_instance("shared-a")
    b_id = _create_hermes_instance("shared-b")

    backend_a = r.get_backend_for_instance(a_id)
    backend_b = r.get_backend_for_instance(b_id)

    assert backend_a is backend_b  # historical shared behavior preserved
    assert backend_a.hermes_home is None


def test_spawn_sets_hermes_home_env_var(monkeypatch):
    backend = HermesGatewayBackend(hermes_home="/tmp/hermes-isolated")

    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        raise FileNotFoundError("not actually spawning in this test")

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def run():
        try:
            await backend._spawn_if_needed()
        except Exception:
            pass

    asyncio.run(run())

    assert captured["env"]["HERMES_HOME"] == "/tmp/hermes-isolated"


def test_evict_backend_shuts_down_and_removes_the_cached_object(temp_db, monkeypatch):
    # Real-world case this closes (found via live testing): editing a
    # hermes_gateway instance's model/hermes_home builds a NEW Backend
    # object under a new cache key, silently orphaning the OLD one —
    # still holding its spawned `hermes serve` subprocess — under its now
    # unreachable old key forever.
    monkeypatch.setattr(config, "_data", {"backends": {}, "action_overrides": {}, "timeouts": {}})
    r = Router()
    a_id = _create_hermes_instance("worker-a", hermes_home="/tmp/hermes-a")

    old_backend = r.get_backend_for_instance(a_id)
    shutdown_calls = []

    async def fake_shutdown():
        shutdown_calls.append(True)

    monkeypatch.setattr(old_backend, "shutdown", fake_shutdown)

    _run(r.evict_backend("hermes_gateway", model_override=None, hermes_home="/tmp/hermes-a"))

    assert shutdown_calls == [True]
    # A fresh lookup now builds a genuinely new object, not the evicted one.
    new_backend = r.get_backend_for_instance(a_id)
    assert new_backend is not old_backend


def test_evict_backend_is_a_no_op_for_an_unknown_key(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"backends": {}, "action_overrides": {}, "timeouts": {}})
    r = Router()
    # Nothing cached at all yet — must not raise.
    _run(r.evict_backend("hermes_gateway", model_override=None, hermes_home="/never-used"))


def test_delegation_config_isolated_from_shared_default(tmp_path, monkeypatch, temp_db):
    shared_path = tmp_path / "shared" / "config.yaml"
    isolated_path = tmp_path / "isolated" / "config.yaml"
    monkeypatch.setattr(hermes_config, "HERMES_CONFIG_PATH", shared_path)

    hermes_config.set_delegation_config(provider="shared-provider", hermes_home=None)
    hermes_config.set_delegation_config(provider="isolated-provider", hermes_home=str(tmp_path / "isolated"))

    assert hermes_config.read_delegation_config(None)["provider"] == "shared-provider"
    assert hermes_config.read_delegation_config(str(tmp_path / "isolated"))["provider"] == "isolated-provider"
    assert isolated_path.is_file()
    assert shared_path.is_file()
