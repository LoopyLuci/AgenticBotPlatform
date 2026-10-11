"""code_search and session_search: full-text search backed by SQLite FTS5 (roadmap P3).

**code_search** finds *where a concept is*, not just where a string is. `grep` needs the
right pattern; `code_search` takes a few words ("token refresh retry") and returns the code
chunks that best match, ranked by BM25, with the file and line. The index is a small SQLite
file per workspace in the agent state folder, built the first time and then updated
incrementally (only files whose size or modified time changed are re-read). Identifiers such
as `refresh_token` are kept whole. On its own it is a keyword index, so it will not find
"authentication" when the code only says "login".

**Semantic layer (optional).** When a ModelMistress hub is running, each chunk is also embedded
through its OpenAI-compatible `/v1/embeddings` (model `mm-embed-1` by default), and a search
merges the BM25 ranking with the nearest chunks by cosine similarity using reciprocal-rank
fusion, so "authenticate user" finds `def login`. Vectors are float32 blobs in the same SQLite
file, kept per file and refreshed only for files that changed; each refresh embeds a bounded
number of chunks so a big repository fills in over several searches. With no hub, a failing
hub, or `native_agent.code_search.embeddings: false`, the search is keyword-only exactly as
before, and embeddings never make the tool fail or wait long.

    native_agent:
      code_search:
        embeddings: true             # false = keyword only
        embedding_model: mm-embed-1
        embedding_max_chunks: 512    # embedded per search at most
        embedding_batch: 32          # chunks per request

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
from array import array
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

EMBED_MODEL = "mm-embed-1"
EMBED_BATCH = 32
EMBED_MAX_CHUNKS = 512
EMBED_BUDGET_S = 20.0
EMBED_QUERY_TIMEOUT_S = 8.0
EMBED_CHUNK_CHARS = 2000
MIN_COSINE = 0.2
RRF_K = 60
QUERY_INSTRUCTION = "Instruct: Given a code search query, retrieve relevant code\nQuery: "


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
    # Embeddings: one float32 blob (unit length) per chunk; `vfiles` records which version of the file they
    # were made from, so a changed file is re-embedded and a deleted one dropped.
    conn.execute("CREATE TABLE IF NOT EXISTS vecs (path TEXT, line INTEGER, vec BLOB, PRIMARY KEY (path, line))")
    conn.execute("CREATE TABLE IF NOT EXISTS vfiles (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, model TEXT)")
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


# ---- semantic layer ---------------------------------------------------------------
def _embed_cfg() -> dict:
    try:
        from bot.config import config

        c = (config.current.get("native_agent") or {}).get("code_search")
        return c if isinstance(c, dict) else {}
    except Exception:  # noqa: BLE001 - a config problem must not stop a search
        return {}


def embeddings_enabled() -> bool:
    return _embed_cfg().get("embeddings", True) is not False


def _hub():
    from bot.modelmistress import client as mm

    return mm.find_cached()


def _embed_texts(hub, model: str, texts: list[str], timeout: float) -> list[list[float]]:
    from bot.modelmistress import client as mm

    data = mm.request("POST", "/v1/embeddings", body={"model": model, "input": texts}, hub=hub, timeout=timeout)
    rows = sorted((data or {}).get("data") or [], key=lambda r: r.get("index", 0))
    vecs = [r["embedding"] for r in rows]
    if len(vecs) != len(texts) or not all(isinstance(v, list) and v for v in vecs):
        raise ValueError("the embeddings reply did not match the request")
    return vecs


def _unit_blob(vec: list[float]) -> bytes:
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return array("f", (x / norm for x in vec)).tobytes()


def refresh_embeddings(workspace: Path, hub=None) -> dict:
    """Embed the chunks of files whose vectors are missing or stale, at most `embedding_max_chunks` per call.
    Raises on a hub failure (search_code catches it). Returns {"embedded": chunks, "pending": files left}."""
    cfg = _embed_cfg()
    model = str(cfg.get("embedding_model") or EMBED_MODEL)
    batch = max(1, int(cfg.get("embedding_batch") or EMBED_BATCH))
    cap = max(1, int(cfg.get("embedding_max_chunks") or EMBED_MAX_CHUNKS))
    hub = hub or _hub()
    if hub is None:
        raise RuntimeError("no embeddings hub")
    conn = _code_db(Path(workspace).resolve())
    deadline = time.monotonic() + EMBED_BUDGET_S
    embedded = 0
    try:
        conn.execute("DELETE FROM vecs WHERE path NOT IN (SELECT path FROM files)")
        conn.execute("DELETE FROM vfiles WHERE path NOT IN (SELECT path FROM files)")
        stale = conn.execute(
            "SELECT f.path, f.mtime_ns, f.size FROM files f LEFT JOIN vfiles v ON v.path = f.path "
            "WHERE v.path IS NULL OR v.mtime_ns != f.mtime_ns OR v.size != f.size OR v.model != ? ORDER BY f.path",
            (model,)).fetchall()
        pending = len(stale)
        for f in stale:
            if embedded >= cap or time.monotonic() > deadline:
                break
            chunks = conn.execute("SELECT line, body FROM chunks WHERE path = ? ORDER BY CAST(line AS INTEGER)",
                                  (f["path"],)).fetchall()
            vecs: list[list[float]] = []
            for i in range(0, len(chunks), batch):
                texts = [c["body"][:EMBED_CHUNK_CHARS] for c in chunks[i:i + batch]]
                vecs.extend(_embed_texts(hub, model, texts, 60.0))
            conn.execute("DELETE FROM vecs WHERE path = ?", (f["path"],))
            conn.executemany("INSERT INTO vecs VALUES (?, ?, ?)",
                             [(f["path"], int(c["line"]), _unit_blob(v)) for c, v in zip(chunks, vecs)])
            conn.execute("INSERT OR REPLACE INTO vfiles VALUES (?, ?, ?, ?)", (f["path"], f["mtime_ns"], f["size"], model))
            conn.commit()
            embedded += len(chunks)
            pending -= 1
        conn.commit()
    finally:
        conn.close()
    return {"embedded": embedded, "pending": pending}


def _vector_hits(workspace: Path, query: str, limit: int, hub) -> list[tuple[str, int]]:
    """The chunks nearest the query by cosine similarity, best first."""
    import numpy as np

    model = str(_embed_cfg().get("embedding_model") or EMBED_MODEL)
    q = _embed_texts(hub, model, [QUERY_INSTRUCTION + query], EMBED_QUERY_TIMEOUT_S)[0]
    qv = np.asarray(q, dtype=np.float32)
    qv = qv / (np.linalg.norm(qv) or 1.0)
    conn = _code_db(Path(workspace).resolve())
    try:
        rows = conn.execute(
            "SELECT v.path, v.line, v.vec FROM vecs v JOIN vfiles vf ON vf.path = v.path JOIN files f ON f.path = v.path "
            "WHERE vf.mtime_ns = f.mtime_ns AND vf.size = f.size AND vf.model = ?", (model,)).fetchall()
    finally:
        conn.close()
    rows = [r for r in rows if len(r["vec"]) == qv.size * 4]
    if not rows:
        return []
    mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), qv.size)
    sims = mat @ qv
    order = np.argsort(-sims)[:limit]
    return [(rows[i]["path"], int(rows[i]["line"])) for i in order if sims[i] >= MIN_COSINE]


def _fuse(keyword: list[tuple[str, int]], vector: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Reciprocal-rank fusion: a chunk's score is the sum of 1/(60 + rank) over the lists it is in."""
    score: dict[tuple[str, int], float] = {}
    for ranking in (keyword, vector):
        for rank, key in enumerate(ranking):
            score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(score, key=lambda k: -score[k])


