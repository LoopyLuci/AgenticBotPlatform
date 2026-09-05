"""Read-only, allowlisted file browsing/download (config/file_share.yaml).

Lets an authenticated caller (dashboard token or a paired mobile device
key — the same auth every other API route already uses, reachable over
Tailscale Funnel from anywhere) browse and download from specific
directories on this machine the operator has explicitly named as
"roots". This is not a general filesystem browser: nothing outside an
explicitly configured root is ever reachable, and a root only exists
because the operator added it on purpose — the same "no new attack
surface beyond what's already trusted" bar as everything else exposed
via Funnel.

Adding/removing a root is desktop-token-only (see bot/dashboard/server.py
— a meaningfully bigger grant than browsing an already-configured one; a
stolen mobile device key must never be able to mount a new root, e.g.
"C:\\"). Browsing/downloading within an already-configured root uses the
token-or-api-key gate the rest of the API uses, since reaching a file
from anywhere is the whole point.

Reuses bot/config.py's ConfigManager for the read side (hot reload,
`.current`, `on_reload()`) and a ruamel.yaml round-trip for writes, same
pattern bot/providers.py and bot/hermes_config.py already use — a plain
yaml.safe_load/safe_dump round trip would silently strip any comments an
operator adds to this file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml as _pyyaml

from bot.config import ConfigManager
from bot.envfile import PROJECT_ROOT

FILE_SHARE_PATH = PROJECT_ROOT / "config" / "file_share.yaml"

# Seeded into a fresh config/file_share.yaml the first time this module is
# ever imported on a machine, so "grab the Android APK from anywhere"
# works with zero setup — still just an ordinary root the operator can
# rename/remove like any other afterward, nothing here is special-cased
# beyond existing by default.
_DEFAULT_ROOTS = {
    "android-builds": str(PROJECT_ROOT / "android-app" / "app" / "build" / "outputs" / "apk"),
}


def _seed_if_missing(path: Path) -> None:
    """Creates `path` with the default root(s) if it doesn't exist yet.
    Must run before ConfigManager's constructor, which — like every other
    config/*.yaml-backed module in this codebase — requires the file to
    already exist; never overwrites a file that's already there, even an
    empty one where the operator deliberately removed every default
    root."""
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        _pyyaml.safe_dump({"roots": dict(_DEFAULT_ROOTS)}, f)


_seed_if_missing(FILE_SHARE_PATH)
_manager = ConfigManager(path=FILE_SHARE_PATH)


class PathEscapeError(ValueError):
    """A requested relative path would resolve outside its root — a
    traversal attempt (../..), an absolute-path override, or a symlink
    pointing out of bounds."""


def _yaml():
    from ruamel.yaml import YAML

    y = YAML(typ="rt")  # round-trip: preserves comments, key order, quoting style
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load_yaml_or_empty(path: Path, yaml) -> dict[str, Any]:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.load(f) or {}


def _atomic_write_yaml(path: Path, data: dict, yaml) -> None:
    tmp_path = path.with_suffix(".yaml.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    tmp_path.replace(path)  # atomic on the same filesystem


def on_reload(callback) -> None:
    _manager.on_reload(callback)


def reload(actor: str = "file-watch") -> tuple[bool, str]:
    """Re-reads config/file_share.yaml from disk — used after
    bot/snapshots.py restores a previous copy of the file, same as
    bot.config.config.reload() is used for backends.yaml."""
    return _manager.reload(actor=actor)


def list_roots() -> dict[str, dict]:
    """name -> {"path": str, "exists": bool} — exists is live-checked on
    every call (a root's target directory can come and go, e.g. before a
    build has ever run) rather than cached, since a stale True/False
    would be actively misleading for a "can I actually get a file from
    here" question."""
    roots = _manager.current.get("roots") or {}
    return {name: {"path": path, "exists": Path(path).is_dir()} for name, path in roots.items()}


def get_root_path(name: str) -> Optional[Path]:
    roots = _manager.current.get("roots") or {}
    raw = roots.get(name)
    return Path(raw) if raw else None


def resolve_safe_path(root_name: str, rel_path: str = "") -> Path:
    """The real, on-disk path for `rel_path` under root `root_name` —
    raises KeyError if the root isn't configured, PathEscapeError if the
    resolved path would land outside that root. Safe even against a
    `rel_path` that's itself absolute (e.g. "/etc/passwd" or "C:\\Windows")
    — pathlib's `/` operator would otherwise silently replace the root
    entirely, but the check here is on the final *resolved* path against
    the root, not on how the two were joined, so an override still lands
    outside root and still raises. Mirrors the exact
    resolve()+is_relative_to() guard bot/attachments.py's attachment
    routes already use."""
    root = get_root_path(root_name)
    if root is None:
        raise KeyError(root_name)
    root_resolved = root.resolve()
    candidate = (root / rel_path).resolve() if rel_path else root_resolved
    if not candidate.is_relative_to(root_resolved):
        raise PathEscapeError(f"{rel_path!r} escapes root {root_name!r}")
    return candidate


def list_dir(root_name: str, rel_path: str = "") -> list[dict]:
    """Non-recursive listing of one directory under a root —
    {"name", "is_dir", "size", "modified_at"} per entry (size is None for
    a directory), directories first then files, alphabetical within each
    group. Raises KeyError/PathEscapeError (see resolve_safe_path) or
    NotADirectoryError/FileNotFoundError for a bad rel_path."""
    target = resolve_safe_path(root_name, rel_path)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    entries = []
    for child in target.iterdir():
        is_dir = child.is_dir()
        stat = child.stat()
        entries.append({
            "name": child.name,
            "is_dir": is_dir,
            "size": None if is_dir else stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def add_root(name: str, path: str, actor: str = "dashboard") -> None:
    if not name or not name.strip():
        raise ValueError("a name is required")
    if not path or not path.strip():
        raise ValueError("a path is required")
    p = Path(path.strip())
    if not p.is_dir():
        raise ValueError(f"{path!r} is not a directory this machine can see")
    yaml = _yaml()
    data = _load_yaml_or_empty(_manager.path, yaml)
    roots = data.get("roots")
    if roots is None:
        roots = {}
        data["roots"] = roots
    roots[name.strip()] = str(p)
    _write(data, yaml, actor)


def remove_root(name: str, actor: str = "dashboard") -> bool:
    yaml = _yaml()
    data = _load_yaml_or_empty(_manager.path, yaml)
    roots = data.get("roots") or {}
    if name not in roots:
        return False
    del roots[name]
    _write(data, yaml, actor)
    return True


def _write(data: dict, yaml, actor: str) -> None:
    _atomic_write_yaml(_manager.path, data, yaml)
    _manager.reload(actor=actor)
