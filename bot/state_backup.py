"""State backup (recovery plan P0, T1; contract C4 in docs/recovery-spec.md).

Stages every ABP-owned store into one folder and stores that folder in a File Server backup repository (encrypted,
deduplicated, versioned, checked). The staged folder carries a manifest.json: each file's SHA-256, each SQLite
database's table row counts, what was left out on purpose, and every *.db that no store claims (the coverage audit).

Stores. Every path is resolved at run time; nothing is written for a particular machine:
    config     config/backends.yaml and config/providers.yaml
    private    <ABP_PRIVATE_DIR>/config/*.yaml (the owner's overlays, where the secrets live)
    databases  bot.db, the context-vault index, the storage-tiering index, every File Server database
    vault      vault.enc (ciphertext). vault.key is NOT staged: it is listed as excluded
    tiers      every context-vault tier folder of kind "dir", wherever it lives (it may sit outside the data folder)
    runtime    the unit runtime's manifest, when the engine is reachable

Left out on purpose: snapshots and this module's own folders (they are backups already), trash (its own store, C3),
checkpoint_store (git history of agent workspaces), and data/agent (rebuildable indexes and worktrees).

The drill restores the newest state backup into a temporary folder under the data folder and checks every SHA-256,
every SQLite integrity_check, and every recorded row count. It never touches the live stores.

    python -m bot.state_backup run   --repo <folder>     [--password-env ABP_STATE_BACKUP_PASSWORD]
    python -m bot.state_backup drill --repo <folder>     [--password-env ...]
    python -m bot.state_backup status

The repository password is never stored. Without the environment variable it is asked for at the terminal.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

FORMAT = 1
TAG = "abp-state"
KEEP = {"keep_last": 10}
CONFIG_FILES = ("config/backends.yaml", "config/providers.yaml")
# Folders under the data folder that are never walked for *.db (own stores, backups, rebuildable state).
SKIP_DIRS = frozenset({"snapshots", "state_backup", "trash", "checkpoint_store", "agent"})
# The databases every ABP install has, relative to the data folder. Anything else found under data is "unlisted".
KNOWN_DBS = ("bot.db", "storage_tiering.db", "context_vault/index.db")
DEFAULT_PASSWORD_ENV = "ABP_STATE_BACKUP_PASSWORD"

Log = Callable[[str], None]


class StateBackupError(Exception):
    """A state backup or drill could not run."""


@dataclass
class Plan:
    files: list[tuple[str, Path]] = field(default_factory=list)      # (staged path, live file)
    sqlite: list[tuple[str, Path]] = field(default_factory=list)     # (staged path, live database)
    trees: list[tuple[str, Path]] = field(default_factory=list)      # (staged prefix, live folder, copied file by file)
    excluded: list[dict] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unlisted_dbs(data: Path, known: set[Path]) -> list[str]:
    """Every *.db under the data folder that no store claims, skipping SKIP_DIRS. Walks by hand so it can prune."""
    found = []
    if not data.is_dir():
        return found
    for root, dirs, names in os.walk(data):
        here = Path(root)
        dirs[:] = [d for d in dirs if not (here == data and d in SKIP_DIRS)]
        for name in names:
            p = here / name
            if name.endswith(".db") and p not in known:
                found.append(p.relative_to(data).as_posix())
    return sorted(found)


def collect(*, data: Path, project_root: Path, private_dir: Optional[Path], tier_dirs: list[tuple[str, Path]],
            fileserver_dir: Optional[Path] = None) -> Plan:
    """Decide what goes into a state backup. Pure: reads paths, copies nothing."""
    plan = Plan()
    for rel in CONFIG_FILES:
        src = project_root / rel
        if src.is_file():
            plan.files.append((rel, src))
    if private_dir is not None and (private_dir / "config").is_dir():
        for src in sorted((private_dir / "config").glob("*.yaml")):
            plan.files.append((f"private/config/{src.name}", src))
    known: set[Path] = set()
    for rel in KNOWN_DBS:
        src = data / rel
        known.add(src)
        if src.is_file():
            plan.sqlite.append((f"db/{rel}", src))
    fs_dir = fileserver_dir if fileserver_dir is not None else data / "fileserver"
    if fs_dir.is_dir():
        for src in sorted(fs_dir.glob("*.db")):
            known.add(src)
            plan.sqlite.append((f"db/fileserver/{src.name}", src))
    vault = data / "vault.enc"
    if vault.is_file():
        plan.files.append(("vault/vault.enc", vault))
    if (data / "vault.key").exists():
        plan.excluded.append({"label": "vault key", "path": "vault.key",
                              "reason": "kept out of the backup on purpose; keep your own copy or use ABP_VAULT_KEY"})
    for name, folder in tier_dirs:
        if folder.is_dir():
            plan.trees.append((f"tiers/{name}", folder))
        else:
            plan.excluded.append({"label": f"context-vault tier {name}", "path": str(folder), "reason": "folder does not exist"})
    plan.unlisted = _unlisted_dbs(data, known)
    return plan


def _backup_sqlite(src: Path, out: Path) -> dict:
    """Copy one database with SQLite's online backup API, and return its row counts."""
    out.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(str(out))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return _table_counts(out)


