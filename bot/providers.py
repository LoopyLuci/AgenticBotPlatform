"""Named custom model-provider registry (config/providers.yaml).

Lets a bot instance point at any OpenAI-compatible endpoint — a locally
running Ollama/LM Studio/vLLM/llama.cpp server, OpenRouter, or real
OpenAI — via a short provider name instead of wiring a raw base_url/api
key straight into bot_instances. A bot instance's existing `model`
column becomes "<provider_name>/<model_id>" when its backend is
"custom_model" (see parse_model_ref()).

Reuses bot/config.py's ConfigManager wholesale (atomic writes, hot
reload, "a bad file never crashes the app") for a second file
(config/providers.yaml) rather than inventing a second loader.
"""

from __future__ import annotations

import os
from typing import Optional

import yaml

from bot.config import ConfigManager
from bot.envfile import PROJECT_ROOT

PROVIDERS_PATH = PROJECT_ROOT / "config" / "providers.yaml"

_manager = ConfigManager(path=PROVIDERS_PATH)


def on_reload(callback) -> None:
    _manager.on_reload(callback)


def list_providers() -> dict[str, dict]:
    """name -> {base_url, protocol, api_key_env?} — never includes a raw
    inline api_key's value beyond what's already in the file verbatim,
    same "no extra redaction on read" stance the rest of config.py takes
    (GET /api/config already serves backends.yaml back unredacted)."""
    return dict(_manager.current.get("providers") or {})


def get_provider(name: str) -> Optional[dict]:
    return list_providers().get(name)


def get_api_key(name: str) -> Optional[str]:
    """The real key value for `name`, resolved from an env var
    (api_key_env, preferred) or an inline api_key field, or None if
    neither is configured — some local servers (Ollama, LM Studio) don't
    require one at all."""
    provider = get_provider(name)
    if not provider:
        return None
    env_name = provider.get("api_key_env")
    if env_name:
        return os.environ.get(env_name)
    return provider.get("api_key")


def set_provider(
    name: str,
    base_url: str,
    *,
    protocol: str = "openai",
    api_key_env: Optional[str] = None,
    api_key: Optional[str] = None,
    actor: str = "dashboard",
) -> None:
    if not name or not name.strip():
        raise ValueError("provider name is required")
    if "/" in name:
        raise ValueError("provider name may not contain '/' — it's used as the <provider>/<model_id> separator")
    if not base_url or not base_url.strip():
        raise ValueError("base_url is required")

    entry: dict = {"base_url": base_url.strip(), "protocol": protocol}
    if api_key_env:
        entry["api_key_env"] = api_key_env.strip()
    elif api_key:
        entry["api_key"] = api_key

    data = _manager.read_raw()
    providers = data.setdefault("providers", {})
    providers[name] = entry
    _write(data, actor)


def delete_provider(name: str, actor: str = "dashboard") -> bool:
    data = _manager.read_raw()
    providers = data.get("providers") or {}
    if name not in providers:
        return False
    del providers[name]
    _write(data, actor)
    return True


def _write(data: dict, actor: str) -> None:
    tmp_path = _manager.path.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    tmp_path.replace(_manager.path)  # atomic on the same filesystem
    _manager.reload(actor=actor)


def parse_model_ref(model_ref: str) -> tuple[str, str]:
    """"<provider>/<model_id>" -> (provider, model_id). Raises ValueError
    if `model_ref` isn't in that shape."""
    if not model_ref or "/" not in model_ref:
        raise ValueError(f"expected a model of the form '<provider>/<model_id>', got {model_ref!r}")
    provider, _, model_id = model_ref.partition("/")
    if not provider or not model_id:
        raise ValueError(f"expected a model of the form '<provider>/<model_id>', got {model_ref!r}")
    return provider, model_id
