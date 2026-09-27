"""Allowed users, pairing codes, API keys, devices, push tokens, APK pushes and mesh tokens.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from typing import Any, Optional

from bot import db as _db


def add_allowed_user(telegram_id: int, name: str = "") -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT OR REPLACE INTO allowed_users (telegram_id, name, added_at) VALUES (?, ?, ?)",
            (telegram_id, name, _db._now()),
        )
        conn.commit()


def remove_allowed_user(telegram_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM allowed_users WHERE telegram_id=?", (telegram_id,))
        conn.commit()


def list_allowed_users() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM allowed_users ORDER BY added_at").fetchall()


def create_pairing_code(
    instance_id: int, code: str, user_id: str, user_name: str, chat_id: str, expires_at: str
) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO pairing_codes (instance_id, code, user_id, user_name, chat_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (instance_id, code, str(user_id), user_name or "", str(chat_id), _db._now(), expires_at),
        )
        conn.commit()
        return cur.lastrowid


def get_pairing_code(code: str) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM pairing_codes WHERE code=?", (code,)).fetchone()


def get_pairing_code_by_id(pairing_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM pairing_codes WHERE id=?", (pairing_id,)).fetchone()


def count_recent_pairing_requests(instance_id: int, user_id: str, since_iso: str) -> int:
    conn = _db.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pairing_codes WHERE instance_id=? AND user_id=? AND created_at>?",
        (instance_id, str(user_id), since_iso),
    ).fetchone()
    return row["n"] if row else 0


def count_pending_pairing_codes(instance_id: int, user_id: Optional[str] = None) -> int:
    conn = _db.get_conn()
    clauses = ["instance_id=?", "approved_at IS NULL", "denied_at IS NULL", "expires_at>?"]
    params: list[Any] = [instance_id, _db._now()]
    if user_id is not None:
        clauses.append("user_id=?")
        params.append(str(user_id))
    row = conn.execute(f"SELECT COUNT(*) AS n FROM pairing_codes WHERE {' AND '.join(clauses)}", params).fetchone()
    return row["n"] if row else 0


def list_pending_pairing_codes(instance_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if instance_id is not None:
        return conn.execute(
            "SELECT * FROM pairing_codes WHERE instance_id=? AND approved_at IS NULL AND denied_at IS NULL "
            "AND expires_at>? ORDER BY created_at DESC",
            (instance_id, _db._now()),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM pairing_codes WHERE approved_at IS NULL AND denied_at IS NULL AND expires_at>? "
        "ORDER BY created_at DESC",
        (_db._now(),),
    ).fetchall()


def approve_pairing_code(pairing_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE pairing_codes SET approved_at=? WHERE id=?", (_db._now(), pairing_id))
        conn.commit()


def deny_pairing_code(pairing_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE pairing_codes SET denied_at=? WHERE id=?", (_db._now(), pairing_id))
        conn.commit()


def create_api_key(label: str, kind: str = "device", permission_tier: str = "none") -> tuple[int, str]:
    plaintext = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO api_keys (label, key_hash, created_at, kind, permission_tier) VALUES (?, ?, ?, ?, ?)",
            (label, key_hash, _db._now(), kind, permission_tier),
        )
        conn.commit()
        return cur.lastrowid, plaintext


def list_api_keys(kind: Optional[str] = None) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if kind is not None:
        return conn.execute(
            "SELECT id, label, created_at, last_used_at, revoked_at, kind, permission_tier FROM api_keys WHERE kind=? ORDER BY created_at DESC",
            (kind,),
        ).fetchall()
    return conn.execute("SELECT id, label, created_at, last_used_at, revoked_at, kind, permission_tier FROM api_keys ORDER BY created_at DESC").fetchall()


def set_api_key_tier(key_id: int, tier: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("UPDATE api_keys SET permission_tier=? WHERE id=?", (tier, key_id))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"api key {key_id} not found")


def update_api_key_label(key_id: int, label: str) -> None:
    """Renames a paired device's label — the dashboard's Paired Devices
    "Save Devices List Data" action, so a device auto-paired under a generic
    name (or a stale manually-typed one) can be corrected in place instead
    of revoking and re-pairing just to fix a name."""
    label = (label or "").strip()
    if not label:
        raise ValueError("label can't be empty")
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("UPDATE api_keys SET label=? WHERE id=?", (label, key_id))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"api key {key_id} not found")


def revoke_api_key(key_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (_db._now(), key_id))
        conn.execute("DELETE FROM push_tokens WHERE api_key_id=?", (key_id,))
        conn.execute("DELETE FROM device_presence WHERE api_key_id=?", (key_id,))
        conn.commit()


def get_api_key(key_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT id, label, created_at, last_used_at, revoked_at, kind, permission_tier FROM api_keys WHERE id=?", (key_id,)
    ).fetchone()


def purge_revoked_keys() -> int:
    """Permanently deletes every already-revoked api_keys row — the
    dashboard's "Clear Revoked Devices" action. Revoking already tears down
    a device's presence/push rows immediately (see revoke_api_key above);
    this just removes the now-inert row itself so the Paired Devices list
    doesn't accumulate dead entries forever. Returns the number removed."""
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("DELETE FROM api_keys WHERE revoked_at IS NOT NULL")
        conn.commit()
        return cur.rowcount


