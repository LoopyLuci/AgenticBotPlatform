"""DB-backed bot instances — the unit of "one bot" from here on.

Where bot/envfile.py is the safe-storage model for the app's own core
secrets (ANTHROPIC_API_KEY, DASHBOARD_TOKEN — a handful of fixed named
settings), bot instances are a dynamic, arbitrarily-large collection of
independently-configured bots (any number of Telegram/Discord/Slack
credentials, each pointed at its own backend), which is a DB-table shape,
not a flat-file shape. This module is the DB equivalent of envfile.py:
every mutation is preceded by a full-table JSON snapshot into
data/bot_instances_backups/, restorable the same way an .env backup is,
so credentials get the same safety net without forcing them into .env's
one-secret-per-line format.

Platform-specific `credentials` JSON shapes:
  telegram: {"bot_token": "..."}
  discord:  {"bot_token": "..."}
  slack:    {"bot_token": "...", "app_token": "..."}
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot import db, envfile
from bot.personas import DEFAULT_PERSONA
from bot.validators import PLATFORM_TOKEN_VALIDATORS, validate_user_ids

PLATFORMS = ("telegram", "discord", "slack")

BACKUP_DIR = envfile.PROJECT_ROOT / "data" / "bot_instances_backups"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ValidationError(ValueError):
    pass


def _validate_credentials(platform: str, credentials: dict[str, Any]) -> None:
    if platform not in PLATFORMS:
        raise ValidationError(f"unknown platform {platform!r}, expected one of {PLATFORMS}")
    validators = PLATFORM_TOKEN_VALIDATORS[platform]
    for field, validator in validators.items():
        value = (credentials.get(field) or "").strip()
        if not value:
            raise ValidationError(f"{platform} instance requires {field!r}")
        ok, msg = validator(value)
        if not ok:
            raise ValidationError(f"{field}: {msg}")


def _validate_allowed_ids(platform: str, allowed_user_ids: list[Any]) -> None:
    if not allowed_user_ids:
        raise ValidationError("at least one allowed user id is required")
    # Reuse the numeric-id validator for telegram/discord; slack ids are
    # strings (U…/W…-prefixed) so only require non-empty entries there.
    if platform in ("telegram", "discord"):
        ok, msg = validate_user_ids(",".join(str(i) for i in allowed_user_ids))
        if not ok:
            raise ValidationError(msg)


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["credentials"] = json.loads(d["credentials"])
    d["allowed_user_ids"] = json.loads(d["allowed_user_ids"])
    d["action_overrides"] = json.loads(d["action_overrides"])
    d["can_target"] = json.loads(d["can_target"])
    d["enabled"] = bool(d["enabled"])
    return d


def get_instance(instance_id: int) -> Optional[dict[str, Any]]:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM bot_instances WHERE id=?", (instance_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_instances(platform: Optional[str] = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    conn = db.get_conn()
    clauses = []
    params: list[Any] = []
    if platform is not None:
        clauses.append("platform=?")
        params.append(platform)
    if enabled_only:
        clauses.append("enabled=1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM bot_instances {where} ORDER BY id", params).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_instance(
    name: str,
    platform: str,
    backend: str,
    credentials: dict[str, Any],
    allowed_user_ids: list[Any],
    action_overrides: Optional[dict[str, Any]] = None,
    can_target: Optional[list[int]] = None,
    enabled: bool = True,
    model: Optional[str] = None,
    custom_instructions: Optional[str] = None,
    persona: Optional[str] = None,
    actor: str = "dashboard",
) -> int:
    name = (name or "").strip()
    if not name:
        raise ValidationError("name is required")
    _validate_credentials(platform, credentials)
    _validate_allowed_ids(platform, allowed_user_ids)

    conn = db.get_conn()
    with db._lock:
        try:
            cur = conn.execute(
                "INSERT INTO bot_instances "
                "(name, platform, backend, enabled, credentials, allowed_user_ids, action_overrides, can_target, model, custom_instructions, persona, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    platform,
                    backend,
                    1 if enabled else 0,
                    json.dumps(credentials),
                    json.dumps(allowed_user_ids),
                    json.dumps(action_overrides or {}),
                    json.dumps(can_target or []),
                    model or None,
                    (custom_instructions or "").strip() or None,
                    (persona or "").strip() or DEFAULT_PERSONA,
                    _now(),
                    _now(),
                ),
            )
            conn.commit()
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ValidationError(f"a bot instance named {name!r} already exists") from exc
            raise
        instance_id = cur.lastrowid
    db.log_audit(actor=actor, action="bot_instance_create", detail=f"created {name!r} ({platform}/{backend})")
    backup_instances(reason=f"after create: {name}")
    return instance_id


def update_instance(instance_id: int, actor: str = "dashboard", **fields: Any) -> None:
    current = get_instance(instance_id)
    if current is None:
        raise ValidationError(f"bot instance {instance_id} not found")

    platform = fields.get("platform", current["platform"])
    credentials = fields.get("credentials", current["credentials"])
    allowed_user_ids = fields.get("allowed_user_ids", current["allowed_user_ids"])
    if "credentials" in fields or "platform" in fields:
        _validate_credentials(platform, credentials)
    if "allowed_user_ids" in fields or "platform" in fields:
        _validate_allowed_ids(platform, allowed_user_ids)

    columns: list[str] = []
    params: list[Any] = []
    for key in ("name", "platform", "backend", "enabled", "model", "custom_instructions", "persona"):
        if key in fields:
            columns.append(f"{key}=?")
            params.append(1 if key == "enabled" and fields[key] else (0 if key == "enabled" else fields[key]))
    for key in ("credentials", "allowed_user_ids", "action_overrides", "can_target"):
        if key in fields:
            columns.append(f"{key}=?")
            params.append(json.dumps(fields[key]))
    if not columns:
        return
    columns.append("updated_at=?")
    params.append(_now())
    params.append(instance_id)

    conn = db.get_conn()
    with db._lock:
        try:
            conn.execute(f"UPDATE bot_instances SET {', '.join(columns)} WHERE id=?", params)
            conn.commit()
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ValidationError(f"a bot instance named {fields.get('name')!r} already exists") from exc
            raise
    db.log_audit(actor=actor, action="bot_instance_update", detail=f"updated instance {instance_id} ({current['name']!r})")
    backup_instances(reason=f"after update: {current['name']}")


def delete_instance(instance_id: int, actor: str = "dashboard") -> None:
    current = get_instance(instance_id)
    if current is None:
        raise ValidationError(f"bot instance {instance_id} not found")
    conn = db.get_conn()
    with db._lock:
        conn.execute("DELETE FROM bot_instances WHERE id=?", (instance_id,))
        conn.commit()
    db.log_audit(actor=actor, action="bot_instance_delete", detail=f"deleted {current['name']!r} ({current['platform']})")
    backup_instances(reason=f"after delete: {current['name']}")


def enable_instance(instance_id: int, actor: str = "dashboard") -> None:
    update_instance(instance_id, enabled=True, actor=actor)


def disable_instance(instance_id: int, actor: str = "dashboard") -> None:
    update_instance(instance_id, enabled=False, actor=actor)


def record_start(instance_id: int) -> None:
    conn = db.get_conn()
    with db._lock:
        conn.execute(
            "UPDATE bot_instances SET last_started_at=?, last_error=NULL WHERE id=?", (_now(), instance_id)
        )
        conn.commit()


def record_error(instance_id: int, error: str) -> None:
    conn = db.get_conn()
    with db._lock:
        conn.execute("UPDATE bot_instances SET last_error=? WHERE id=?", (error, instance_id))
        conn.commit()


def set_desktop_session_key(instance_id: int, key: Optional[str], actor: str = "system") -> None:
    """Links (or clears, if key is None) this instance to one specific
    already-created chat/session inside the real Claude Desktop / Hermes app —
    see bot/router.py's create_session() and bot/backends/ui_backend.py /
    hermes_gateway_backend.py. Deliberately bypasses update_instance()'s
    validation/backup machinery — this is routing plumbing set by the
    backend layer itself, not a user-editable instance field."""
    conn = db.get_conn()
    with db._lock:
        conn.execute("UPDATE bot_instances SET desktop_session_key=?, updated_at=? WHERE id=?", (key, _now(), instance_id))
        conn.commit()
    db.log_audit(actor=actor, action="bot_instance_session_link", detail=f"instance {instance_id} desktop_session_key -> {key!r}")


# ------------------------------------------------------------- backups ----
# Mirrors bot/envfile.py's backup_current()/list_backups()/restore_backup()
# shape exactly, just snapshotting the whole bot_instances table as JSON
# instead of a single .env file.

def backup_instances(reason: str = "") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"instances-{stamp}.json"
    n = 1
    while dest.exists():  # collision guard for saves within the same second
        dest = BACKUP_DIR / f"instances-{stamp}-{n}.json"
        n += 1
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM bot_instances ORDER BY id").fetchall()
    snapshot = {
        "reason": reason,
        "taken_at": _now(),
        "instances": [dict(r) for r in rows],
    }
    dest.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return dest


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("instances-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = p.stat()
        out.append(
            {
                "name": p.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            }
        )
    return out


def _safe_backup_path(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in (".", "..") or not name.startswith("instances-"):
        raise ValueError(f"invalid backup name: {name!r}")
    candidate = BACKUP_DIR / name
    if candidate.resolve().parent != BACKUP_DIR.resolve():
        raise ValueError(f"invalid backup name: {name!r}")
    return candidate


def restore_backup(name: str, actor: str = "dashboard") -> None:
    """Replace the entire bot_instances table with a snapshot's contents —
    snapshotting the about-to-be-overwritten current version first, so a
    restore is itself always undoable, same as envfile.restore_backup()."""
    candidate = _safe_backup_path(name)
    if not candidate.exists():
        raise FileNotFoundError(f"backup {name!r} not found")
    snapshot = json.loads(candidate.read_text(encoding="utf-8"))

    backup_instances(reason=f"pre-restore snapshot (restoring {name})")

    conn = db.get_conn()
    with db._lock:
        conn.execute("DELETE FROM bot_instances")
        for row in snapshot["instances"]:
            conn.execute(
                "INSERT INTO bot_instances "
                "(id, name, platform, backend, enabled, credentials, allowed_user_ids, action_overrides, can_target, model, "
                "persona, created_at, updated_at, last_started_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["name"], row["platform"], row["backend"], row["enabled"],
                    row["credentials"], row["allowed_user_ids"], row["action_overrides"], row.get("can_target", "[]"),
                    row.get("model"),
                    row.get("persona") or "assistant",
                    row["created_at"], row["updated_at"], row.get("last_started_at"), row.get("last_error"),
                ),
            )
        conn.commit()
    db.log_audit(actor=actor, action="bot_instances_restore", detail=f"restored {name}")


# --------------------------------------------------------- legacy migration
# Protects the one bot that was already live before this feature existed:
# whatever's currently in .env as TELEGRAM_BOT_TOKEN/ALLOWED_TELEGRAM_USER_IDS
# becomes bot instance #1, and existing jobs/messages rows get backfilled to
# point at it, so nothing about the user's history or running bot is lost.

def migrate_legacy_env_instance() -> Optional[int]:
    """Idempotent — a non-empty bot_instances table means this has already
    run (the only realistic way it's non-empty at upgrade time), so it's a
    no-op on every later boot."""
    if list_instances():
        return None

    from bot import setup_wizard

    values = setup_wizard.current_values()
    token = (values.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return None
    allowed_raw = (values.get("ALLOWED_TELEGRAM_USER_IDS") or "").strip()
    allowed_ids = {int(p.strip()) for p in allowed_raw.split(",") if p.strip().isdigit()}
    # Also pull in anyone added at runtime via the old dashboard's Security
    # card (db.allowed_users) — a DB-backed additive allowlist that existed
    # before per-instance allowlists did, so someone approved that way isn't
    # silently dropped once this migration runs.
    allowed_ids |= {row["telegram_id"] for row in db.list_allowed_users()}
    if not allowed_ids:
        return None
    allowed_ids = sorted(allowed_ids)

    from bot.config import config

    default_backend = config.current.get("default_backend", "cli")

    instance_id = create_instance(
        name="telegram (migrated)",
        platform="telegram",
        backend=default_backend,
        credentials={"bot_token": token},
        allowed_user_ids=allowed_ids,
        actor="system",
    )

    conn = db.get_conn()
    with db._lock:
        conn.execute("UPDATE jobs SET instance_id=? WHERE instance_id IS NULL", (instance_id,))
        conn.execute(
            "UPDATE messages SET instance_id=? WHERE instance_id IS NULL AND platform='telegram'",
            (instance_id,),
        )
        conn.commit()

    db.log_audit(
        actor="system",
        action="migrate_legacy_bot_instance",
        detail=f"created instance {instance_id} from .env TELEGRAM_BOT_TOKEN, backfilled jobs/messages",
    )
    backup_instances(reason="post-migration snapshot")
    return instance_id
