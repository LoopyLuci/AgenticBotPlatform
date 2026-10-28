"""The catalog: one row for every file in the repository, parsed or not, so nothing is missing without a reason.

The symbol index only holds source files it can parse. The catalog holds every file the listing returns (git's tracked
and untracked-but-not-ignored files), each with its size, content hash, whether it is text or binary, and its status:

    indexed      in the symbol index (outline / find / read work on it)
    catalogued   listed with its hash, not parsed; `reason` says why: not-source (no parser for the type), too-large,
                 binary, generated, skipped-folder (vendored or build output), empty

Incremental like the index: a row is re-hashed only when the file's size or mtime changed. `report()` renders the
coverage, the files that are only catalogued and why, and the Markdown links whose target does not exist.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path, PurePosixPath
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS ci_catalog (
    path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, hash TEXT, kind TEXT, status TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ci_cat_status ON ci_catalog(status, reason);
"""

_CHUNK = 1 << 20


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _hash_file(path: Path) -> tuple[str, bool]:
    """Content hash (blake2b-128, the index's) and whether the file looks binary (a NUL in its first 4 KiB)."""
    h = hashlib.blake2b(digest_size=16)
    binary = False
    with path.open("rb") as fh:
        first = True
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            if first:
                binary = b"\x00" in block[:4096]
                first = False
            h.update(block)
    return h.hexdigest(), binary


def _reason(rel: str, size: int, binary: bool, suffixes: frozenset, skip_segments: frozenset, generated_name,
            max_bytes: int) -> str:
    parts = rel.split("/")
    if any(p in skip_segments or p.startswith(".") for p in parts[:-1]):
        return "skipped-folder"
    if size == 0:
        return "empty"
    if binary:
        return "binary"
    if os.path.splitext(parts[-1])[1].lower() not in suffixes:
        return "not-source"
    if generated_name.search(parts[-1]):
        return "generated"
    if size > max_bytes:
        return "too-large"
    return "generated"                       # a source file the indexer read and declined: it says it is generated


def build(conn: sqlite3.Connection, root: Path, listing: list[str], deadline: float) -> bool:
    """Bring ci_catalog in line with `listing`. Returns False when the deadline cut it short (rows already done stay)."""
    from bot.codeintel import indexer, parsers

    ensure_schema(conn)
    suffixes = parsers.source_suffixes()
    indexed = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM ci_files")}
    known = {r["path"]: r for r in conn.execute("SELECT * FROM ci_catalog")}
    seen: set[str] = set()
    complete = True
    for rel in listing:
        if time.monotonic() > deadline:
            complete = False
            break
        full = root / rel
        try:
            st = full.stat()
        except OSError:
            continue
        if not full.is_file():
            continue
        seen.add(rel)
        old = known.get(rel)
        if rel in indexed:
            row = (rel, st.st_size, st.st_mtime_ns, indexed[rel], "text", "indexed", "")
        elif old is not None and old["size"] == st.st_size and old["mtime_ns"] == st.st_mtime_ns and old["status"] != "indexed":
            continue
        else:
            try:
                digest, binary = _hash_file(full)
            except OSError:
                continue
            reason = _reason(rel, st.st_size, binary, suffixes, indexer.SKIP_SEGMENTS, indexer._GENERATED_NAME,
                             indexer.MAX_FILE_BYTES)
            row = (rel, st.st_size, st.st_mtime_ns, digest, "binary" if binary else "text", "catalogued", reason)
        if old is None or tuple(old) != row:
            conn.execute("INSERT OR REPLACE INTO ci_catalog VALUES (?,?,?,?,?,?,?)", row)
    if complete:
        gone = set(known) - seen
        conn.executemany("DELETE FROM ci_catalog WHERE path=?", [(p,) for p in gone])
    return complete


def broken_links(conn: sqlite3.Connection, root: Path, prefix: str = "") -> list[tuple[str, int, str]]:
    """(file, line, target) for each relative Markdown link whose target is not a file or folder on disk."""
    out = []
    rows = conn.execute("SELECT file, module, line FROM ci_imports WHERE resolved IS NULL AND (file LIKE '%.md' OR "
                        "file LIKE '%.markdown') AND file LIKE ? ORDER BY file, line", (prefix + "%",)).fetchall()
    for r in rows:
        target = r["module"]
        base = PurePosixPath(r["file"]).parent
        rel = target.lstrip("/") if target.startswith("/") else str(base / target)
        try:
            if (root / os.path.normpath(rel)).exists():
                continue
        except (OSError, ValueError):
            pass
        out.append((r["file"], r["line"], target))
    return out


def report(conn: sqlite3.Connection, root: Path, prefix: str = "", status: str = "", limit: int = 200) -> tuple[list[str], dict]:
    """Coverage lines for the catalog under `prefix` (a folder, '' for all) and the numbers behind them."""
    ensure_schema(conn)
    like = prefix.rstrip("/") + "/%" if prefix else "%"
    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM ci_catalog WHERE path LIKE ?", (like,)).fetchone()
    by = conn.execute("SELECT status, reason, COUNT(*) AS n, SUM(size) AS bytes FROM ci_catalog WHERE path LIKE ? "
                      "GROUP BY status, reason ORDER BY status DESC, n DESC", (like,)).fetchall()
    lines = [f"catalog of {prefix or '.'}: {total[0]} files, {total[1]:,} bytes"]
    data: dict = {"files": total[0], "bytes": total[1], "by_status": {}}
    for r in by:
        key = r["status"] + (f"/{r['reason']}" if r["reason"] else "")
        data["by_status"][key] = r["n"]
        lines.append(f"  {key}: {r['n']} files, {r['bytes'] or 0:,} bytes")
    tops = conn.execute("SELECT path, status FROM ci_catalog WHERE path LIKE ?", (like,)).fetchall()
    depth = len(prefix.rstrip("/").split("/")) if prefix else 0
    folders: dict[str, list[int]] = {}
    for r in tops:
        parts = r["path"].split("/")
        key = "/".join(parts[: depth + 1]) + ("/" if len(parts) > depth + 1 else "")
        n = folders.setdefault(key, [0, 0])
        n[0] += 1
        n[1] += r["status"] == "indexed"
    lines.append("by entry (files, indexed):")
    lines += [f"  {k}: {v[0]}, {v[1]}" for k, v in sorted(folders.items())]
    where = "status='catalogued'" + (" AND reason=?" if status else "")
    args: tuple = (like, status) if status else (like,)
    rest = conn.execute(f"SELECT path, size, reason FROM ci_catalog WHERE path LIKE ? AND {where} ORDER BY reason, path "
                        f"LIMIT {int(limit)}", args).fetchall()
    if rest:
        lines.append(f"catalogued, not parsed{' (' + status + ')' if status else ''}:")
        lines += [f"  {r['path']} [{r['reason']}, {r['size']:,} bytes]" for r in rest]
    links = broken_links(conn, root, prefix.rstrip("/") + "/" if prefix else "")
    data["broken_links"] = len(links)
    if links:
        lines.append(f"broken Markdown links ({len(links)}):")
        lines += [f"  {f}:{line} -> {t}" for f, line, t in links[:limit]]
    return lines, data


def hash_of(conn: sqlite3.Connection, rel: str) -> Optional[str]:
    row = conn.execute("SELECT hash FROM ci_catalog WHERE path=?", (rel,)).fetchone()
    return row["hash"] if row else None