def list_devices() -> list[sqlite3.Row]:
    """Paired, unrevoked devices with their live presence, if any — used by
    /api/devices and the WebSocket broadcaster. "Online" is left for the
    caller to compute from last_seen, so both can share the exact same
    freshness window without this layer hardcoding one."""
    conn = _db.get_conn()
    return conn.execute(
        "SELECT ak.id, ak.label, ak.created_at, ak.last_used_at, ak.permission_tier, "
        "dp.platform, dp.app_version, dp.device_model, dp.os_version, dp.last_seen "
        "FROM api_keys ak LEFT JOIN device_presence dp ON dp.api_key_id = ak.id "
        "WHERE ak.revoked_at IS NULL AND ak.kind='device' ORDER BY ak.created_at DESC"
    ).fetchall()


def upsert_push_token(api_key_id: int, fcm_token: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO push_tokens (api_key_id, fcm_token, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(fcm_token) DO UPDATE SET api_key_id=excluded.api_key_id, updated_at=excluded.updated_at",
            (api_key_id, fcm_token, _db._now()),
        )
        conn.commit()


def list_push_tokens() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT pt.* FROM push_tokens pt JOIN api_keys ak ON ak.id = pt.api_key_id WHERE ak.revoked_at IS NULL"
    ).fetchall()


def create_apk_push(
    api_key_id: int,
    apk_path: str,
    version_label: Optional[str] = None,
    origin_api_key_id: Optional[int] = None,
    mesh_token: Optional[str] = None,
) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO apk_pushes (api_key_id, apk_path, version_label, created_at, origin_api_key_id, mesh_token) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (api_key_id, apk_path, version_label, _db._now(), origin_api_key_id, mesh_token),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_apk_push(api_key_id: int) -> Optional[sqlite3.Row]:
    """The newest not-yet-downloaded push for this device, or None. Only
    the newest matters — an older undownloaded push for the same device is
    superseded, not queued behind it."""
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM apk_pushes WHERE api_key_id=? AND downloaded_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (api_key_id,),
    ).fetchone()


