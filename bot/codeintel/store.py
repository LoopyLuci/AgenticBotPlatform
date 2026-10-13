"""The index database: one SQLite file per repository, shared with code_search's chunk tables.

The file lives where search_index keeps its own (the agent state folder, `code_index/<hash of the root>.db`, or the
folder named by `native_agent.code_index_dir`). The code_search tables (`files`, `chunks`, `vecs`, `vfiles`) stay
exactly as they were; the tables here all start with `ci_`, so the FTS5 chunks and the embeddings are reused, not
duplicated. Every operation opens its own connection and closes it on the same thread, which is what makes the index
safe to call from any number of threads; the database runs in WAL mode so readers never wait for a writer.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = "3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ci_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ci_files (
    path TEXT PRIMARY KEY, lang TEXT, module TEXT, size INTEGER, mtime_ns INTEGER, hash TEXT,
    lines INTEGER, is_test INTEGER, parser TEXT);
CREATE TABLE IF NOT EXISTS ci_symbols (
    id INTEGER PRIMARY KEY, file TEXT, kind TEXT, name TEXT, qname TEXT, fq TEXT, signature TEXT, doc TEXT,
    decorators TEXT, start_line INTEGER, end_line INTEGER, parent_id INTEGER, depth INTEGER);
CREATE INDEX IF NOT EXISTS ci_sym_name ON ci_symbols(name);
CREATE INDEX IF NOT EXISTS ci_sym_file ON ci_symbols(file, start_line);
CREATE INDEX IF NOT EXISTS ci_sym_fq ON ci_symbols(fq);
CREATE TABLE IF NOT EXISTS ci_refs (name TEXT, file TEXT, line INTEGER, sym_id INTEGER, kind TEXT);
CREATE INDEX IF NOT EXISTS ci_ref_name ON ci_refs(name);
CREATE INDEX IF NOT EXISTS ci_ref_file ON ci_refs(file);
CREATE TABLE IF NOT EXISTS ci_imports (file TEXT, module TEXT, name TEXT, level INTEGER, line INTEGER, resolved TEXT);
CREATE INDEX IF NOT EXISTS ci_imp_file ON ci_imports(file);
CREATE INDEX IF NOT EXISTS ci_imp_resolved ON ci_imports(resolved);
CREATE TABLE IF NOT EXISTS ci_testmap (test_file TEXT, test_qname TEXT, source_file TEXT, how TEXT, weight INTEGER);
CREATE INDEX IF NOT EXISTS ci_tm_src ON ci_testmap(source_file);
CREATE INDEX IF NOT EXISTS ci_tm_test ON ci_testmap(test_file);
CREATE TABLE IF NOT EXISTS ci_usage (tool TEXT PRIMARY KEY, calls INTEGER, tokens INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS ci_symfts USING fts5(name, parts, qname, doc, tokenize="unicode61 tokenchars '_'");
"""
_TABLES = ("ci_testmap", "ci_imports", "ci_refs", "ci_symbols", "ci_files", "ci_symfts")

_ready: set[str] = set()          # databases whose schema this process has already verified
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def write_lock(root: Path) -> threading.RLock:
    """One writer at a time per repository, in this process."""
    key = str(Path(root).resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def db_path(root: Path) -> Path:
    from bot.agent_runtime import search_index

    return search_index.code_db_path(Path(root).resolve())


def connect(root: Path) -> sqlite3.Connection:
    """A connection to the repository's index (the schema is created, or rebuilt when its version changed).
    The caller closes it, on the same thread."""
    from bot.agent_runtime import search_index

    conn = search_index._code_db(Path(root).resolve())          # also creates the code_search tables
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    key = str(db_path(root))
    if key in _ready and Path(key).exists():
        return conn
    conn.execute("CREATE TABLE IF NOT EXISTS ci_meta (k TEXT PRIMARY KEY, v TEXT)")
    row = conn.execute("SELECT v FROM ci_meta WHERE k='schema'").fetchone()
    if row is not None and row["v"] != SCHEMA_VERSION:
        with write_lock(root):
            for t in _TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute("DELETE FROM ci_meta")
            conn.commit()
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT v FROM ci_meta WHERE k='schema'").fetchone() is None:
        conn.execute("INSERT OR REPLACE INTO ci_meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
        conn.commit()
    _ready.add(key)
    return conn


def meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM ci_meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO ci_meta VALUES (?, ?)", (key, value))
