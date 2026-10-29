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
    python -m bot.state_backup plain --dest <folder>     (no password: an unencrypted copy, see plain_copy)
    python -m bot.state_backup prune --dest <folder> [--dry-run]   (the plain copies' keep rules, see prune_plain_copies)

The repository password is never stored. Without the environment variable it is asked for at the terminal.

The plain copy is for a drive the owner keeps private. It never carries a live credential: the configuration files are
left out, credential columns are blanked in every copied database, key-shaped text is redacted, and the copy is checked
before it is kept (a copy that fails the check is removed).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import re
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
# How many plain copies the owner keeps: the newest per day, ISO week and month (grandfather-father-son), see prune_plain_copies.
KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 7, 4, 6
KEEP_SETTINGS = (("keep_daily", KEEP_DAILY), ("keep_weekly", KEEP_WEEKLY), ("keep_monthly", KEEP_MONTHLY))
MAX_KEEP = 999
PLAIN_NAME = re.compile(r"plain-(\d{8}T\d{6}Z)(?:-[A-Za-z0-9_.-]+)?")      # a plain copy's folder name: UTC time, optional suffix
logger = logging.getLogger("bot.state_backup")

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


def current_status() -> dict:
    """The last run and the last drill, read from the default data folder (the Storage page's Recovery tab)."""
    return status(_defaults()[0])


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


# ---- plain copy: unencrypted, for a drive the owner keeps private, with no live credential in it -----------------------
# The owner chose a copy without a password. It still must not carry what would let someone into a service: the
# configuration files (they hold API keys) are left out, the credential columns are blanked in each copy, key-shaped
# text is redacted, and a leak check runs before the copy is kept.
SECRET_CONFIG_PREFIXES = ("config/", "private/config/")
CREDENTIAL_COLUMNS = (
    ("bot_instances", "credentials"), ("push_tokens", "fcm_token"), ("apk_pushes", "mesh_token"),
    ("peer_servers", "outbound_api_key"), ("external_mcp_servers", "auth_token"),
    ("external_mcp_servers", "oauth_client_info_json"), ("external_mcp_servers", "oauth_tokens_json"),
)
KEY_SHAPED = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"ghp_[A-Za-z0-9]{30,}"), re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)
KEY_SHAPED_BYTES = re.compile(rb"sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
                              rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
MIN_SECRET_LEN = 8        # shorter values are too common to be a meaningful match in a binary file


def _tables(conn: sqlite3.Connection) -> dict[str, set[str]]:
    return {t: {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _live_credentials(sources: list[Path]) -> set[bytes]:
    """The non-empty credential values in the live databases, read-only. Kept in memory for the leak check only."""
    values: set[bytes] = set()
    for src in sources:
        conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            tables = _tables(conn)
            for table, col in CREDENTIAL_COLUMNS:
                if col not in tables.get(table, set()):
                    continue
                for (value,) in conn.execute(f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL AND "{col}" <> \'\''):
                    values.add(value.encode("utf-8") if isinstance(value, str) else bytes(value))
        finally:
            conn.close()
    return values


def _scrub_copy(db: Path) -> dict[str, int]:
    """Blank the credential columns of one copy, redact key-shaped text in its text columns, then rebuild it so no old
    value survives in free pages (secure_delete, then VACUUM). Returns how many values changed, per column."""
    changed: dict[str, int] = {}
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        tables = _tables(conn)
        for table, col in CREDENTIAL_COLUMNS:
            if col not in tables.get(table, set()):
                continue
            n = conn.execute(f"UPDATE \"{table}\" SET \"{col}\" = '' WHERE \"{col}\" IS NOT NULL AND \"{col}\" <> ''").rowcount
            if n:
                changed[f"{table}.{col}"] = n
        for table, cols in tables.items():
            for col in cols:
                try:
                    rows = conn.execute(f'SELECT rowid, "{col}" FROM "{table}" WHERE typeof("{col}") = \'text\'').fetchall()
                except sqlite3.DatabaseError:
                    continue          # a table without rowids: nothing to rewrite row by row
                n = 0
                for rowid, value in rows:
                    new = value
                    for pattern in KEY_SHAPED:
                        new = pattern.sub("[redacted]", new)
                    if new != value:
                        conn.execute(f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?', (new, rowid))
                        n += 1
                if n:
                    changed[f"{table}.{col} (key-shaped text)"] = n
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return changed


def _refresh_manifest(dest: Path) -> None:
    """Record the copies' bytes as they are now (the scrub changed the databases)."""
    path = dest / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["kind"] == "sqlite":
            target = dest / entry["path"]
            entry["sha256"] = _sha256(target)
            entry["size"] = target.stat().st_size
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _mark_verified(dest: Path) -> None:
    """Record in the manifest that the copy passed its check. prune_plain_copies keeps the newest copy with this record."""
    path = dest / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["verified"] = {"at": _now(), "ok": True}
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _leak_check(dest: Path, values: set[bytes]) -> dict:
    """Which copied files still hold a live credential value or a key-shaped string. Names only, never the text."""
    checked = [v for v in values if len(v) >= MIN_SECRET_LEN]
    with_value, with_shape, files = [], [], 0
    for f in sorted(dest.rglob("*")):
        if not f.is_file():
            continue
        files += 1
        data = f.read_bytes()
        rel = f.relative_to(dest).as_posix()
        if any(v in data for v in checked):
            with_value.append(rel)
        if KEY_SHAPED_BYTES.search(data):
            with_shape.append(rel)
    return {"files": files, "credential_values_checked": len(checked),
            "files_with_a_credential": with_value, "files_with_key_shaped_text": with_shape}


def plain_copy(*, dest_root: Path, data: Path, project_root: Path, private_dir: Optional[Path],
               tier_dirs: list[tuple[str, Path]], fileserver_dir: Optional[Path] = None,
               log: Callable[[str], None] = lambda m: None) -> dict:
    """An unencrypted copy of the state, written to a new folder under dest_root and checked before it is kept.

    Every planned store is copied, and so is every unlisted database (as unlisted/<path>). Configuration files (they hold
    API keys) are not copied; credential columns are blanked in each copied database; key-shaped text is redacted. A
    copy that still holds a live credential value or key-shaped text fails the check, is removed, and is refused.
    The live stores are read, never written."""
    check_destination(dest_root)
    plan = collect(data=data, project_root=project_root, private_dir=private_dir, tier_dirs=tier_dirs,
                   fileserver_dir=fileserver_dir)
    kept = []
    for rel, src in plan.files:
        if rel.startswith(SECRET_CONFIG_PREFIXES):
            plan.excluded.append({"label": rel, "path": rel, "reason": "secret-bearing config: never copied unencrypted"})
        else:
            kept.append((rel, src))
    plan.files = kept
    plan.sqlite += [(f"unlisted/{rel}", data / rel) for rel in plan.unlisted]
    values = _live_credentials([src for _, src in plan.sqlite])
    dest = dest_root / f"plain-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    log(f"plain copy: {len(plan.files)} file(s), {len(plan.sqlite)} database(s) -> {dest}")
    stage(plan, dest)
    try:
        changed = {rel: _scrub_copy(dest / rel) for rel, _ in plan.sqlite}
        _refresh_manifest(dest)
        check = _leak_check(dest, values)
        report = verify(dest)
        if check["files_with_a_credential"] or check["files_with_key_shaped_text"] or not report["ok"]:
            raise StateBackupError(
                f"the plain copy failed its check and was removed: credential values in "
                f"{check['files_with_a_credential'] or 'none'}; key-shaped text in "
                f"{check['files_with_key_shaped_text'] or 'none'}; verify ok={report['ok']}")
        _mark_verified(dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    summary = {"copy": str(dest), "files": len(plan.files), "databases": len(plan.sqlite),
               "changed": {rel: counts for rel, counts in changed.items() if counts},
               "excluded": [e["label"] for e in plan.excluded], "unlisted_copied": len(plan.unlisted),
               "credential_values_checked": check["credential_values_checked"], "verify_ok": True, "at": _now()}
    _write_status(data, {"last_plain": summary})
    return summary


def check_destination(dest: Path) -> None:
    """A plain copy goes to a folder the owner chose: an absolute path, never on C:."""
    text = str(dest).strip()
    if not text or not dest.is_absolute():
        raise StateBackupError("the copy folder must be an absolute path (a drive other than C:)")
    if os.path.splitdrive(text)[0].upper() == "C:":
        raise StateBackupError("a plain copy is never written to C: (choose a folder on another drive)")


def run_plain(dest_root: Path, log: Callable[[str], None] = lambda m: None) -> dict:
    """plain_copy against this instance's own folders (the data folder, the project, the private overlay, the tiers)."""
    data, project, private, tiers = _defaults()
    return plain_copy(dest_root=dest_root, data=data, project_root=project, private_dir=private, tier_dirs=tiers, log=log)


def _schedule_path() -> Path:
    return _defaults()[0] / "state_backup" / "schedule.json"


def schedule() -> dict:
    """The daily plain copy: off until the owner turns it on, the folder it writes to, and how many copies to keep."""
    path = _schedule_path()
    stored: dict = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stored = {}
    keeps = {name: _stored_keep(stored, name, default) for name, default in KEEP_SETTINGS}
    return {"enabled": bool(stored.get("enabled", False)), "dest": str(stored.get("dest") or ""), **keeps}


def _check_keep(name: str, value) -> int:
    """A copy count from the owner: a whole number. At least one daily copy, so the newest copy is always kept."""
    minimum = 1 if name == "keep_daily" else 0
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= MAX_KEEP:
        raise StateBackupError(f"{name} must be a whole number from {minimum} to {MAX_KEEP}")
    return value


def _stored_keep(stored: dict, name: str, default: int) -> int:
    try:
        return _check_keep(name, stored.get(name, default))
    except StateBackupError:
        return default


def set_schedule(*, enabled: bool, dest: str, keep_daily: Optional[int] = None, keep_weekly: Optional[int] = None,
                 keep_monthly: Optional[int] = None) -> dict:
    """Save the schedule. A keep count left as None keeps the value already stored."""
    dest = (dest or "").strip()
    if dest:
        check_destination(Path(dest))
    if enabled and not dest:
        raise StateBackupError("choose the folder the daily copy goes to before turning it on")
    current = schedule()
    wanted = {"keep_daily": keep_daily, "keep_weekly": keep_weekly, "keep_monthly": keep_monthly}
    keeps = {name: _check_keep(name, current[name] if value is None else value) for name, value in wanted.items()}
    path = _schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": bool(enabled), "dest": dest, **keeps}, indent=2), encoding="utf-8")
    return schedule()


def run_scheduled() -> dict:
    """One scheduled pass: a plain copy into the chosen folder, when the owner has turned the schedule on."""
    sched = schedule()
    if not sched["enabled"]:
        raise StateBackupError("the daily plain copy is off")
    return run_plain(Path(sched["dest"]))


# ---- prune: the owner's keep rules for the plain copies (grandfather-father-son) ---------------------------------------
def _is_verified(folder: Path) -> bool:
    """True when the copy's manifest carries the record plain_copy writes after its check passed. No record: unverified."""
    try:
        record = json.loads((folder / "manifest.json").read_text(encoding="utf-8")).get("verified")
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    return isinstance(record, dict) and record.get("ok") is True


def _plain_copies(root: Path) -> list[tuple[datetime, Path]]:
    """The plain copies in root, newest first, by the UTC time in their names. Anything else in root is not a copy."""
    found = []
    if not root.is_dir():
        return found
    for path in root.iterdir():
        match = PLAIN_NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_dir():
            continue
        try:
            when = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        found.append((when, path))
    return sorted(found, key=lambda item: (item[0], item[1].name), reverse=True)


def _keep_newest_per(copies: list[tuple[datetime, Path]], bucket: Callable[[datetime], object], count: int, label: str,
                     reasons: dict[str, list[str]]) -> None:
    """Keep the newest copy of each of the `count` most recent buckets (days, ISO weeks, months) that hold a copy."""
    newest: dict = {}
    for when, path in copies:                       # newest first, so the first copy seen in a bucket is its newest
        newest.setdefault(bucket(when), path)
    for key in sorted(newest, reverse=True)[:count]:
        reasons.setdefault(newest[key].name, []).append(label)


def prune_plain_copies(root: Path, *, daily: int = KEEP_DAILY, weekly: int = KEEP_WEEKLY, monthly: int = KEEP_MONTHLY,
                       dry_run: bool = False) -> dict:
    """Delete the plain copies in root that no keep rule holds. Returns what is kept and what was removed.

    Kept: the newest copy of each of the last `daily` days, `weekly` ISO weeks and `monthly` months that hold a copy (a
    day, week or month with no copy does not count), and the newest copy that passed its check (a copy with no record of
    passing is unverified). A copy any rule keeps is kept. Only folders named like a plain copy are candidates; nothing
    else in root is touched. The keep set is computed before anything is deleted, and each removal is logged. dry_run
    returns the same lists and deletes nothing."""
    check_destination(root)
    daily = _check_keep("keep_daily", daily)
    weekly = _check_keep("keep_weekly", weekly)
    monthly = _check_keep("keep_monthly", monthly)
    copies = _plain_copies(root)
    reasons: dict[str, list[str]] = {}
    _keep_newest_per(copies, lambda when: when.date(), daily, "daily", reasons)
    _keep_newest_per(copies, lambda when: when.isocalendar()[:2], weekly, "weekly", reasons)
    _keep_newest_per(copies, lambda when: (when.year, when.month), monthly, "monthly", reasons)
    newest_verified = next((path for _, path in copies if _is_verified(path)), None)
    if newest_verified is not None:
        reasons.setdefault(newest_verified.name, []).append("newest verified")
    kept = [path.name for _, path in copies if path.name in reasons]
    doomed = [path for _, path in copies if path.name not in reasons]
    removed, failed = [], []
    for path in doomed:
        if dry_run:
            removed.append(path.name)
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            failed.append(path.name)
            logger.warning("could not remove plain state copy %s: %s", path, exc)
            continue
        removed.append(path.name)
        logger.info("removed plain state copy %s (no keep rule holds it)", path)
    return {"root": str(root), "dry_run": dry_run, "kept": kept, "removed": removed, "failed": failed,
            "reasons": reasons, "ok": not failed}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.state_backup", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("run", "stage every store and back it up"), ("drill", "restore the newest backup and verify it")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", type=Path, required=True, help="the backup repository folder (another drive, not C:)")
        p.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV, help="environment variable holding the password")
    sub.add_parser("status", help="when the last backup and drill ran")
    plain = sub.add_parser("plain", help="an unencrypted copy with no live credential in it, checked before it is kept")
    plain.add_argument("--dest", type=Path, required=True, help="the folder to put the copy in (another drive, not C:)")
    prune = sub.add_parser("prune", help="delete the plain copies no keep rule holds (the newest verified copy is kept)")
    prune.add_argument("--dest", type=Path, required=True, help="the folder the plain copies are in (another drive, not C:)")
    prune.add_argument("--dry-run", action="store_true", help="list what would be removed and delete nothing")
    args = parser.parse_args(argv)
    data, project, private, tiers = _defaults()
    try:
        if args.cmd == "status":
            result = status(data)
        elif args.cmd == "plain":
            result = plain_copy(dest_root=args.dest, data=data, project_root=project, private_dir=private,
                                tier_dirs=tiers, log=lambda m: print(m, file=sys.stderr))
        elif args.cmd == "prune":
            keeps = schedule()
            result = prune_plain_copies(args.dest, daily=keeps["keep_daily"], weekly=keeps["keep_weekly"],
                                        monthly=keeps["keep_monthly"], dry_run=args.dry_run)
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