def get_apk_push(push_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM apk_pushes WHERE id=?", (push_id,)).fetchone()


def mark_apk_push_downloaded(push_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE apk_pushes SET downloaded_at=? WHERE id=?", (_db._now(), push_id))
        conn.commit()


def get_device_presence(api_key_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM device_presence WHERE api_key_id=?", (api_key_id,)).fetchone()


def redeem_mesh_token(push_id: int, token: str) -> bool:
    """Single-use check for a mesh transfer: the token must match the one
    minted for this exact push and not have been redeemed before. Called by
    the *origin* device's own mesh listener (via the server, since that's
    the only party who knows what token it handed out) before it starts
    streaming its APK to whoever presents the token — without this, any
    device that guessed or replayed a push_id could pull another device's
    installed APK indefinitely."""
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute(
            "SELECT mesh_token, mesh_token_used_at FROM apk_pushes WHERE id=?", (push_id,)
        ).fetchone()
        if row is None or not row["mesh_token"] or row["mesh_token"] != token or row["mesh_token_used_at"]:
            return False
        now = _db._now()
        # Redemption *is* the transfer starting — there's no separate
        # "download complete" signal in pure P2P mode (this server never
        # sees the bytes), so downloaded_at is stamped here too, same as
        # mark_apk_push_downloaded() does for the server-relay path.
        conn.execute(
            "UPDATE apk_pushes SET mesh_token_used_at=?, downloaded_at=? WHERE id=?",
            (now, now, push_id),
        )
        conn.commit()
        return True


def device_label(device_id: int) -> str:
    if device_id == _db.SERVER_CHAT_DESKTOP_DEVICE_ID:
        return "Desktop"
    if device_id == _db.SERVER_CHAT_BOT_DEVICE_ID:
        return "AgenticBotPlatform"
    row = _db.get_conn().execute(
        "SELECT label, revoked_at FROM api_keys WHERE id=?", (device_id,)
    ).fetchone()
    if row is None:
        return f"device {device_id}"
    return row["label"] + (" (revoked)" if row["revoked_at"] else "")


def verify_api_key(
    plaintext: str,
    platform: Optional[str] = None,
    app_version: Optional[str] = None,
    device_model: Optional[str] = None,
    os_version: Optional[str] = None,
    local_ip: Optional[str] = None,
    mesh_port: Optional[int] = None,
) -> Optional[int]:
    """Also upserts device_presence on every successful call — piggybacking
    presence tracking on the auth check every mobile request already makes,
    rather than a separate heartbeat endpoint. All device fields are
    optional (COALESCE keeps whatever was last known if this particular
    caller didn't send them) since not every route bothers threading device
    headers through. `device_model`/`os_version` are the real hardware model
    and OS release (e.g. "Pixel 8 Pro" / "Android 14") — distinct from the
    user-typed pairing label, so devices are identifiable even when someone
    left the label as something generic. `local_ip` is the caller's address
    as this server itself observed it (request.client.host) — only
    meaningful when the caller is actually on the same LAN as the server,
    which is exactly the case the mesh transfer feature needs: it's used to
    let a *different* device dial this one directly instead of relaying
    through the server. `mesh_port` is which local TCP port this device's
    own mesh listener (if any) is currently bound to, self-reported since
    the server has no other way to know it."""
    if not plaintext:
        return None
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (key_hash,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (_db._now(), row["id"]))
        conn.execute(
            "INSERT INTO device_presence "
            "(api_key_id, platform, app_version, device_model, os_version, local_ip, mesh_port, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(api_key_id) DO UPDATE SET "
            "platform=COALESCE(excluded.platform, device_presence.platform), "
            "app_version=COALESCE(excluded.app_version, device_presence.app_version), "
            "device_model=COALESCE(excluded.device_model, device_presence.device_model), "
            "os_version=COALESCE(excluded.os_version, device_presence.os_version), "
            "local_ip=COALESCE(excluded.local_ip, device_presence.local_ip), "
            "mesh_port=excluded.mesh_port, "
            "last_seen=excluded.last_seen",
            (row["id"], platform, app_version, device_model, os_version, local_ip, mesh_port, _db._now()),
        )
        conn.commit()
        return row["id"]


def api_key_kind(plaintext: str) -> Optional[str]:
    """Read-only lookup of an already-verified key's `kind` (e.g. "device"
    vs "peer_server") — separate from verify_api_key() so callers that
    don't want its device_presence side effects (like re-checking what kind
    of caller this is, after auth already succeeded once) can ask without
    re-touching presence data."""
    if not plaintext:
        return None
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    conn = _db.get_conn()
    row = conn.execute(
        "SELECT kind FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (key_hash,)
    ).fetchone()
    return row["kind"] if row else None
