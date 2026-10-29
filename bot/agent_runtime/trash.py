"""Trash for files an agent removes (recovery plan P0, T2; contract C3 in docs/recovery-spec.md).

An agent's file removals (apply_patch deletes, and the source side of renames) move the file here instead of
unlinking it. Layout: <root>/<YYYY-MM-DD>/<drive>/<path without its drive>, with a sidecar <item>.trash.json that
records the original path, size, SHA-256, session, reason, trashed_at and retention_days.

Restore never overwrites a live path. Purge is an owner action: the terminal CLI below, never an agent tool, and it
refuses until retention is over. Nothing here is a background job.

    python -m bot.agent_runtime.trash list
    python -m bot.agent_runtime.trash restore <id>
    python -m bot.agent_runtime.trash purge <id> --yes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

RETENTION_DAYS = 30
SIDECAR = ".trash.json"
_DAY = 86400


class TrashError(Exception):
    """A trash operation was refused: the path is occupied, retention is not over, or the item is unknown."""


def trash_root() -> Path:
    from bot import envfile

    home = getattr(envfile, "ABP_HOME_ACTIVE", None)
    return (Path(home) if home else Path(envfile.PROJECT_ROOT) / "data") / "trash"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _drive_tag(path: Path) -> str:
    return path.drive.rstrip(":\\/").upper() or "root"


def _unique(dest: Path) -> Path:
    if not os.path.lexists(dest):
        return dest
    n = 1
    while os.path.lexists(dest.with_name(f"{dest.name}.{n}")):
        n += 1
    return dest.with_name(f"{dest.name}.{n}")


def _sidecar(item_path: Path) -> Path:
    return item_path.with_name(item_path.name + SIDECAR)


def _move(src: Path, dst: Path) -> None:
    """Rename when the volume allows it. Otherwise copy, verify the copy, and only then remove the source."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass                                    # another volume: fall through to copy-verify-remove
    part = dst.with_name(dst.name + ".trash-part")
    shutil.copy2(src, part)
    if _sha256(part) != _sha256(src):
        part.unlink(missing_ok=True)
        raise TrashError(f"the copy of {src} did not verify; the original is untouched")
    os.replace(part, dst)
    src.unlink()


def _prune_empty(path: Path, base: Path) -> None:
    while path != base and path.is_dir() and not any(path.iterdir()):
        path.rmdir()
        path = path.parent


def _resolve(item_id: str, base: Path) -> Path:
    if not item_id or Path(item_id).is_absolute() or ".." in Path(item_id).parts:
        raise TrashError(f"not a trash id: {item_id!r}")
    return base / item_id


def trash_file(workspace: Path, path: Path, *, session: str, reason: str, root: Optional[Path] = None,
               now: Optional[float] = None) -> dict:
    """Move one regular file into the trash and write its sidecar. Returns the item record."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise TrashError(f"{path} is not a regular file")
    base = root or trash_root()
    now = now or time.time()
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    dest = _unique(base / day / _drive_tag(path) / path.relative_to(path.anchor))
    digest = _sha256(path)
    size = path.stat().st_size
    _move(path, dest)
    item = {"original": str(path), "workspace": str(Path(workspace)), "size": size, "sha256": digest,
            "session": session, "reason": reason, "trashed_at": now, "retention_days": RETENTION_DAYS}
    tmp = _sidecar(dest).with_name(_sidecar(dest).name + ".part")
    tmp.write_text(json.dumps(item), encoding="utf-8")
    os.replace(tmp, _sidecar(dest))
    return {**item, "id": dest.relative_to(base).as_posix()}


def items(root: Optional[Path] = None, *, workspace: Optional[Path] = None) -> list[dict]:
    """Every trashed item, newest first. With `workspace`, only the items that came from that folder."""
    base = root or trash_root()
    if not base.exists():
        return []
    out = []
    for sidecar in base.rglob(f"*{SIDECAR}"):
        try:
            item = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        item_id = sidecar.relative_to(base).as_posix()[: -len(SIDECAR)]
        if workspace is not None and not _is_within(Path(item["original"]), Path(workspace)):
            continue
        out.append({**item, "id": item_id, "present": (base / item_id).is_file(),
                    "days_left": round(max(0.0, item["retention_days"] - (time.time() - item["trashed_at"]) / _DAY), 1)})
    return sorted(out, key=lambda i: i["trashed_at"], reverse=True)


def _is_within(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _find(item_id: str, base: Path) -> dict:
    for item in items(base):
        if item["id"] == item_id:
            return item
    raise TrashError(f"no trashed item {item_id!r}")


def restore(item_id: str, *, root: Optional[Path] = None, workspace: Optional[Path] = None) -> dict:
    """Move a trashed file back to its original path. Refused if that path exists again, or if `workspace` is
    given and the original lies outside it."""
    base = root or trash_root()
    item = _find(item_id, base)
    original = Path(item["original"])
    if workspace is not None and not _is_within(original, Path(workspace)):
        raise TrashError(f"{original} is outside this workspace")
    if os.path.lexists(original):
        raise TrashError(f"{original} exists again; restore refuses to overwrite it")
    src = _resolve(item_id, base)
    if not src.is_file():
        raise TrashError(f"the trashed copy of {item_id} is missing")
    if _sha256(src) != item["sha256"]:
        raise TrashError(f"the trashed copy of {item_id} no longer matches its recorded SHA-256; nothing was moved")
    _move(src, original)
    _sidecar(src).unlink(missing_ok=True)
    _prune_empty(src.parent, base)
    return {"restored": str(original), "id": item_id}


def purge(item_id: str, *, owner: bool, root: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """Delete one trashed item for good. Owner only, and only after its retention period."""
    if not owner:
        raise TrashError("purging is an owner action (the terminal CLI); agents cannot purge")
    base = root or trash_root()
    item = _find(item_id, base)
    age_days = ((now or time.time()) - item["trashed_at"]) / _DAY
    if age_days < item["retention_days"]:
        raise TrashError(f"retention not over: {age_days:.1f} of {item['retention_days']} days")
    src = _resolve(item_id, base)
    src.unlink(missing_ok=True)
    _sidecar(src).unlink(missing_ok=True)
    _prune_empty(src.parent, base)
    return {"purged": item_id, "bytes": item["size"]}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.agent_runtime.trash", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=None, help="trash folder (default: the ABP data folder's trash)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list trashed items")
    p_restore = sub.add_parser("restore", help="restore an item to its original path")
    p_restore.add_argument("id")
    p_purge = sub.add_parser("purge", help="delete an item for good (after retention)")
    p_purge.add_argument("id")
    p_purge.add_argument("--yes", action="store_true", help="confirm the permanent delete")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            result = items(args.root)
        elif args.cmd == "restore":
            result = restore(args.id, root=args.root)
        else:
            if not args.yes:
                raise TrashError("purge deletes for good; repeat with --yes")
            result = purge(args.id, owner=True, root=args.root)
    except TrashError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
