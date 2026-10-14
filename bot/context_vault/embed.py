"""Embeddings from the configured local model through ModelMistress (`/v1/embeddings`). Optional: every failure
returns None and the vault falls back to keyword scoring. Blocking; callers off the event loop (the vault's worker
thread, or `asyncio.to_thread`)."""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("bot.context_vault")

Embedder = Callable[[list[str]], Optional[list[list[float]]]]


def modelmistress_embedder(model: str, timeout_s: float = 3.0) -> Optional[Embedder]:
    if not model:
        return None

    def embed(texts: list[str]) -> Optional[list[list[float]]]:
        try:
            import httpx
            from bot.modelmistress import client

            hub = client.find_cached()
            if hub is None:
                return None
            r = httpx.post(hub.openai_base + "/embeddings", json={"model": model, "input": texts},
                           headers=client._headers(hub), timeout=timeout_s)
            if r.status_code >= 400:
                return None
            data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
            vecs = [d["embedding"] for d in data]
            return vecs if len(vecs) == len(texts) else None
        except Exception as exc:  # noqa: BLE001 - optional feature
            logger.debug("context vault: embeddings unavailable: %s", exc)
            return None

    return embed


def from_settings(cfg: dict) -> Optional[Embedder]:
    e = cfg.get("embeddings") or {}
    if not e.get("enabled"):
        return None
    return modelmistress_embedder(str(e.get("model") or ""), float(e.get("timeout_s") or 3.0))
