"""Ollama, driven from ABP: find it, pull and load models and serve them to ABP's agents, build models from Modelfiles or
GGUF files, manage storage, and reach every other feature its API offers.

    client.py   finding Ollama (a configured provider whose address answers /api/version), HTTP, streamed progress, and
                a table of every route Ollama 0.34 serves (the parity layer; Ollama publishes no API description itself)
    harness.py  the everyday workflows: status, models and their capabilities, pull / push / copy / delete / create,
                importing a GGUF file, loading with a context that fits, unloading, moving old models into its folder
    tools.py    the agent's tools; anything that changes Ollama asks first

See docs/agents/ollama.md.

`ollama.enabled: false` (Settings → Local models) switches all of it off: the Ollama page, its routes and tools are not
loaded, find() reports no Ollama, the model router and Local Chat leave Ollama models out, and model discovery skips its
store. Nothing is uninstalled; switching it back on restores everything.
"""
from __future__ import annotations

from typing import Any, Optional


def enabled(cfg: Optional[dict] = None) -> bool:
    """False only when the config says `ollama: {enabled: false}`."""
    if cfg is None:
        try:
            from bot.config import config

            cfg = config.current or {}
        except Exception:  # noqa: BLE001 - no config yet: the default
            return True
    section: Any = (cfg or {}).get("ollama")
    return not (isinstance(section, dict) and section.get("enabled") is False)


def is_ollama_name(name: str) -> bool:
    """A provider or model-ref prefix that means Ollama ("ollama", "local_ollama", "my_ollama"...)."""
    return "ollama" in str(name or "").strip().lower()


def is_ollama_ref(ref: str) -> bool:
    return is_ollama_name(str(ref or "").partition("/")[0])


def leave_out(refs: list[str]) -> list[str]:
    """`refs` without Ollama's models while Ollama is switched off."""
    return list(refs) if enabled() else [r for r in refs if not is_ollama_ref(r)]
