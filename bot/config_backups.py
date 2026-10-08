"""A copy of a config file before every write, and a way back.

Every writer of ABP's own YAML config (bot/config.py's ConfigManager.set_values, providers.yaml, file_share.yaml)
calls `before_write(path, actor)` just before it replaces the file, so the version about to be overwritten is kept.
A change made by hand is kept too: ConfigManager.reload calls `after_reload(path)` when the file changed on disk, so
the history has every version the app has run with, however it was written.

Versions live in `data/config-backups/<file name>/<UTC time>__<actor>.yaml`, identical content is stored once (a
write that changes nothing adds nothing), and the newest `KEEP` per file are kept. Restoring a version saves the
current one first, so a restore can itself be undone, then writes the file atomically and reloads it.

What a version contains is shown masked: every secret-named value (api_key, token, password, ...) becomes
`<hidden>`, the same rule the config audit trail uses. The files themselves are copied verbatim, because a restore
has to put the real keys back.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.config_backups")

ROOT = PROJECT_ROOT / "data" / "config-backups"
#: Only files in this folder are kept (see _keep).
CONFIG_DIR = PROJECT_ROOT / "config"
KEEP = 100
_NAME = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")
_VERSION = re.compile(r"^\d{8}T\d{6}(\d{3})?Z__[A-Za-z0-9._-]{1,40}\.ya?ml$")


def _tracked() -> dict[str, Path]:
    """The config files ABP writes itself, by file name."""
    from bot import config as config_mod

    out = {Path(config_mod.CONFIG_PATH).name: Path(config_mod.CONFIG_PATH)}
    try:
        from bot import providers

        out[providers.PROVIDERS_PATH.name] = providers.PROVIDERS_PATH
    except Exception:  # noqa: BLE001 - a missing optional writer just is not tracked
        pass
    try:
        from bot import file_share

        out[file_share.FILE_SHARE_PATH.name] = file_share.FILE_SHARE_PATH
    except Exception:  # noqa: BLE001
        pass
    return out


def _dir(name: str, root: Optional[Path] = None) -> Path:
    if not _NAME.match(name):
        raise ValueError(f"not a config file name: {name!r}")
    return (root or ROOT) / name


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _versions(d: Path) -> list[Path]:
    return sorted((p for p in d.glob("*.y*ml") if _VERSION.match(p.name)), key=lambda p: p.name, reverse=True)


def _keep(path: Path, actor: str, reason: str, root: Optional[Path] = None) -> Optional[dict]:
    """Store the file's current content as a version, unless the newest version already has it. Only ABP's own
    config files are kept (a test's temporary config never lands in the real history), unless `root` is given."""
    if root is None and (path.resolve() not in {p.resolve() for p in _tracked().values()}
                         or path.resolve().parent != Path(CONFIG_DIR).resolve()):
        return None  # a test points a writer at a temporary file: that never belongs in the real history
    try:
        data = path.read_bytes()
    except OSError:
        return None  # nothing there yet: the first write creates it
    d = _dir(path.name, root)
    d.mkdir(parents=True, exist_ok=True)
    existing = _versions(d)
    if existing and _sha(existing[0].read_bytes()) == _sha(data):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}Z"
    who = re.sub(r"[^A-Za-z0-9._-]", "-", actor or "unknown")[:40] or "unknown"
    target = d / f"{stamp}__{who}{path.suffix}"
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    meta = d / (target.name + ".reason")
    meta.write_text(reason[:300], encoding="utf-8")
    for old in existing[KEEP - 1:]:
        for p in (old, d / (old.name + ".reason")):
            try:
                p.unlink()
            except OSError:
                pass
    return {"file": path.name, "version": target.name}


def before_write(path: Path, actor: str = "unknown", reason: str = "before a change", *,
                 root: Optional[Path] = None) -> Optional[dict]:
    """Keep the version a writer is about to overwrite. Never raises: a backup that fails must not block the
    change, but it is logged."""
    try:
        return _keep(Path(path), actor, reason, root)
    except Exception:  # noqa: BLE001
        logger.exception("could not keep a copy of %s before writing it", path)
        return None


