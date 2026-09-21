"""A store of every model provider that has ever been configured, so removing one is never final.

config/providers.yaml is the live registry: whatever is in it is what the bot routes to. Removing a provider used to be
a plain delete from that file with no undo - the address, the protocol, the key, all gone. This module keeps a copy of
each provider in ``<state>/data/provider_store.db``:

* saving a provider records it as *active*; removing one marks it *deleted* and keeps everything, so it can be restored
  in one step (`providers.restore_provider`, the dashboard's "Deleted providers" list, `POST /api/providers/store/<name>/restore`);
* an inline API key is kept **encrypted** (the vault's Fernet key, see bot/vault.py), never in plain text, and is
  never returned by the API - only whether one is stored;
* a provider that disappears from the file by any other route (a hand edit, a restored snapshot) is noticed and moved
  to *deleted* the next time the store is read, rather than silently forgotten;
* the per-model on/off choices live in the main database and are left alone by a removal, so a restored provider comes
  back with its models set as they were;
* forgetting one for good is a separate, explicit step (`purge`) and only works on a deleted provider.

The store is its own file, not part of bot.db, so restoring a database snapshot cannot swallow it.

    python -m bot.provider_store list
    python -m bot.provider_store recover --from <another ABP state folder>   # rebuild deleted providers from its history
"""
from __future__ import annotations

import ast
import logging
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.provider_store")

STORE_PATH: Path = PROJECT_ROOT / "data" / "provider_store.db"

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_store (
    name         TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    base_url     TEXT NOT NULL DEFAULT '',
    protocol     TEXT NOT NULL DEFAULT 'openai',
    api_key_env  TEXT,
    catalog_id   TEXT,
    key_sealed   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    deleted_at   TEXT,
    deleted_by   TEXT,
    source       TEXT NOT NULL DEFAULT 'app'
);
CREATE TABLE IF NOT EXISTS provider_store_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

