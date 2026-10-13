"""CacheIt on ABP's hot paths (bot/hotcache.py): off by default, a miss computes and stores, a hit does not compute,
and with CacheIt absent everything behaves as before. Fake cache and fake sources: no network, no real hub."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import numpy as np
import pytest

from bot import cache, hotcache, models as models_module
from bot.agent_runtime import web


class FakeCache:
    """Stands in for bot.cache: a dict, and a log of every call."""

    def __init__(self, up: bool = True):
        self.up, self.store, self.calls, self.ttl = up, {}, [], None

    def get_json(self, ns, key):
        self.calls.append(("get", ns))
        return json.loads(self.store[(ns, key)]) if self.up and (ns, key) in self.store else None

    def put_json(self, ns, key, value, ttl_s=None):
        self.calls.append(("put", ns))
        if not self.up:
            return False
        self.store[(ns, key)] = json.dumps(value)
        self.ttl = ttl_s
        return True


@pytest.fixture
def fake(monkeypatch):
    f = FakeCache()
    monkeypatch.setattr(hotcache, "cache", f)
    hotcache.reset_stats()
    return f


def turn_on(monkeypatch, **flags):
    block = dict(flags)
    monkeypatch.setattr(hotcache, "_block", lambda: block)
    return block


def run(coro):
    return asyncio.run(coro)


# ---- config ---------------------------------------------------------------------------
def test_everything_is_off_by_default(monkeypatch, fake):
    monkeypatch.setattr(hotcache, "_block", lambda: {})
    assert [hotcache.enabled(f) for f in hotcache.FEATURES] == [False] * 3
    assert hotcache.get("web_fetch", "k") is None and hotcache.put("web_fetch", "k", {"a": 1}) is False
    assert fake.calls == []


def test_the_shipped_config_has_the_block_all_off():
    import yaml
    from bot.config import CONFIG_PATH
    block = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["cache"]
    assert [block[f] for f in hotcache.FEATURES] == [False] * 3
    assert block["web_fetch_ttl_s"] == 300


def test_ttl_defaults_and_overrides(monkeypatch):
    turn_on(monkeypatch, model_lists_ttl_s=42, web_fetch_ttl_s="junk")
    assert hotcache.ttl_s("model_lists") == 42 and hotcache.ttl_s("web_fetch") == 300


# ---- web fetch ------------------------------------------------------------------------
@pytest.fixture
def net(monkeypatch):
    """A fake slow network behind web._fetch_network; web.check_url passes."""
    state = {"calls": 0, "result": ("https://x.test/p", 200, "text/html", b"<p>hi</p>")}

    async def fake_network(url):
        state["calls"] += 1
        await asyncio.sleep(0.2)
        return state["result"]

    async def ok(url):
        return url

    monkeypatch.setattr(web, "_fetch_network", fake_network)
    monkeypatch.setattr(web, "check_url", ok)
    return state


def test_web_fetch_off_makes_no_cache_calls(monkeypatch, fake, net):
    turn_on(monkeypatch)
    assert run(web.fetch("https://x.test/p"))[1] == 200
    assert fake.calls == [] and net["calls"] == 1


def test_web_fetch_miss_computes_and_puts_then_hit_does_not_compute(monkeypatch, fake, net):
    turn_on(monkeypatch, web_fetch=True, web_fetch_ttl_s=77)
    first = run(web.fetch("https://x.test/p"))
    second = run(web.fetch("https://x.test/p"))
    assert first == second == ("https://x.test/p", 200, "text/html", b"<p>hi</p>")
    assert net["calls"] == 1 and fake.ttl == 77
    s = hotcache.stats()["web_fetch"]
    assert (s["hits"], s["misses"], s["puts"], s["hit_rate"]) == (1, 1, 1, 0.5)
    assert cache.stats()["hot_paths"]["web_fetch"]["hits"] == 1


@pytest.mark.parametrize("result", [
    ("https://x.test/p", 404, "text/html", b"nope"),
    ("https://x.test/p", 302, "", b""),
    ("https://x.test/p", 500, "text/html", b"err"),
    ("https://x.test/p", 200, "text/html", b"x" * 2000),          # over web_fetch_max_bytes
])
def test_web_fetch_does_not_cache_non_2xx_or_big_bodies(monkeypatch, fake, net, result):
    turn_on(monkeypatch, web_fetch=True, web_fetch_max_bytes=1000)
    net["result"] = result
    run(web.fetch("https://x.test/p"))
    run(web.fetch("https://x.test/p"))
    assert net["calls"] == 2 and fake.store == {}


def test_web_fetch_does_not_cache_a_truncated_body(monkeypatch, fake, net):
    turn_on(monkeypatch, web_fetch=True, web_fetch_max_bytes=10 * web.MAX_BODY_BYTES)
    net["result"] = ("https://x.test/p", 200, "text/plain", b"x" * web.MAX_BODY_BYTES)
    run(web.fetch("https://x.test/p"))
    assert fake.store == {}


def test_web_fetch_with_cacheit_absent_behaves_as_before(monkeypatch, net):
    monkeypatch.setattr(hotcache, "cache", FakeCache(up=False))
    turn_on(monkeypatch, web_fetch=True)
    assert run(web.fetch("https://x.test/p"))[1] == 200
    assert run(web.fetch("https://x.test/p"))[1] == 200
    assert net["calls"] == 2


def test_web_fetch_still_checks_the_url_on_a_hit(monkeypatch, fake, net):
    turn_on(monkeypatch, web_fetch=True)
    run(web.fetch("https://x.test/p"))
    checked = []

    async def deny(url):
        checked.append(url)
        raise web.ToolError("blocked")

    monkeypatch.setattr(web, "check_url", deny)
    with pytest.raises(web.ToolError):
        run(web.fetch("https://x.test/p"))
    assert checked == ["https://x.test/p"]


def test_the_fetch_client_only_ever_sends_a_credential_free_get(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain", "set-cookie": "a=b"})

    async def ok(url):
        return url

    monkeypatch.setattr(web, "check_url", ok)
    monkeypatch.setattr(web, "_new_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": web.USER_AGENT}))
    run(web._fetch_network("https://x.test/p"))
    assert [r.method for r in seen] == ["GET"]
    assert not ({"authorization", "cookie"} & set(seen[0].headers))


# ---- model lists ----------------------------------------------------------------------
class Registry:
    def __init__(self, key):
        self.key = key

    def get_api_key(self, name):
        return self.key


def fake_models_client(monkeypatch, calls):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls.append((url, headers))
            await asyncio.sleep(0.2)
            return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)


ENTRY = {"base_url": "http://models.test/v1"}


def test_model_lists_off_makes_no_cache_calls(monkeypatch, fake):
    turn_on(monkeypatch)
    calls = []
    fake_models_client(monkeypatch, calls)
    assert run(models_module._fetch_custom_models("p", ENTRY, Registry("sk-secret"))) == ["a", "b"]
    assert fake.calls == [] and len(calls) == 1


def test_model_lists_miss_then_hit_and_the_key_is_never_stored(monkeypatch, fake):
    turn_on(monkeypatch, model_lists=True)
    calls = []
    fake_models_client(monkeypatch, calls)
    reg = Registry("sk-secret")
    assert run(models_module._fetch_custom_models("p", ENTRY, reg)) == ["a", "b"]
    assert run(models_module._fetch_custom_models("p", ENTRY, reg)) == ["a", "b"]
    assert len(calls) == 1
    assert all("sk-secret" not in repr(k) and "sk-secret" not in v for k, v in fake.store.items())
    assert hotcache.stats()["model_lists"]["hits"] == 1
    run(models_module._fetch_custom_models("p", ENTRY, Registry("sk-other")))     # another key, another list
    assert len(calls) == 2


def test_model_lists_with_cacheit_absent_behaves_as_before(monkeypatch):
    monkeypatch.setattr(hotcache, "cache", FakeCache(up=False))
    turn_on(monkeypatch, model_lists=True)
    calls = []
    fake_models_client(monkeypatch, calls)
    for _ in range(2):
        assert run(models_module._fetch_custom_models("p", ENTRY, Registry(None))) == ["a", "b"]
    assert len(calls) == 2


# ---- embeddings -----------------------------------------------------------------------
class Embedder:
    def __init__(self):
        self.batches = []

    def __call__(self, texts):
        self.batches.append(list(texts))
        return np.asarray([[float(len(t)), 1.0] for t in texts], dtype=np.float32)


def test_embeddings_off_calls_the_model_and_not_the_cache(monkeypatch, fake):
    turn_on(monkeypatch)
    emb = Embedder()
    out = hotcache.embed_cached("m@u", emb, ["aa", "b"])
    assert out.shape == (2, 2) and fake.calls == [] and emb.batches == [["aa", "b"]]


def test_embeddings_miss_computes_and_puts_hit_does_not_compute(monkeypatch, fake):
    turn_on(monkeypatch, embeddings=True)
    emb = Embedder()
    first = hotcache.embed_cached("m@u", emb, ["aa", "b"])
    second = hotcache.embed_cached("m@u", emb, ["aa", "b"])
    assert np.array_equal(first, second) and len(emb.batches) == 1
    third = hotcache.embed_cached("m@u", emb, ["aa", "ccc"])       # only the new text is embedded
    assert emb.batches[-1] == ["ccc"] and third[1][0] == 3.0
    hotcache.embed_cached("other@u", emb, ["aa"])                  # the model id is part of the key
    assert emb.batches[-1] == ["aa"]
    s = hotcache.stats()["embeddings"]
    assert (s["hits"], s["misses"], s["puts"]) == (3, 4, 4)


def test_embeddings_with_cacheit_absent_behaves_as_before(monkeypatch):
    monkeypatch.setattr(hotcache, "cache", FakeCache(up=False))
    turn_on(monkeypatch, embeddings=True)
    emb = Embedder()
    hotcache.embed_cached("m@u", emb, ["aa"])
    hotcache.embed_cached("m@u", emb, ["aa"])
    assert len(emb.batches) == 2


def test_a_failing_embedder_returns_none(monkeypatch, fake):
    turn_on(monkeypatch, embeddings=True)
    assert hotcache.embed_cached("m@u", lambda t: None, ["aa"]) is None


def test_memoryfabric_skips_the_cache_for_the_hashed_fallback(monkeypatch, fake):
    from bot.memoryfabric import store
    turn_on(monkeypatch, embeddings=True)
    out = store._embed("hash-tfidf", lambda t: np.ones((len(t), 2), dtype=np.float32), ["a"])
    assert out.shape == (1, 2) and fake.calls == []
    store._embed("real@u", lambda t: np.ones((len(t), 2), dtype=np.float32), ["a"])
    assert fake.calls


# ---- timing ---------------------------------------------------------------------------
def test_a_repeated_call_is_faster_with_the_cache(monkeypatch, fake, net):
    turn_on(monkeypatch, web_fetch=True)
    t0 = time.perf_counter()
    run(web.fetch("https://x.test/p"))
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    run(web.fetch("https://x.test/p"))
    warm = time.perf_counter() - t0
    print(f"\nTIMING cold={cold * 1000:.0f} ms warm={warm * 1000:.0f} ms")
    assert warm < cold / 2


# ---- embeddings: batch cap ------------------------------------------------------------
def test_big_batches_skip_the_per_text_lookups_and_only_store(monkeypatch, fake):
    turn_on(monkeypatch, embeddings=True, embeddings_max_lookups=3)
    emb = Embedder()
    out = hotcache.embed_cached("m@u", emb, ["a", "bb", "ccc", "dddd"])
    assert out.shape == (4, 2) and emb.batches == [["a", "bb", "ccc", "dddd"]]
    assert [c[0] for c in fake.calls] == ["put"] * 4          # no GET at all
    emb2 = Embedder()
    hotcache.embed_cached("m@u", emb2, ["a", "bb", "ccc"])     # within the cap: looks up, all hits
    assert emb2.batches == [] and [c[0] for c in fake.calls[4:]] == ["get"] * 3


def test_max_lookups_default_and_bad_values(monkeypatch):
    for blk, want in (({}, 64), ({"embeddings_max_lookups": "x"}, 64), ({"embeddings_max_lookups": 0}, 64),
                      ({"embeddings_max_lookups": 5}, 5)):
        monkeypatch.setattr(hotcache, "_block", lambda b=blk: b)
        assert hotcache.max_lookups() == want


# ---- fileserver index embeddings ------------------------------------------------------
def test_fileserver_index_embeddings_use_the_cache_except_hash_vectors(monkeypatch, fake):
    from bot.fileserver import index
    turn_on(monkeypatch, embeddings=True)
    emb = Embedder()
    index._embed("ollama:m", emb, ["aa", "b"])
    index._embed("ollama:m", emb, ["aa", "b"])
    assert emb.batches == [["aa", "b"]]                        # the second call was all hits
    n = len(fake.calls)
    index._embed("hash-tfidf", emb, ["aa"])
    assert len(fake.calls) == n and emb.batches[-1] == ["aa"]  # hash vectors never touch the cache


# ---- fetch_audio ----------------------------------------------------------------------
class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    async def aiter_bytes(self):
        for i in range(0, len(self._data), 4):
            yield self._data[i:i + 4]


class _Client:
    def __init__(self, status=200, data=b"RIFFaudiobytes"):
        self.resp, self.n = _Resp(status, data), 0

    def stream(self, method, url, headers=None):
        client = self

        class _Ctx:
            async def __aenter__(self_):
                client.n += 1
                return client.resp

            async def __aexit__(self_, *a):
                return False
        return _Ctx()


def test_fetch_audio_caches_a_public_get_under_the_web_fetch_flag(monkeypatch, fake):
    from bot.platforms import _voice
    turn_on(monkeypatch, web_fetch=True)
    c = _Client()
    assert run(_voice.fetch_audio(c, "https://x.test/a.ogg", {})) == b"RIFFaudiobytes"
    assert run(_voice.fetch_audio(c, "https://x.test/a.ogg", {})) == b"RIFFaudiobytes"
    assert c.n == 1                                            # second answered from the cache


def test_fetch_audio_never_caches_a_request_with_credentials(monkeypatch, fake):
    from bot.platforms import _voice
    turn_on(monkeypatch, web_fetch=True)
    c = _Client()
    for _ in range(2):
        assert run(_voice.fetch_audio(c, "https://x.test/a.ogg", {"Authorization": "Bot t"})) == b"RIFFaudiobytes"
    assert c.n == 2 and fake.calls == []


def test_fetch_audio_does_not_cache_failures_or_big_bodies_and_is_off_by_default(monkeypatch, fake):
    from bot.platforms import _voice
    turn_on(monkeypatch, web_fetch=True, web_fetch_max_bytes=8)
    assert run(_voice.fetch_audio(_Client(404), "https://x.test/a", {})) is None
    assert run(_voice.fetch_audio(_Client(), "https://x.test/b", {})) == b"RIFFaudiobytes"   # 14 bytes > 8
    assert [c for c in fake.calls if c[0] == "put"] == []
    turn_on(monkeypatch)
    assert run(_voice.fetch_audio(_Client(), "https://x.test/c", {})) == b"RIFFaudiobytes"
    assert fake.calls.count(("get", "web_fetch")) == 2         # only the two earlier (enabled) lookups