def search_code_ex(workspace: Path, query: str, limit: int = DEFAULT_RESULTS, path_prefix: str = "") -> tuple[list[dict], str]:
    """(results, mode): mode is "hybrid", "keyword" (embeddings off) or "keyword-fallback" (wanted, unavailable)."""
    _require_fts5()
    root = Path(workspace).resolve()
    refresh_code_index(root)
    vector: list[tuple[str, int]] = []
    mode = "keyword"
    if embeddings_enabled():
        mode = "keyword-fallback"
        try:
            hub = _hub()
            if hub is not None:
                refresh_embeddings(root, hub)
                vector = _vector_hits(root, query, limit * 3, hub)
                mode = "hybrid"
        except Exception:  # noqa: BLE001 - embeddings are an extra: any failure means keyword-only
            vector, mode = [], "keyword-fallback"
    conn = _code_db(root)
    try:
        try:
            rows = _rank_query(conn, "chunks", "path, line, snippet(chunks, 2, '>>', '<<', ' ... ', 40) AS snip", query, limit * 3)
        except ToolError:
            if not vector:
                raise
            rows = []
        by_key = {(r["path"], int(r["line"])): r for r in rows}
        keyword = [(r["path"], int(r["line"])) for r in rows]
        order = _fuse(keyword, vector) if vector else keyword
        out = []
        for key in order:
            if path_prefix and not key[0].startswith(path_prefix):
                continue
            r = by_key.get(key)
            if r is not None:
                out.append({"path": key[0], "line": key[1], "snippet": r["snip"], "rank": r["rank"]})
            else:
                body = conn.execute("SELECT body FROM chunks WHERE path = ? AND line = ?", key).fetchone()
                if body is None:
                    continue
                out.append({"path": key[0], "line": key[1], "snippet": body["body"][:400], "rank": None})
            if len(out) >= limit:
                break
    finally:
        conn.close()
    return out, mode


def search_code(workspace: Path, query: str, limit: int = DEFAULT_RESULTS, path_prefix: str = "") -> list[dict]:
    return search_code_ex(workspace, query, limit, path_prefix)[0]


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
    except (TypeError, ValueError) as exc:
        raise ToolError("max_results must be a number") from exc
    results, mode = await asyncio.to_thread(search_code_ex, workspace, query, limit, prefix)
    note = "\n[keyword only: the embeddings model is not available]" if mode == "keyword-fallback" else ""
    if not results:
        return "No matches. (code_search matches words: try other words, or use grep for an exact pattern.)" + note
    lines = []
    for r in results:
        snippet = " ".join(r["snippet"].split())[:280]
        lines.append(f"{r['path']}:{r['line']}\n    {snippet}")
    return "\n".join(lines) + note


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
    except (TypeError, ValueError) as exc:
        raise ToolError("max_results must be a number") from exc
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
                        "text. Matches words, and meaning too when the local embeddings model is running. Narrow with path.",
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
