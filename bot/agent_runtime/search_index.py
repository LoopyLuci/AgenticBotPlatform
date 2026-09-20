"""code_search and session_search: full-text search backed by SQLite FTS5 (roadmap P3).

**code_search** finds *where a concept is*, not just where a string is. `grep` needs the
right pattern; `code_search` takes a few words ("token refresh retry") and returns the code
chunks that best match, ranked by BM25, with the file and line. The index is a small SQLite
file per workspace in the agent state folder, built the first time and then updated
incrementally (only files whose size or modified time changed are re-read). Identifiers such
as `refresh_token` are kept whole. It is a keyword index, not a semantic one: no embeddings
are used, so it will not find "authentication" when the code only says "login".

**session_search** searches what was said in earlier conversations - the user's and the
agent's messages and the names of tools used - so the agent can find "what did we decide
about the database". It covers the current session and the other sessions of the same bot
instance, never another instance's.

Both need only the `sqlite3` module that ships with Python (FTS5 is checked at start-up; if
it is missing the tools report that instead of failing).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from bot.agent_runtime import toolspec
from bot.agent_runtime.coding_tools import SKIP_DIRS
from bot.agent_runtime.errors import ToolError, safe_path
from bot.agent_runtime.state import state_dir

CHUNK_LINES = 40
CHUNK_OVERLAP = 5
MAX_FILE_BYTES = 300_000
MAX_FILES = 8000
BUILD_BUDGET_S = 40.0
DEFAULT_RESULTS = 8
MAX_RESULTS = 30
TOKENIZE = "unicode61 tokenchars '_'"
_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("create virtual table t using fts5(a)")
        c.close()
        return True
    except sqlite3.Error:
        return False


def _require_fts5() -> None:
    if not fts5_available():
        raise ToolError("this Python's SQLite was built without FTS5, so search indexes are unavailable")


def _match_query(query: str, mode: str = "and") -> str:
    """FTS5 syntax built from plain words, so punctuation in a question cannot break the query."""
    words = []
    for w in _WORD.findall(query):
        words.append(w)
        parts = [p for p in _CAMEL.split(w) if p]
        if len(parts) > 1:                        # camelCase: also search the parts
            words.extend(p for p in parts if len(p) > 2)
    words = [w for w in dict.fromkeys(x.lower() for x in words) if len(w) > 1][:12]
    if not words:
        raise ToolError("the query has no searchable words")
    return (" OR " if mode == "or" else " ").join(f'"{w}"' for w in words)


def _identifier_parts(text: str) -> str:
    parts: set[str] = set()
    for ident in set(_WORD.findall(text)):
        if len(ident) < 4 or ("_" not in ident and not _CAMEL.search(ident)):
            continue
        for piece in re.split(r"_+", ident):
            for p in _CAMEL.split(piece):
                if len(p) > 1:
                    parts.add(p.lower())
    return " ".join(sorted(parts))


def _rank_query(conn: sqlite3.Connection, table: str, cols: str, query: str, limit: int) -> list[sqlite3.Row]:
    sql = (f"SELECT {cols}, bm25({table}) AS rank FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?")
    for mode in ("and", "or"):
        rows = conn.execute(sql, (_match_query(query, mode), limit)).fetchall()
        if rows:
            return rows
    return []


# ---- code index -------------------------------------------------------------------
def _code_db(workspace: Path) -> sqlite3.Connection:
    key = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:16]
    conn = sqlite3.connect(str(state_dir("code_index") / f"{key}.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER)")
    # `parts` holds the pieces of identifiers found in the chunk (refresh_token -> refresh token, parseHttp ->
    # parse http) so a search for the words finds the identifier.
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, line UNINDEXED, body, parts, tokenize=\"{TOKENIZE}\")")
    return conn


def _walk_text_files(root: Path, deadline: float):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if count >= MAX_FILES or time.monotonic() > deadline:
                return
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES or st.st_size == 0:
                continue
            count += 1
            yield p, st


def refresh_code_index(workspace: Path) -> dict:
    """Bring the index up to date. Returns {"indexed": n, "removed": n, "total": n, "complete": bool}."""
    _require_fts5()
    root = Path(workspace).resolve()
    conn = _code_db(root)
    deadline = time.monotonic() + BUILD_BUDGET_S
    indexed = removed = 0
    seen: set[str] = set()
    complete = True
    try:
        known = {r["path"]: (r["mtime_ns"], r["size"]) for r in conn.execute("SELECT * FROM files")}
        for p, st in _walk_text_files(root, deadline):
            rel = p.relative_to(root).as_posix()
            seen.add(rel)
            if known.get(rel) == (st.st_mtime_ns, st.st_size):
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                seen.discard(rel)
                continue
            lines = raw.decode("utf-8", errors="replace").split("\n")
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            step = CHUNK_LINES - CHUNK_OVERLAP
            for start in range(0, max(len(lines), 1), step):
                body = "\n".join(lines[start:start + CHUNK_LINES])
                if body.strip():
                    conn.execute("INSERT INTO chunks (path, line, body, parts) VALUES (?, ?, ?, ?)",
                                 (rel, start + 1, body, _identifier_parts(body)))
                if start + CHUNK_LINES >= len(lines):
                    break
            conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (rel, st.st_mtime_ns, st.st_size))
            indexed += 1
        if time.monotonic() > deadline:
            complete = False
        else:
            for rel in set(known) - seen:
                conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
                conn.execute("DELETE FROM files WHERE path = ?", (rel,))
                removed += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    finally:
        conn.close()
    return {"indexed": indexed, "removed": removed, "total": total, "complete": complete}


def search_code(workspace: Path, query: str, limit: int = DEFAULT_RESULTS, path_prefix: str = "") -> list[dict]:
    _require_fts5()
    root = Path(workspace).resolve()
    refresh_code_index(root)
    conn = _code_db(root)
    try:
        rows = _rank_query(conn, "chunks", "path, line, snippet(chunks, 2, '>>', '<<', ' ... ', 40) AS snip", query, limit * 3)
    finally:
        conn.close()
    out = []
    for r in rows:
        if path_prefix and not r["path"].startswith(path_prefix):
            continue
        out.append({"path": r["path"], "line": int(r["line"]), "snippet": r["snip"], "rank": r["rank"]})
        if len(out) >= limit:
            break
    return out


async def _code_search(inp: dict, *, workspace: Path, instance_id=None, device_tier=None) -> str:
    query = inp.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query is required")
    workspace = Path(workspace).resolve()
    prefix = ""
    if inp.get("path"):
        target = safe_path(workspace, inp["path"])
        rel = target.relative_to(workspace).as_posix()
        prefix = "" if rel == "." else (rel + "/" if target.is_dir() else rel)
    try:
        limit = max(1, min(int(inp.get("max_results") or DEFAULT_RESULTS), MAX_RESULTS))
    except (TypeError, ValueError):
        raise ToolError("max_results must be a number")
    results = await asyncio.to_thread(search_code, workspace, query, limit, prefix)
    if not results:
        return "No matches. (code_search is a keyword index: try other words, or use grep for an exact pattern.)"
    lines = []
    for r in results:
        snippet = " ".join(r["snippet"].split())[:280]
        lines.append(f"{r['path']}:{r['line']}\n    {snippet}")
    return "\n".join(lines)


# ---- session index -----------------------------------------------------------------
def _session_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(state_dir() / "session_index.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v INTEGER)")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5(session UNINDEXED, role UNINDEXED, msg_id UNINDEXED, "
                 f"created UNINDEXED, body, tokenize=\"{TOKENIZE}\")")
    return conn


def _message_text(content) -> str:
    """The words in a stored message: text blocks and tool names, not big tool outputs or thinking."""
    if isinstance(content, str):
        return content
    parts: list[str] = []

    def walk(v, depth=0):
        if depth > 5:
            return
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
        elif isinstance(v, dict):
            kind = v.get("type")
            if kind in ("thinking", "redacted_thinking", "image", "document"):
                return
            if kind == "tool_use":
                parts.append(f"[tool {v.get('name', '')}]")
                return
            if kind == "tool_result":
                body = v.get("content")
                parts.append("[tool result] " + (body if isinstance(body, str) else "")[:300])
                return
            for key in ("text", "content"):
                if key in v:
                    walk(v[key], depth + 1)

    walk(content)
    return "\n".join(p for p in parts if p)


def _purge_gone(conn: sqlite3.Connection) -> int:
    """Drop indexed sessions whose conversation no longer exists (cleared by a person), so deleting a
    conversation also removes it from search."""
    from bot import db

    live = {r["session_key"] for r in db.get_conn().execute("SELECT DISTINCT session_key FROM agent_messages")}
    indexed = {r["session"] for r in conn.execute("SELECT DISTINCT session FROM msgs")}
    gone = indexed - live
    for s in gone:
        conn.execute("DELETE FROM msgs WHERE session = ?", (s,))
    return len(gone)


def refresh_session_index(conn: Optional[sqlite3.Connection] = None) -> int:
    """Index agent_messages rows added since the last refresh. Returns how many were added."""
    from bot import db

    _require_fts5()
    own = conn is None
    conn = conn or _session_db()
    added = 0
    try:
        row = conn.execute("SELECT v FROM state WHERE k='last_id'").fetchone()
        last = int(row["v"]) if row else 0
        # A digest replaces old rows with new ids, so anything at or below the newest indexed id that
        # is missing is picked up by id; deletions are handled by dropping sessions that no longer exist.
        rows = db.get_conn().execute(
            "SELECT id, session_key, role, content, created_at FROM agent_messages WHERE id > ? ORDER BY id ASC LIMIT 5000",
            (last,)).fetchall()
        for r in rows:
            try:
                text = _message_text(json.loads(r["content"]))
            except ValueError:
                text = str(r["content"])
            if text.strip():
                conn.execute("INSERT INTO msgs (session, role, msg_id, created, body) VALUES (?, ?, ?, ?, ?)",
                             (r["session_key"], r["role"], r["id"], r["created_at"], text[:4000]))
                added += 1
            last = r["id"]
        conn.execute("INSERT OR REPLACE INTO state VALUES ('last_id', ?)", (last,))
        _purge_gone(conn)
        conn.commit()
    finally:
        if own:
            conn.close()
    return added


def sessions_for(instance_id: Optional[int], current: str) -> list[str]:
    from bot import db

    keys = {current} if current else set()
    if instance_id is not None:
        for r in db.get_conn().execute("SELECT desktop_session_key FROM chat_sessions WHERE instance_id = ?", (instance_id,)):
            keys.add(r["desktop_session_key"])
    return sorted(keys)


def search_sessions(instance_id: Optional[int], current: str, query: str, limit: int = DEFAULT_RESULTS,
                    scope: str = "instance") -> list[dict]:
    _require_fts5()
    conn = _session_db()
    try:
        while refresh_session_index(conn) >= 5000:
            pass
        allowed = set([current] if scope == "this" else sessions_for(instance_id, current))
        if not allowed:
            return []
        rows = _rank_query(conn, "msgs", "session, role, created, snippet(msgs, 4, '>>', '<<', ' ... ', 40) AS snip",
                           query, limit * 6)
    finally:
        conn.close()
    out = []
    for r in rows:
        if r["session"] in allowed:
            out.append({"session": r["session"], "role": r["role"], "when": r["created"], "snippet": r["snip"]})
            if len(out) >= limit:
                break
    return out


async def _session_search(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    query = inp.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query is required")
    scope = inp.get("scope") or "instance"
    if scope not in ("this", "instance"):
        raise ToolError("scope must be 'this' or 'instance'")
    try:
        limit = max(1, min(int(inp.get("max_results") or DEFAULT_RESULTS), MAX_RESULTS))
    except (TypeError, ValueError):
        raise ToolError("max_results must be a number")
    session = toolspec.current_session()
    results = await asyncio.to_thread(search_sessions, instance_id, session, query, limit, scope)
    if not results:
        return "No matches in earlier conversations."
    lines = []
    for r in results:
        where = "this conversation" if r["session"] == session else f"session {r['session'][-8:]}"
        lines.append(f"[{r['when'][:16]}] {r['role']} ({where}): " + " ".join(r["snippet"].split())[:300])
    return "\n".join(lines)


def register_all() -> None:
    S = {"type": "string"}
    toolspec.register(
        {"name": "code_search",
         "description": "Find code by meaning-in-words: give a few keywords (\"token refresh retry\") and get the best-matching "
                        "chunks of the project with file and line, ranked. Better than grep when you do not know the exact "
                        "text. It is a keyword index - it will not connect synonyms. Narrow with path.",
         "input_schema": {"type": "object", "properties": {"query": S, "path": S, "max_results": {"type": "integer"}},
                          "required": ["query"]}},
        toolspec.ToolSpec("code_search", "read", read_only=True, concurrency_safe=True, origin="registered"), _code_search)
    toolspec.register(
        {"name": "session_search",
         "description": "Search earlier conversations with this user (what was asked, decided or done). Use it when the user "
                        "refers to something from before that is not in your current context. scope: 'instance' (default, "
                        "this bot's other sessions too) or 'this'.",
         "input_schema": {"type": "object", "properties": {"query": S, "scope": {"type": "string", "enum": ["this", "instance"]},
                                                           "max_results": {"type": "integer"}}, "required": ["query"]}},
        toolspec.ToolSpec("session_search", "read", read_only=True, concurrency_safe=True, origin="registered"), _session_search)


register_all()
