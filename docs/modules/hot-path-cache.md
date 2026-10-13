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
  embeddings_max_lookups: 64   # bigger batches skip the per-text lookups
```

- Web fetch: only GET page fetches, only 2xx, only bodies that arrived whole and are under `web_fetch_max_bytes`.
  The fetch client sends no credentials or cookies. The URL is still checked against the web policy before the cache
  is read.
- Embeddings: the memory store's vectors and the file server index's vectors (documents and search queries) for a real
  embedding model (not the built-in hashed fallback, in either place). Only texts without a cached vector reach the
  model. CacheIt has no multi-get, so a lookup is one request per text; a batch of more than `embeddings_max_lookups`
  texts (default 64) skips the lookups and only stores its vectors, so a big indexing batch never pays hundreds of
  round trips for what is mostly a cold cache.
- Voice attachments: `fetch_audio` (Discord/Slack voice messages) uses the `web_fetch` flag, under the same limits
  (2xx only, whole body, under `web_fetch_max_bytes` and the voice size cap). It caches only a request that carries no
  credentials (no Authorization, cookie or API-key header) and never answers a credentialed request from the cache. The
  Discord and Slack adapters send the bot's token, so today this applies to public attachment URLs only.
- Model lists: the Models page's live `/models` call per custom provider. Only model ids are stored; the key is part
  of the cache key as a SHA-256 hash only. A model pulled into a local server can take up to the TTL to appear.

Hit and miss counts per feature (this process) are in `bot.cache.stats()["hot_paths"]`: hits, misses, puts, hit rate
and whether the feature is on.