def _table_counts(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def stage(plan: Plan, dest: Path, *, runtime: Optional[dict] = None) -> dict:
    """Copy the plan into a new folder and write manifest.json into it. Returns the manifest."""
    dest.mkdir(parents=True, exist_ok=False)
    entries = []
    for rel, src in plan.files:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        entries.append({"path": rel, "sha256": _sha256(out), "size": out.stat().st_size, "kind": "file"})
    for rel, src in plan.sqlite:
        out = dest / rel
        tables = _backup_sqlite(src, out)
        entries.append({"path": rel, "sha256": _sha256(out), "size": out.stat().st_size, "kind": "sqlite", "tables": tables})
    for prefix, folder in plan.trees:
        for src in sorted(folder.rglob("*")):
            if src.is_symlink() or not src.is_file():
                continue
            rel = f"{prefix}/{src.relative_to(folder).as_posix()}"
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            entries.append({"path": rel, "sha256": _sha256(out), "size": out.stat().st_size, "kind": "file"})
    if runtime is not None:
        out = dest / "runtime.json"
        out.write_text(json.dumps(runtime), encoding="utf-8")
        entries.append({"path": "runtime.json", "sha256": _sha256(out), "size": out.stat().st_size, "kind": "file"})
    manifest = {"format": FORMAT, "created_at": _now(), "files": entries, "excluded": plan.excluded, "unlisted": plan.unlisted}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def verify(root: Path) -> dict:
    """Check a restored (or staged) folder against its manifest. Returns a summary; ok is True only when all pass."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    bad_files, bad_dbs = [], []
    for entry in manifest["files"]:
        path = root / entry["path"]
        if not path.is_file() or _sha256(path) != entry["sha256"]:
            bad_files.append(entry["path"])
            continue
        if entry["kind"] == "sqlite":
            try:
                conn = sqlite3.connect(str(path))
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    conn.close()
                ok = integrity == "ok" and _table_counts(path) == entry["tables"]
            except sqlite3.DatabaseError:
                ok = False
            if not ok:
                bad_dbs.append(entry["path"])
    return {"files": len(manifest["files"]), "bad_files": bad_files,
            "databases": sum(1 for e in manifest["files"] if e["kind"] == "sqlite"), "bad_databases": bad_dbs,
            "excluded": manifest["excluded"], "unlisted": manifest["unlisted"],
            "ok": not bad_files and not bad_dbs}


def _repo(path: Path, password: str, *, create: bool):
    from bot.fileserver import backup as fs_backup

    if (path / "config.json").exists():
        return fs_backup.Repo(path, password)
    if not create:
        raise StateBackupError(f"{path} is not a backup repository yet (run a state backup first)")
    return fs_backup.Repo.create(path, password)


def _runtime_part() -> Optional[dict]:
    try:
        from bot.runtime import snapshot as runtime_snapshot

        return runtime_snapshot.export()
    except Exception:  # noqa: BLE001 - best effort, as in bot/snapshots.py
        return None


def run(*, repo: Path, password: str, data: Path, project_root: Path, private_dir: Optional[Path],
        tier_dirs: list[tuple[str, Path]], fileserver_dir: Optional[Path] = None, log: Log = lambda m: None) -> dict:
    """Stage every store and store it in the repository. Returns the snapshot summary."""
    from bot.fileserver import backup as fs_backup

    plan = collect(data=data, project_root=project_root, private_dir=private_dir, tier_dirs=tier_dirs,
                   fileserver_dir=fileserver_dir)
    staging = data / "state_backup" / "staging" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        manifest = stage(plan, staging, runtime=_runtime_part())
        r = _repo(repo, password, create=True)
        res = fs_backup.backup(r, [str(staging)], tags=[TAG], log=log)
        forgotten = fs_backup.forget(r, **KEEP)
        if forgotten.get("forgotten"):
            fs_backup.prune(r)
    finally:
        shutil.rmtree(staging, ignore_errors=True)       # our own staged copy; the repository holds the data
    summary = {"snapshot": res["snapshot"], "files": len(manifest["files"]), "bytes": res["bytes"],
               "excluded": manifest["excluded"], "unlisted": manifest["unlisted"], "at": _now()}
    _write_status(data, {"last_run": summary})
    return summary


def drill(*, repo: Path, password: str, data: Path, log: Log = lambda m: None) -> dict:
    """Restore the newest state backup into a temporary folder and verify it. The live stores are never read here."""
    from bot.fileserver import backup as fs_backup

    r = _repo(repo, password, create=False)
    snaps = [s for s in r.snapshots() if TAG in s.get("tags", [])]
    if not snaps:
        raise StateBackupError("this repository holds no state backup yet")
    sid = snaps[-1]["id"]
    scratch = data / "state_backup" / "drill"
    scratch.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="drill-", dir=scratch))
    try:
        restored = fs_backup.restore(r, sid, str(tmp), log=log)
        report = verify(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)            # our scratch copy of the restore
    report = {"snapshot": sid, "restored_files": restored["files"], **report, "at": _now()}
    _write_status(data, {"last_drill": report})
    return report


def _status_path(data: Path) -> Path:
    return data / "state_backup" / "status.json"


def _write_status(data: Path, update: dict) -> None:
    path = _status_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = status(data)
    current.update(update)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def status(data: Path) -> dict:
    path = _status_path(data)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _defaults() -> tuple[Path, Path, Optional[Path], list[tuple[str, Path]]]:
    """The data folder, the project root, the private overlay folder and the context-vault directory tiers."""
    from bot import envfile
    from bot.context_vault import settings as cv

    project = Path(envfile.PROJECT_ROOT)
    home = getattr(envfile, "ABP_HOME_ACTIVE", None)
    data = Path(home) if home else project / "data"
    private = os.environ.get("ABP_PRIVATE_DIR", "").strip()
    tiers = []
    for i, tier in enumerate(cv.load()["tiers"]):
        if tier.get("kind") == "dir":
            folder = Path(tier["path"]) if tier.get("path") else cv.default_store_dir()
            tiers.append((f"{i}-{tier['name']}", folder))
    return data, project, Path(private) if private else None, tiers


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.state_backup", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("run", "stage every store and back it up"), ("drill", "restore the newest backup and verify it")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", type=Path, required=True, help="the backup repository folder (another drive, not C:)")
        p.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV, help="environment variable holding the password")
    sub.add_parser("status", help="when the last backup and drill ran")
    args = parser.parse_args(argv)
    data, project, private, tiers = _defaults()
    try:
        if args.cmd == "status":
            result = status(data)
        else:
            if args.cmd == "drill" and not (args.repo / "config.json").exists():
                raise StateBackupError(f"{args.repo} is not a backup repository yet (run a state backup first)")
            password = os.environ.get(args.password_env) or getpass.getpass("Backup repository password: ")
            if args.cmd == "run":
                result = run(repo=args.repo, password=password, data=data, project_root=project, private_dir=private,
                             tier_dirs=tiers, log=lambda m: print(m, file=sys.stderr))
            else:
                result = drill(repo=args.repo, password=password, data=data, log=lambda m: print(m, file=sys.stderr))
    except StateBackupError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
