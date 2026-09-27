"""Skills, shared context docs, plugins, external MCP servers and agent hooks.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from bot import db as _db


def install_skill(instance_id: Optional[int], name: str, description: str, content: str) -> int:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM skills WHERE instance_id IS ? AND name=?", (instance_id, name))
        cur = conn.execute(
            "INSERT INTO skills (instance_id, name, description, content, installed_at) VALUES (?, ?, ?, ?, ?)",
            (instance_id, name, description, content, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_skills(instance_id: Optional[int]) -> list[sqlite3.Row]:
    """Skills visible to this instance: its own plus every global
    (instance_id IS NULL) one."""
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM skills WHERE instance_id IS NULL OR instance_id=? ORDER BY name", (instance_id,)
    ).fetchall()


def get_skill(instance_id: Optional[int], name: str) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute(
        "SELECT * FROM skills WHERE (instance_id IS NULL OR instance_id=?) AND name=? "
        "ORDER BY instance_id IS NULL LIMIT 1",
        (instance_id, name),
    ).fetchone()


def delete_skill(instance_id: Optional[int], name: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM skills WHERE instance_id IS ? AND name=?", (instance_id, name))
        conn.commit()


def set_context_doc(name: str, content: str, updated_by: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO shared_context_docs (name, content, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (name, content, _db._now(), updated_by),
        )
        conn.commit()


def get_context_doc(name: str) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM shared_context_docs WHERE name=?", (name,)).fetchone()


def list_context_docs() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT name, updated_at, updated_by, length(content) AS size FROM shared_context_docs ORDER BY name").fetchall()


def delete_context_doc(name: str) -> bool:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("DELETE FROM shared_context_docs WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0


def install_plugin_row(name: str, path: str, description: str) -> int:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM plugins WHERE name=?", (name,))
        cur = conn.execute(
            "INSERT INTO plugins (name, path, description, enabled, installed_at) VALUES (?, ?, ?, 1, ?)",
            (name, path, description, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_plugin_rows() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM plugins ORDER BY name").fetchall()


def get_plugin_row(name: str) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM plugins WHERE name=?", (name,)).fetchone()


def set_plugin_enabled(name: str, enabled: bool) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE plugins SET enabled=? WHERE name=?", (1 if enabled else 0, name))
        conn.commit()


def delete_plugin_row(name: str) -> bool:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("DELETE FROM plugins WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
        conn.commit()


def add_external_mcp_server(
    name: str, transport: str, *, command: Optional[str] = None, args_json: str = "[]",
    env_json: str = "{}", url: Optional[str] = None, auth_token: Optional[str] = None,
    oauth_enabled: bool = False, instance_id: Optional[int] = None,
) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO external_mcp_servers "
            "(name, transport, command, args_json, env_json, url, auth_token, oauth_enabled, enabled, instance_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (name, transport, command, args_json, env_json, url, auth_token, 1 if oauth_enabled else 0, instance_id, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_external_mcp_servers(instance_id: Optional[int] = None) -> list[sqlite3.Row]:
    """Every global (instance_id IS NULL) server, plus [instance_id]'s own
    scoped ones when given — omitting instance_id lists every row
    regardless of scope, for the dashboard/Telegram management UI."""
    conn = _db.get_conn()
    if instance_id is None:
        return conn.execute("SELECT * FROM external_mcp_servers ORDER BY name").fetchall()
    return conn.execute(
        "SELECT * FROM external_mcp_servers WHERE instance_id IS NULL OR instance_id=? ORDER BY name", (instance_id,)
    ).fetchall()


def get_external_mcp_server(name: str) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM external_mcp_servers WHERE name=?", (name,)).fetchone()


def set_external_mcp_server_enabled(name: str, enabled: bool) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE external_mcp_servers SET enabled=? WHERE name=?", (1 if enabled else 0, name))
        conn.commit()


def delete_external_mcp_server(name: str) -> bool:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("DELETE FROM external_mcp_servers WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0


def set_external_mcp_oauth_client_info(name: str, client_info_json: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE external_mcp_servers SET oauth_client_info_json=? WHERE name=?", (client_info_json, name))
        conn.commit()


def set_external_mcp_oauth_tokens(name: str, tokens_json: str) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE external_mcp_servers SET oauth_tokens_json=? WHERE name=?", (tokens_json, name))
        conn.commit()


def add_agent_hook(event: str, command: str, *, matcher: Optional[str] = None, instance_id: Optional[int] = None) -> int:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO agent_hooks (event, matcher, command, instance_id, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (event, matcher, command, instance_id, _db._now()),
        )
        conn.commit()
        return cur.lastrowid


def list_agent_hooks(event: Optional[str] = None, instance_id: Optional[int] = None) -> list[sqlite3.Row]:
    """Every global (instance_id IS NULL) hook, plus [instance_id]'s own
    scoped ones when given — omitting both filters lists every row, for
    the dashboard/Telegram management UI (mirrors
    list_external_mcp_servers()'s own shape)."""
    conn = _db.get_conn()
    clauses = []
    params: list[Any] = []
    if event is not None:
        clauses.append("event=?")
        params.append(event)
    if instance_id is not None:
        clauses.append("(instance_id IS NULL OR instance_id=?)")
        params.append(instance_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(f"SELECT * FROM agent_hooks{where} ORDER BY id", params).fetchall()


def get_agent_hook(hook_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM agent_hooks WHERE id=?", (hook_id,)).fetchone()


def set_agent_hook_enabled(hook_id: int, enabled: bool) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE agent_hooks SET enabled=? WHERE id=?", (1 if enabled else 0, hook_id))
        conn.commit()


def delete_agent_hook(hook_id: int) -> bool:
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute("DELETE FROM agent_hooks WHERE id=?", (hook_id,))
        conn.commit()
        return cur.rowcount > 0
