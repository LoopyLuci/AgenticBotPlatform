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
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

import yaml

from bot import extensions
from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.config")

CONFIG_PATH = PROJECT_ROOT / "config" / "backends.yaml"


_SECRET_KEY_NAME = re.compile(r"(?i)(secret|token|password|passwd|api[_\-]?key|private[_\-]?key|credential)")


def _is_secret_key(name: object) -> bool:
    return isinstance(name, str) and bool(_SECRET_KEY_NAME.search(name))


def _scrub(value: Any) -> Any:
    """A copy of ``value`` with every secret-named entry replaced, at any depth.

    A provider added in one reload arrives as a whole dict (``None -> {...,
    'api_key': ...}``), so masking only the top-level key name would still
    print the key inside it."""
    if isinstance(value, dict):
        return {k: ("<hidden>" if _is_secret_key(k) and v not in (None, "") else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


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
                # Never write a secret's actual value into the log, the audit
                # trail or config_history — the last two are served back
                # verbatim by GET /api/config, and the log ends up in support
                # bundles, so a raw value in the diff summary leaks it just as
                # much as putting it in the config dict itself. That covers a
                # secret-named key (api_key, token, ...) and secrets nested
                # inside a dict that was added or removed whole.
                if _is_secret_key(k):
                    changes.append(f"{path}: changed")
                else:
                    changes.append(f"{path}: {_scrub(ov)!r} -> {_scrub(nv)!r}")

    try:
        walk(old, new)
    except Exception:
        return "config changed"
    if not changes:
        return "no effective change"
    return "; ".join(changes[:5]) + (" ..." if len(changes) > 5 else "")


class ConfigManager:
    """Thread-safe, hot-reloadable view of config/backends.yaml."""

    def __init__(self, path: Path = CONFIG_PATH, missing_ok: bool = False):
        self.path = path
        # For an optional, user-created file (providers.yaml): a fresh
        # install has none, and that must mean "empty", not an import-time
        # crash. Left False for files the app can't run without.
        self.missing_ok = missing_ok
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.version = 0
        self._on_reload: list[Callable[[dict, dict], None]] = []
        self._load_initial()

    def _read_yaml(self) -> dict:
        if self.missing_ok and not self.path.exists():
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            # Not merged, and deliberately: reload() rejects a wrong-shaped root by name, and a
            # merge that quietly turned a list into {} would hide exactly that mistake.
            return data
        # <ABP_PRIVATE_DIR>/config/<name>.yaml deep-merged over the tracked file, plus whatever
        # the extensions registered. Unset, that merge is a no-op and this stays the public
        # install's config exactly as tracked.
        return extensions.merged_config(self.path.stem, data)

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

        if not isinstance(new_data, dict):
            # Syntactically valid YAML (so _read_yaml didn't raise) but the
            # wrong shape — e.g. someone turned the top-level mapping into
            # a list. Every reader in this app calls .get()/[] on this as
            # a dict; swapping in anything else would parse cleanly here
            # and then crash the first thing that touches it, possibly
            # minutes later and far from this file. Reject it the same
            # way a parse error is rejected: keep the previous good config.
            msg = f"reload failed: root must be a mapping, got {type(new_data).__name__}"
            logger.error("config reload failed, keeping previous config: %s", msg)
            return False, msg

        with self._lock:
            old_data = self._data
            if new_data == old_data:
                return False, "no effective change"
            self._data = new_data
            self.version += 1
            version = self.version

        summary = _diff_summary(old_data, new_data)
        logger.info("config reloaded -> v%d (%s)", version, summary)
        try:  # the version now running is kept too, however it was written (a hand edit included)
            from bot import config_backups

            config_backups.after_reload(self.path, actor)
        except Exception:  # noqa: BLE001
            logger.exception("could not keep a copy of the reloaded config")

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
        """Edit one key in the live config and persist it atomically to disk, then reload so the change is visible
        immediately. Goes through set_values(), which keeps the file's comments: this used to re-dump the whole file
        with yaml.safe_dump, and every caller silently stripped backends.yaml's documentation (found 2026-09-29,
        when three settings saved through it removed all 222 comment lines)."""
        self.set_values({tuple(path): value}, actor=actor)

    def set_values(self, changes: "dict[tuple[str, ...], Any]", actor: str = "dashboard") -> None:
        """Apply several key edits in ONE write and ONE reload, keeping the file's comments and layout.

        set_value() above re-dumps the whole file with yaml.safe_dump, which drops every comment; backends.yaml is
        heavily documented, so a settings page that saved through it would strip that documentation on the first
        click. This uses ruamel.yaml's round-trip mode instead (the same approach bot/providers.py takes for
        providers.yaml) and writes atomically. `changes` maps a key path, such as ("native_agent", "limits",
        "max_iterations"), to the new value.

        With a private overlay for this file (<ABP_PRIVATE_DIR>/config/<name>.yaml) the edits go THERE: the overlay
        wins the merge, so an edit written under it would never take effect, and this machine's settings must not
        land in the public checkout's tracked file."""
        if not changes:
            return
        target = self.write_path()
        from ruamel.yaml import YAML

        y = YAML(typ="rt")
        y.preserve_quotes = True
        y.width = 4096  # never re-wrap a long line
        y.indent(mapping=2, sequence=4, offset=2)
        # ruamel writes None as an empty value; keep the file's own `null`s so a save does not rewrite unrelated lines.
        y.representer.add_representer(type(None), lambda r, _v: r.represent_scalar("tag:yaml.org,2002:null", "null"))
        with open(target, encoding="utf-8") as f:
            data = y.load(f)
        if data is None:
            data = {}
        for path, value in changes.items():
            node = data
            for key in path[:-1]:
                child = node.get(key)
                if not isinstance(child, dict):
                    child = {}
                    node[key] = child
                node = child
            node[path[-1]] = value

        tmp_path = target.with_suffix(".yaml.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            y.dump(data, f)
            f.flush()
            os.fsync(f.fileno())  # on disk before the rename: a power cut right after a save left a zero-filled file
        from bot import config_backups

        config_backups.before_write(target, actor)  # the version about to be replaced is kept
        tmp_path.replace(target)  # atomic on the same filesystem
        self.reload(actor=actor)

    def overlay_path(self) -> "Path | None":
        """This file's private overlay, when ABP_PRIVATE_DIR has one (bot/extensions.py)."""
        return extensions.config_overlay(self.path.stem)

    def write_path(self) -> Path:
        """Where an edit made through ABP is written: the overlay when there is one, else the tracked file."""
        return self.overlay_path() or self.path

    async def watch_forever(self) -> None:
        """Background task: watch the config file and hot-reload on change.

        reload() itself never raises (bad YAML/wrong shape/callback
        failures are all caught internally and logged) — the real risk
        here is awatch() dying outright (a deleted watched file, a
        permission change, an OS-level file-watching backend hiccup), a
        real documented watchfiles failure mode that would otherwise
        leave config file changes silently, permanently unnoticed for
        the rest of the process's life with no visible sign short of a
        restart. Re-enters the watch fresh after a short delay instead."""
        from watchfiles import awatch

        while True:
            if self.missing_ok and not self.path.exists():
                await asyncio.sleep(5)            # an optional file (providers.yaml) that does not exist yet
                if self.path.exists():
                    self.reload(actor="file-watch")
                continue
            try:
                # The overlay is part of what this config is, so a hand edit there reloads too.
                watched = [str(p) for p in (self.path, self.overlay_path()) if p is not None]
                async for _changes in awatch(*watched):
                    self.reload(actor="file-watch")
            except Exception:
                logger.exception("config file watcher crashed — restarting it")
                await asyncio.sleep(2)


# Module-level singleton — every part of the app shares one config view.
config = ConfigManager()
