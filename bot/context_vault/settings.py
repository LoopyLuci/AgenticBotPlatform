"""Settings for the context vault (config/backends.yaml `context_vault:`), merged over these defaults.

Nothing here touches disk: the vault is off until `enabled: true`, and the default directory tier lives under
ABP's own data folder. A tier is {name, kind: ram|dir, path (dir), max_bytes (0 = no cap), media (a hint only)}.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Optional

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "tiers": [
        {"name": "ram", "kind": "ram", "max_bytes": 256 * 1024 * 1024, "media": "ram"},
        {"name": "store", "kind": "dir", "path": "", "max_bytes": 0, "media": "ssd"},
    ],
    "inject_for_local": True,          # a small "Relevant context" block for local-model turns (never for API turns)
    "inject_k": 6,
    "inject_budget_tokens": 600,
    "capture": {
        "compaction": True, "turn_end": True, "board": True, "swarm": True,
        "api_turns": True, "local_turns": False,
        "queue_max": 200, "min_chars": 24, "max_chars": 4000,
    },
    "dedup_threshold": 0.92,           # embedding cosine; without embeddings a word-overlap check at 0.9 is used
    "embeddings": {"enabled": False, "model": "", "timeout_s": 3.0},
    "summarise": {"enabled": False, "model": ""},    # a LOCAL model (ModelMistress) turns a summary into items
    "maintenance_interval_s": 60,
    "promote_score": 0.35,
    "min_residency_s": 300,            # an item that just moved does not move again for this long
    "demote_target": 0.9,              # a tier over its cap is emptied down to this share of it
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(raw: Optional[dict] = None) -> dict:
    """The effective settings. `raw` replaces the configured block (tests, embedding)."""
    if raw is None:
        try:
            from bot.config import config

            raw = config.current.get("context_vault")
        except Exception:  # noqa: BLE001 - config trouble must not break a turn
            raw = None
    return _merge(DEFAULTS, raw if isinstance(raw, dict) else {})


def enabled(raw: Optional[dict] = None) -> bool:
    return bool(load(raw).get("enabled"))


def data_dir() -> Path:
    from bot import envfile

    home = getattr(envfile, "ABP_HOME_ACTIVE", None)
    return Path(home) if home else Path(envfile.PROJECT_ROOT) / "data"


def default_store_dir() -> Path:
    return data_dir() / "context_vault" / "store"


def index_path() -> Path:
    return data_dir() / "context_vault" / "index.db"