def after_reload(path: Path, actor: str = "file-watch", *, root: Optional[Path] = None) -> Optional[dict]:
    """Keep the version now on disk (a hand edit, or any writer that did not call before_write)."""
    try:
        return _keep(Path(path), actor, "the file as it was loaded", root)
    except Exception:  # noqa: BLE001
        logger.exception("could not keep a copy of %s after reloading it", path)
        return None


def _yaml_load(data: bytes) -> Any:
    import yaml

    try:
        return yaml.safe_load(data.decode("utf-8", errors="replace")) or {}
    except Exception:  # noqa: BLE001
        return None


def list_files(root: Optional[Path] = None) -> list[dict]:
    """Every tracked config file, with how many versions are kept for it."""
    out = []
    for name, path in _tracked().items():
        d = _dir(name, root)
        out.append({"file": name, "exists": path.is_file(), "versions": len(_versions(d)) if d.is_dir() else 0})
    return out


def list_versions(name: str, root: Optional[Path] = None) -> list[dict]:
    """A file's versions, newest first: when, who, why, and what differs from the file as it is now."""
    from bot.config import _diff_summary

    d = _dir(name, root)
    if not d.is_dir():
        return []
    live = _tracked().get(name)
    current = _yaml_load(live.read_bytes()) if live and live.is_file() else {}
    out = []
    for p in _versions(d):
        stamp, _, rest = p.name.partition("__")
        reason_file = d / (p.name + ".reason")
        old = _yaml_load(p.read_bytes())
        diff = _diff_summary(current, old) if isinstance(old, dict) and isinstance(current, dict) else "unreadable"
        out.append({
            "version": p.name,
            "time": datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat(),
            "actor": rest.rsplit(".", 1)[0],
            "reason": reason_file.read_text(encoding="utf-8") if reason_file.is_file() else "",
            "bytes": p.stat().st_size,
            "restoring_changes": diff,
        })
    return out


def _version_path(name: str, version: str, root: Optional[Path] = None) -> Path:
    if not _VERSION.match(version or ""):
        raise ValueError(f"not a version name: {version!r}")
    p = _dir(name, root) / version
    if not p.is_file():
        raise FileNotFoundError(f"no version {version} of {name}")
    return p


def read_masked(name: str, version: str, root: Optional[Path] = None) -> str:
    """A version's content with every secret-named value replaced by <hidden>."""
    import yaml

    from bot.config import _scrub

    data = _yaml_load(_version_path(name, version, root).read_bytes())
    if data is None:
        return "(this version is not valid YAML)"
    return yaml.safe_dump(_scrub(data), sort_keys=False, allow_unicode=True)


def restore(name: str, version: str, actor: str = "dashboard", root: Optional[Path] = None) -> dict:
    """Put a version back: the current file is kept first (so this can be undone), the version is written
    atomically, and the file is reloaded so the change takes effect now."""
    src = _version_path(name, version, root)
    live = _tracked().get(name)
    if live is None:
        raise ValueError(f"{name} is not a config file ABP restores")
    if _yaml_load(src.read_bytes()) is None:
        raise ValueError(f"version {version} of {name} is not valid YAML; it was not restored")
    before_write(live, actor, f"before restoring {version}", root=root)
    tmp = live.with_suffix(live.suffix + ".restore.tmp")
    shutil.copyfile(src, tmp)
    tmp.replace(live)
    changed, summary = _reload(name, actor)
    return {"file": name, "restored": version, "reloaded": changed, "summary": summary}


def _reload(name: str, actor: str) -> tuple[bool, str]:
    from bot import config as config_mod

    if name == Path(config_mod.CONFIG_PATH).name:
        return config_mod.config.reload(actor=actor)
    if name == "providers.yaml":
        from bot import providers

        return providers.reload(actor=actor)
    if name == "file_share.yaml":
        from bot import file_share

        return file_share.reload(actor=actor)
    return False, "not reloaded"