_HIDDEN_VALUES = {"", "<hidden>", "changed", "None"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STORE_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


@contextmanager
def _open():
    """A connection that commits on success and is always closed."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _seal_key(entry: dict) -> Optional[str]:
    key = entry.get("api_key")
    if not key or str(key) in _HIDDEN_VALUES:
        return None
    try:
        from bot import vault

        return vault.seal(str(key))
    except Exception:
        # Never block a config save because the copy could not be encrypted; the provider is still stored, without its key.
        logger.warning("could not encrypt an API key for the provider store; the copy is kept without it", exc_info=True)
        return None


def _plain(entry: Optional[dict]) -> dict:
    """A ruamel round-trip map or plain dict as a plain dict of plain values."""
    return {str(k): v for k, v in dict(entry or {}).items()}


def record_saved(name: str, entry: dict, *, actor: str = "dashboard", source: str = "app") -> None:
    """The provider is (now) configured with this entry. Creates or refreshes its row and marks it active."""
    entry = _plain(entry)
    now = _now()
    with _lock, _open() as conn:
        conn.execute(
            "INSERT INTO provider_store (name, status, base_url, protocol, api_key_env, catalog_id, key_sealed, created_at, updated_at, source) "
            "VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET status='active', base_url=excluded.base_url, protocol=excluded.protocol, "
            "api_key_env=excluded.api_key_env, catalog_id=excluded.catalog_id, key_sealed=excluded.key_sealed, "
            "updated_at=excluded.updated_at, deleted_at=NULL, deleted_by=NULL",
            (name, entry.get("base_url", ""), entry.get("protocol", "openai") or "openai", entry.get("api_key_env"),
             entry.get("catalog_id"), _seal_key(entry), now, now, source),
        )


def record_deleted(name: str, entry: Optional[dict], *, actor: str = "dashboard", source: str = "app") -> None:
    """The provider was removed from the live registry. Keeps it, with its last known settings, as deleted."""
    now = _now()
    with _lock, _open() as conn:
        exists = conn.execute("SELECT 1 FROM provider_store WHERE name=?", (name,)).fetchone()
        if entry is not None:
            entry = _plain(entry)
            if exists:
                conn.execute(
                    "UPDATE provider_store SET status='deleted', base_url=?, protocol=?, api_key_env=?, catalog_id=?, "
                    "key_sealed=COALESCE(?, key_sealed), updated_at=?, deleted_at=?, deleted_by=? WHERE name=?",
                    (entry.get("base_url", ""), entry.get("protocol", "openai") or "openai", entry.get("api_key_env"),
                     entry.get("catalog_id"), _seal_key(entry), now, now, actor, name),
                )
            else:
                conn.execute(
                    "INSERT INTO provider_store (name, status, base_url, protocol, api_key_env, catalog_id, key_sealed, "
                    "created_at, updated_at, deleted_at, deleted_by, source) VALUES (?, 'deleted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, entry.get("base_url", ""), entry.get("protocol", "openai") or "openai", entry.get("api_key_env"),
                     entry.get("catalog_id"), _seal_key(entry), now, now, now, actor, source),
                )
        elif exists:
            conn.execute("UPDATE provider_store SET status='deleted', updated_at=?, deleted_at=?, deleted_by=? WHERE name=?",
                         (now, now, actor, name))


def sync(current: dict[str, dict]) -> None:
    """Reconcile the store with what config/providers.yaml holds right now.

    A provider in the file that the store has not seen is recorded (someone edited the file by hand, or it predates this
    store); one the store thinks is active but the file no longer has is moved to deleted, key and all. On the very
    first use it also rebuilds deleted providers from this install's own config history."""
    current = {str(n): _plain(e) for n, e in (current or {}).items()}
    with _lock, _open() as conn:
        rows = {r["name"]: r for r in conn.execute("SELECT name, status FROM provider_store")}
    for name, entry in current.items():
        if name not in rows or rows[name]["status"] != "active":
            record_saved(name, entry, actor="file", source="file")
    for name, row in rows.items():
        if row["status"] == "active" and name not in current:
            record_deleted(name, None, actor="file", source="file")
    _recover_once()


def _public(row: sqlite3.Row, toggles: dict[str, int]) -> dict:
    return {
        "name": row["name"], "status": row["status"], "base_url": row["base_url"], "protocol": row["protocol"],
        "api_key_env": row["api_key_env"], "catalog_id": row["catalog_id"], "has_key": bool(row["key_sealed"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "deleted_at": row["deleted_at"],
        "deleted_by": row["deleted_by"], "source": row["source"], "model_settings": toggles.get(row["name"], 0),
    }


def _toggle_counts() -> dict[str, int]:
    try:
        from bot import db

        with db._lock:
            rows = db.get_conn().execute("SELECT provider, COUNT(*) AS n FROM model_toggles GROUP BY provider").fetchall()
        return {r["provider"]: r["n"] for r in rows}
    except Exception:
        return {}


def list_all(status: Optional[str] = None) -> list[dict]:
    """Every stored provider, newest change first. Never includes a key, only whether one is kept."""
    with _lock, _open() as conn:
        if status:
            rows = conn.execute("SELECT * FROM provider_store WHERE status=? ORDER BY updated_at DESC, name", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM provider_store ORDER BY updated_at DESC, name").fetchall()
    toggles = _toggle_counts()
    return [_public(r, toggles) for r in rows]


def stored_entry(name: str) -> dict:
    """The settings of a deleted provider, ready to hand to providers.set_provider - including the decrypted key.
    Raises KeyError if there is no such deleted provider."""
    with _lock, _open() as conn:
        row = conn.execute("SELECT * FROM provider_store WHERE name=? AND status='deleted'", (name,)).fetchone()
    if row is None:
        raise KeyError(name)
    key = None
    if row["key_sealed"]:
        try:
            from bot import vault

            key = vault.unseal(row["key_sealed"])
        except Exception:
            logger.warning("the stored key for provider %r could not be decrypted; restoring it without a key", name)
    return {"base_url": row["base_url"], "protocol": row["protocol"], "api_key_env": row["api_key_env"],
            "catalog_id": row["catalog_id"], "api_key": key}


def purge(name: str) -> bool:
    """Forget a deleted provider for good: its row, its encrypted key and its per-model on/off choices.
    Refuses an active provider (returns False); it has to be removed first."""
    _mark_scanned()  # the history scan must never bring back something that was forgotten on purpose
    with _lock, _open() as conn:
        cur = conn.execute("DELETE FROM provider_store WHERE name=? AND status='deleted'", (name,))
        gone = cur.rowcount > 0
    if gone:
        try:
            from bot import db

            with db._lock:
                conn = db.get_conn()
                conn.execute("DELETE FROM model_toggles WHERE provider=?", (name,))
                conn.commit()
        except Exception:
            logger.warning("could not clear the model settings of purged provider %r", name, exc_info=True)
    return gone


# --------------------------------------------------------------- recovery --
# A config reload writes "providers.<name>: {settings} -> None" into config_history when a provider is removed, and the
# same with the sides swapped when one is added. That trail survives even though the file entry does not.
_CHANGE = re.compile(r"providers\.(?P<name>[^:\s]+): (?P<old>\{[^{}]*\}|None) -> (?P<new>\{[^{}]*\}|None)")


def _recover_entries(summaries: Iterable[str]) -> dict[str, dict]:
    """name -> last known settings, for providers whose latest recorded change was a removal."""
    last: dict[str, Optional[dict]] = {}
    for summary in summaries:
        for m in _CHANGE.finditer(summary or ""):
            try:
                old = None if m["old"] == "None" else ast.literal_eval(m["old"])
                new = None if m["new"] == "None" else ast.literal_eval(m["new"])
            except (ValueError, SyntaxError):
                continue
            if new is None and isinstance(old, dict):
                last[m["name"]] = old
            elif isinstance(new, dict):
                last[m["name"]] = None  # added again after a removal: not deleted any more
    return {n: e for n, e in last.items() if e}


def _insert_recovered(found: dict[str, dict], source: str) -> int:
    added = 0
    for name, entry in found.items():
        with _lock, _open() as conn:
            if conn.execute("SELECT 1 FROM provider_store WHERE name=?", (name,)).fetchone():
                continue
        record_deleted(name, entry, actor="history", source=source)
        added += 1
    return added


def _history_summaries() -> list[str]:
    """This install's own config history (the seam tests replace, so they never read a real database)."""
    from bot import db

    with db._lock:
        rows = db.get_conn().execute("SELECT summary FROM config_history ORDER BY id").fetchall()
    return [r["summary"] for r in rows]


def _mark_scanned() -> bool:
    """Record that the one-time history scan has happened. True if this call was the one that recorded it."""
    with _lock, _open() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO provider_store_meta (k, v) VALUES ('history_scanned', ?)", (_now(),))
        return cur.rowcount > 0


def _recover_once() -> None:
    if not _mark_scanned():
        return
    try:
        _insert_recovered(_recover_entries(_history_summaries()), "history")
    except Exception:
        logger.warning("could not rebuild deleted providers from the config history", exc_info=True)


def recover_from_db(db_file: Path) -> int:
    """Add the providers that another ABP database's history shows as removed, as deleted (restorable) ones.
    Returns how many were added. Read-only on the other database."""
    db_file = Path(db_file)
    if not db_file.is_file():
        raise FileNotFoundError(str(db_file))
    other = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    try:
        summaries = [r[0] for r in other.execute("SELECT summary FROM config_history ORDER BY id")]
    finally:
        other.close()
    return _insert_recovered(_recover_entries(summaries), "recovered")


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["list"]:
        for p in list_all():
            print(f"{p['status']:8} {p['name']:24} {p['base_url']}  key={'stored' if p['has_key'] else 'none'}")
        return 0
    if args[:2] == ["recover", "--from"] and len(args) == 3:
        state = Path(args[2])
        n = recover_from_db(state / "data" / "bot.db" if state.is_dir() else state)
        print(f"added {n} deleted provider(s); restore them from the dashboard's Deleted providers list")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
