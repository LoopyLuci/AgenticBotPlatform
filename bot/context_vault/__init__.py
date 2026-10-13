"""The context vault: the context API models work with, moved passively into a local tiered store (RAM cache,
NVMe, SATA SSD, HDD) and recalled for local models. Off until `context_vault.enabled: true`. See docs/context-vault.md.

`get_vault()` returns the process's vault (None while disabled); the capture hooks live in `capture`, the agent tool
and the local-turn injection in `tools`, the command line in `python -m bot.context_vault`.
"""
from __future__ import annotations

import threading
from typing import Optional

from bot.context_vault import settings
from bot.context_vault.store import KINDS, Vault

__all__ = ["KINDS", "Vault", "enabled", "get_vault", "reset", "settings", "set_vault"]

_lock = threading.Lock()
_vault: Optional[Vault] = None


def enabled() -> bool:
    return settings.enabled()


def get_vault() -> Optional[Vault]:
    """The shared vault, created (with its worker thread) on first use; None while the feature is off."""
    global _vault
    if _vault is not None:
        return _vault
    if not settings.enabled():
        return None
    with _lock:
        if _vault is None:
            from bot.context_vault import embed

            cfg = settings.load()
            v = Vault(cfg, embedder=embed.from_settings(cfg))
            v.start()
            _vault = v
    return _vault


def set_vault(v: Optional[Vault]) -> None:
    """Install a vault (tests, embedding); None forgets it."""
    global _vault
    with _lock:
        _vault = v


def reset() -> None:
    global _vault
    with _lock:
        v, _vault = _vault, None
    if v is not None:
        v.close()
