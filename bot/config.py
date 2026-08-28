"""Hot-reloadable router configuration.

The YAML file at config/backends.yaml is the single source of truth for
backend routing, timeouts, and feature toggles. This module loads it once,
then watches it for changes and swaps the in-memory copy atomically —
readers always see either the old config or the new one in full, never a
half-applied edit — and records every change to config_history/audit_log
so the dashboard's "Resilience" timeline is real data, not decoration.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from pathlib import Path
from typing import Any, Callable

import yaml

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.config")

CONFIG_PATH = PROJECT_ROOT / "config" / "backends.yaml"


def _diff_summary(old: dict, new: dict) -> str:
    """Best-effort one-line summary of what changed, for the audit trail."""
    changes = []

    def walk(o, n, prefix=""):
        keys = set(o.keys()) | set(n.keys()) if isinstance(o, dict) and isinstance(n, dict) else set()
        for k in keys:
            ov = o.get(k) if isinstance(o, dict) else None
            nv = n.get(k) if isinstance(n, dict) else None
            path = f"{prefix}{k}"
            if isinstance(ov, dict) and isinstance(nv, dict):
                walk(ov, nv, prefix=f"{path}.")
            elif ov != nv:
                # Never write a secret's actual value into the audit
                # trail/config_history — both are served back verbatim by
                # GET /api/config, which (like most reads here) has no auth
                # gate, so a raw value in the diff summary would leak it
                # just as much as putting it in the config dict itself.
                if k == "secret":
                    changes.append(f"{path}: changed")
                else:
                    changes.append(f"{path}: {ov!r} -> {nv!r}")

    try:
        walk(old, new)
    except Exception:
        return "config changed"
    if not changes:
        return "no effective change"
    return "; ".join(changes[:5]) + (" ..." if len(changes) > 5 else "")


class ConfigManager:
    """Thread-safe, hot-reloadable view of config/backends.yaml."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.version = 0
        self._on_reload: list[Callable[[dict, dict], None]] = []
        self._load_initial()

    def _read_yaml(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def read_raw(self) -> dict:
        """Fresh read straight from disk, bypassing the in-memory cache —
        for a caller (bot/providers.py) that needs to read-modify-write a
        section of the file this manager doesn't otherwise interpret."""
        return self._read_yaml()

    def _load_initial(self) -> None:
        data = self._read_yaml()
        with self._lock:
            self._data = data
            self.version = 1

    @property
    def current(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def on_reload(self, callback: Callable[[dict, dict], None]) -> None:
        """Register a callback(old_config, new_config) fired after a hot reload."""
        self._on_reload.append(callback)

    def reload(self, actor: str = "file-watch") -> tuple[bool, str]:
        """Reload from disk. Returns (changed, summary). Never raises on bad
        YAML — a broken edit is logged and the previous good config stays
        live, so a typo in the file can't take the bot down."""
        try:
            new_data = self._read_yaml()
        except Exception as exc:
            logger.error("config reload failed, keeping previous config: %s", exc)
            return False, f"reload failed: {exc}"

        with self._lock:
            old_data = self._data
            if new_data == old_data:
                return False, "no effective change"
            self._data = new_data
            self.version += 1
            version = self.version

        summary = _diff_summary(old_data, new_data)
        logger.info("config reloaded -> v%d (%s)", version, summary)

        for cb in self._on_reload:
            try:
                cb(old_data, new_data)
            except Exception:
                logger.exception("on_reload callback failed")

        try:
            from bot import db

            db.record_config_version(version=version, actor=actor, summary=summary)
            db.log_audit(actor=actor, action="config_reload", detail=summary)
        except Exception:
            logger.exception("failed to record config reload in db")

        return True, summary

    def set_value(self, path: list[str], value: Any, actor: str = "dashboard") -> None:
        """Edit one key in the live config and persist it atomically to disk
        (write-to-temp-then-replace), then reload so the change is visible
        immediately. Used by dashboard "set default backend" / toggle controls."""
        data = self._read_yaml()
        node = data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

        tmp_path = self.path.with_suffix(".yaml.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        tmp_path.replace(self.path)  # atomic on the same filesystem

        self.reload(actor=actor)

    async def watch_forever(self) -> None:
        """Background task: watch the config file and hot-reload on change."""
        from watchfiles import awatch

        async for _changes in awatch(str(self.path)):
            self.reload(actor="file-watch")


# Module-level singleton — every part of the app shares one config view.
config = ConfigManager()
