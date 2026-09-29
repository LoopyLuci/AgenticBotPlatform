"""bot/cache.py against a real CacheIt hub (the module's own cacheitd), and without one."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bot import cache
from bot.modules import harness, registry


def _cacheit_built() -> bool:
    try:
        m = registry.get("cacheit")
        return m.hub is not None and all(harness.built(m).values()) and bool(harness.built(m))
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def no_hub(monkeypatch):
    monkeypatch.setattr(cache, "_find", lambda: (None, None))


def test_without_cacheit_everything_is_a_miss_and_nothing_breaks(no_hub):
    assert cache.get("t", "k") is None
    assert cache.put("t", "k", b"v") is False
    calls = []
    assert cache.get_or_compute("t", "k", lambda: calls.append(1) or {"x": 1}) == {"x": 1}
    assert calls == [1]
    assert cache.stats()["available"] is False


@pytest.fixture
def hub(tmp_path, monkeypatch):
    if not _cacheit_built():
        pytest.skip("CacheIt is not built on this machine")
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / m.id)
    registry.modules(refresh=True)
    harness.start_hub("cacheit")
    cache._hub["at"] = 0.0
    yield
    harness.stop_hub("cacheit")
    cache._hub["at"] = 0.0


def test_values_round_trip_and_long_keys_are_hashed(hub):
    assert cache.available()
    assert cache.put("abp-test", "greeting", "hello", ttl_s=60)
    assert cache.get("abp-test", "greeting") == b"hello"
    long_key = "https://example.com/?" + "q" * 500
    blob = bytes(range(256)) * 4000
    assert cache.put("abp-test", long_key, blob)
    assert cache.get("abp-test", long_key) == blob
    assert cache.delete("abp-test", "greeting")
    assert cache.get("abp-test", "greeting") is None
    assert cache.stats()["hub"]["puts"] >= 2


def test_get_or_compute_skips_the_slow_work_the_second_time(hub):
    def slow():
        time.sleep(0.3)
        return {"models": ["a", "b"], "at": 1}
    t0 = time.perf_counter()
    first = cache.get_or_compute("abp-test", "models", slow, ttl_s=60)
    t1 = time.perf_counter()
    second = cache.get_or_compute("abp-test", "models", slow, ttl_s=60)
    t2 = time.perf_counter()
    assert first == second == {"models": ["a", "b"], "at": 1}
    assert t1 - t0 >= 0.3 and t2 - t1 < 0.1, (t1 - t0, t2 - t1)


def test_the_module_is_driven_through_the_framework(hub):
    from bot.modules import conformance
    assert harness.hub_state(registry.get("cacheit"))["running"]
    assert {o["id"] for o in harness.operations("cacheit")} >= {"cache.get", "cache.put", "cache.stats"}
    assert harness.call("cacheit", "cache.put", {"namespace": "abp-test", "key": "k", "value": "v"})["stored"]
    assert harness.call("cacheit", "cache.get", {"namespace": "abp-test", "key": "k"})["value"] == "v"
    assert Path(registry.data_dir(registry.get("cacheit"))).is_dir()
    assert conformance.check("cacheit", start=False)["ok"]
