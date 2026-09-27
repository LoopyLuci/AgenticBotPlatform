"""Linked peer servers and their pairing tokens.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot import db as _db


def create_peer_server(name: str, base_url: str, outbound_api_key: str, inbound_api_key_id: int) -> int:
    """Upserts by base_url when it's non-empty: re-linking the same address
    (the admin re-runs the link form after a restart, a DB reset on one
    side, or just clicking it twice) replaces the stale row in place —
    revoking the credential it's replacing — instead of accumulating
    duplicate rows with one dangling, never-used api_keys credential each
    time. An empty base_url (a peer that can't call us back) can't be
    deduped this way and always inserts a fresh row."""
    conn = _db.get_conn()
    with _db._lock:
        existing = None
        if base_url:
            existing = conn.execute(
                "SELECT id, inbound_api_key_id FROM peer_servers WHERE base_url=?", (base_url,)
            ).fetchone()
        if existing is not None:
            if existing["inbound_api_key_id"] != inbound_api_key_id:
                conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (_db._now(), existing["inbound_api_key_id"]))
            conn.execute(
                "UPDATE peer_servers SET name=?, outbound_api_key=?, inbound_api_key_id=?, "
                "linked_at=?, last_seen_at=NULL, last_error=NULL WHERE id=?",
                (name, outbound_api_key, inbound_api_key_id, _db._now(), existing["id"]),
            )
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO peer_servers (name, base_url, outbound_api_key, inbound_api_key_id, linked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, base_url, outbound_api_key, inbound_api_key_id, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_peer_servers() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM peer_servers ORDER BY linked_at DESC").fetchall()


def get_peer_server(peer_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM peer_servers WHERE id=?", (peer_id,)).fetchone()


def mark_peer_server_ok(peer_id: int) -> None:
    """Called after every successful proxied call to a peer (see
    bot/peers.py) so the dashboard can show whether a linked server is
    actually reachable right now without a separate polling loop."""
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE peer_servers SET last_seen_at=?, last_error=NULL WHERE id=?", (_db._now(), peer_id))
        conn.commit()


def mark_peer_server_error(peer_id: int, error: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE peer_servers SET last_error=? WHERE id=?", (error, peer_id))
        conn.commit()


def delete_peer_server(peer_id: int) -> Optional[sqlite3.Row]:
    """Unlinks a peer: revokes the credential it used to call us (so it
    can't reach us again with the old key) and removes our record of it.
    Returns the deleted row (the caller may still want its outbound_api_key
    to attempt a courtesy unlink call to the peer itself) or None if it
    didn't exist."""
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute("SELECT * FROM peer_servers WHERE id=?", (peer_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (_db._now(), row["inbound_api_key_id"]))
        conn.execute("DELETE FROM peer_servers WHERE id=?", (peer_id,))
        conn.commit()
        return row


def create_server_pairing_token(ttl_s: int = 600) -> tuple[str, str]:
    """Mints a fresh pairing token, invalidating whatever was still pending
    — only one is ever meant to be outstanding at a time, so an admin who
    generates a new one doesn't have to remember to separately revoke the
    old one too. Returns (plaintext, expires_at)."""
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    now = _db._now()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat(timespec="seconds")
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM server_pairing_tokens WHERE used_at IS NULL")
        conn.execute(
            "INSERT INTO server_pairing_tokens (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
            (token_hash, now, expires_at),
        )
        conn.commit()
    return plaintext, expires_at


def consume_server_pairing_token(plaintext: str) -> bool:
    """Validates a pairing token and marks it used in one atomic step —
    it's either accepted exactly once or not at all, closing the window a
    separate check-then-use would leave for a replay. Also opportunistically
    clears out old used/expired rows so the table never grows unbounded."""
    if not plaintext:
        return False
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    conn = _db.get_conn()
    with _db._lock:
        row = conn.execute(
            "SELECT id, expires_at FROM server_pairing_tokens WHERE token_hash=? AND used_at IS NULL",
            (token_hash,),
        ).fetchone()
        if row is None:
            return False
        if datetime.fromisoformat(row["expires_at"]) < now_dt:
            return False
        cur = conn.execute(
            "UPDATE server_pairing_tokens SET used_at=? WHERE id=? AND used_at IS NULL", (now, row["id"])
        )
        conn.execute(
            "DELETE FROM server_pairing_tokens WHERE used_at IS NOT NULL OR expires_at < ?", (now,)
        )
        conn.commit()
        return cur.rowcount == 1
