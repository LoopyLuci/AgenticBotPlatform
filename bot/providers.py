"""Named custom model-provider registry (config/providers.yaml).

Lets a bot instance point at any OpenAI-compatible endpoint — a locally
running Ollama/LM Studio/vLLM/llama.cpp server, OpenRouter, or real
OpenAI — via a short provider name instead of wiring a raw base_url/api
key straight into bot_instances. A bot instance's existing `model`
column becomes "<provider_name>/<model_id>" when its backend is
"custom_model" (see parse_model_ref()).

Reuses bot/config.py's ConfigManager for the READ side (hot reload,
`.current`, `on_reload()` — this module never needed a second watcher
implementation), but writes go through ruamel.yaml's round-trip loader
instead, same pattern bot/hermes_config.py uses for Hermes's own
config.yaml: a plain yaml.safe_load()/safe_dump() round trip silently
strips comments (confirmed live — the schema-explaining header comment
in config/providers.yaml was wiped by a single add-then-delete through
the dashboard before this fix), which matters here because this file
ships with real documentation comments, not just data.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from bot.config import ConfigManager
from bot.envfile import PROJECT_ROOT

PROVIDERS_PATH = PROJECT_ROOT / "config" / "providers.yaml"

_manager = ConfigManager(path=PROVIDERS_PATH)


def _yaml():
    from ruamel.yaml import YAML

    y = YAML(typ="rt")  # round-trip: preserves comments, key order, quoting style
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load_yaml_or_empty(path, yaml) -> dict[str, Any]:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.load(f) or {}


def _atomic_write_yaml(path, data: dict, yaml) -> None:
    tmp_path = path.with_suffix(".yaml.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    tmp_path.replace(path)  # atomic on the same filesystem


def on_reload(callback) -> None:
    _manager.on_reload(callback)


def reload(actor: str = "file-watch") -> tuple[bool, str]:
    """Re-reads config/providers.yaml from disk — used after
    bot/snapshots.py restores a previous copy of the file, same as
    bot.config.config.reload() is used for backends.yaml."""
    return _manager.reload(actor=actor)


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
    catalog_id: Optional[str] = None,
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
    # Which models.dev catalog provider (if any) this entry corresponds
    # to — set when added via the catalog-assisted picker, None for a
    # fully custom/local endpoint models.dev doesn't know about. Lets
    # bot.models.browse_provider_models() look up that provider's full
    # model list without having to guess it from the user-chosen `name`.
    if catalog_id:
        entry["catalog_id"] = catalog_id.strip()

    yaml = _yaml()
    data = _load_yaml_or_empty(_manager.path, yaml)
    providers = data.get("providers")
    if providers is None:
        providers = {}
        data["providers"] = providers
    providers[name] = entry
    _write(data, yaml, actor)


def delete_provider(name: str, actor: str = "dashboard") -> bool:
    yaml = _yaml()
    data = _load_yaml_or_empty(_manager.path, yaml)
    providers = data.get("providers") or {}
    if name not in providers:
        return False
    del providers[name]
    _write(data, yaml, actor)
    return True


def _write(data: dict, yaml, actor: str) -> None:
    _atomic_write_yaml(_manager.path, data, yaml)
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
