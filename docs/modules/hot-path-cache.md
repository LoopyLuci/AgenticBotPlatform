# Hot paths on CacheIt

Three of ABP's repeated, slow lookups can go through the CacheIt module (roadmap CI-E). Each is opt-in and off by
default. With a feature off, or CacheIt not running, ABP does exactly what it did before.

```yaml
# config/backends.yaml
cache:
  web_fetch: false          # the agent's web_fetch GETs, by URL
  embeddings: false         # memory/RAG vectors, key = hash(model id + text)
  model_lists: false        # provider /models lists, key = provider + base URL + hash of the key
  web_fetch_ttl_s: 300
  embeddings_ttl_s: 2592000
  model_lists_ttl_s: 600
  web_fetch_max_bytes: 1000000
```

- Web fetch: only GET page fetches, only 2xx, only bodies that arrived whole and are under `web_fetch_max_bytes`.
  The fetch client sends no credentials or cookies. The URL is still checked against the web policy before the cache
  is read.
- Embeddings: the memory store's vectors for a real embedding model (not the built-in hashed fallback). Only texts
  without a cached vector reach the model.
- Model lists: the Models page's live `/models` call per custom provider. Only model ids are stored; the key is part
  of the cache key as a SHA-256 hash only. A model pulled into a local server can take up to the TTL to appear.

Hit and miss counts per feature (this process) are in `bot.cache.stats()["hot_paths"]`: hits, misses, puts, hit rate
and whether the feature is on.
