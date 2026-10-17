"""Settings for the memory graph (config `memory_graph:`), merged over these defaults."""
from __future__ import annotations

import copy
from typing import Any, Optional

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "codeintel": True,              # resolve episode files to codeintel symbols (needs the repo to be indexable)
    "vault_fixes": True,            # error -> fix edges from the context vault's error_fix items
    "half_life_days": 30.0,         # recency decay of edges and episodes
    "max_files": 8,                 # files per episode that become graph nodes
    "max_symbols_per_file": 4,
    "max_episodes": 5000,           # oldest episodes beyond this are pruned (their edges stay as counts)
    "scan_episodes": 800,           # recall scans this many recent episodes
    "recall_k": 5,                  # default list length per section
    "max_output_chars": 4000,       # cap of the memory_relevant tool output
    "weights": {"keyword": 0.45, "success": 0.25, "recency": 0.1, "graph": 0.2},
    "confidence_n": 3.0,            # an edge's confidence is n / (n + confidence_n)
}


def load(raw: Optional[dict] = None) -> dict:
    if raw is None:
        try:
            from bot.config import config

            raw = config.current.get("memory_graph")
        except Exception:  # noqa: BLE001 - config trouble must not break a turn
            raw = None
    out = copy.deepcopy(DEFAULTS)
    for k, v in (raw if isinstance(raw, dict) else {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def enabled(raw: Optional[dict] = None) -> bool:
    return bool(load(raw).get("enabled"))
