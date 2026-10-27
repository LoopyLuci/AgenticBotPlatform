"""The token-minimal navigation tools, as a plain library: every function takes the repository root first and returns a
`Result` (compact text, a token estimate, and structured data for callers that want it).

    outline(root, path)            signatures and line ranges, no bodies
    find_symbol(root, query)       exact / qualified / prefix / fuzzy (FTS5) symbol search
    read_symbol(root, target)      the exact body of one symbol, by qualified name or path:line
    references(root, symbol)       where a name is used (import-linked first; says when the name is ambiguous)
    callers(root, symbol)          which symbols call it
    related_tests(root, target)    tests for a file or a symbol (imports, references, naming, optional coverage)
    impact(root, changes)          from a diff or a list of paths: symbols changed, their callers, tests to run
    context_pack(root, task, ...)  a budgeted bundle for a task: signatures, key bodies, tests, rules

Every tool enforces a token budget (`max_tokens`) and says so when it had to cut. The index is brought up to date
first (cheap: unchanged files are not read); a file named in a call is checked individually, so a read right after an
edit never serves stale lines. The functions are synchronous and thread-safe; async callers use `asyncio.to_thread`.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bot.codeintel import catalog, indexer, store, summaries

CHARS_PER_TOKEN = 3.5
FRESH_TTL_S = 2.0
CONTAINERS = ("class", "struct", "interface", "trait", "impl", "enum", "module", "namespace", "section")

_fresh: dict[str, float] = {}
_fresh_lock = threading.Lock()
_usage_lock = threading.Lock()
_usage: dict[str, list[int]] = {}          # tool -> [calls, tokens] for this process


def est_tokens(text: str) -> int:
    return int(math.ceil(len(text) / CHARS_PER_TOKEN)) if text else 0


@dataclass
class Result:
    text: str
    tokens: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False

    def render(self) -> str:
        """The text an agent sees: the answer and what it cost."""
        return f"{self.text}\n[~{self.tokens} tokens{', cut to budget' if self.truncated else ''}]"


class CodeIntelError(ValueError):
    """A bad argument (unknown path, empty query); the message is meant for the agent."""


def _result(root: Path, tool: str, lines: list[str], max_tokens: int, data: Optional[dict] = None, hint: str = "") -> Result:
    budget_chars = int(max_tokens * CHARS_PER_TOKEN)
    out: list[str] = []
    used = 0
    cut = 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > budget_chars and out:
            cut = len(lines) - i
            break
        if len(line) > budget_chars:
            line = line[:budget_chars - 1] + "…"
        out.append(line)
        used += len(line) + 1
    if cut:
        out.append(f"… {cut} more line(s) not shown{(' - ' + hint) if hint else ''}")
    text = "\n".join(out)
    res = Result(text, est_tokens(text), data or {}, bool(cut))
    _record(root, tool, res.tokens)
    return res


def _record(root: Path, tool: str, tokens: int) -> None:
    with _usage_lock:
        row = _usage.setdefault(tool, [0, 0])
        row[0] += 1
        row[1] += tokens
    lock = store.write_lock(root)
    if not lock.acquire(timeout=0.25):       # an index build is running: the in-process counter still counted
        return
    try:
        conn = store.connect(root)
        try:
            conn.execute("INSERT INTO ci_usage VALUES (?, 1, ?) ON CONFLICT(tool) DO UPDATE SET calls = calls + 1, tokens = tokens + ?",
                         (tool, tokens, tokens))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - counters never fail a tool
        pass
    finally:
        lock.release()


# ---------------------------------------------------------------- freshness
def ensure(root: Path, *, chunks: bool = False, force: bool = False) -> None:
    """Update the index unless it was updated moments ago."""
    root = Path(root).resolve()
    key = str(root)
    now = time.monotonic()
    with _fresh_lock:
        last = _fresh.get(key, 0.0)
        if not force and not chunks and now - last < FRESH_TTL_S:
            return
        if chunks and not force and now - _fresh.get(key + "#chunks", 0.0) < FRESH_TTL_S * 5:
            chunks = False
    indexer.update(root, chunks=chunks)
    with _fresh_lock:
        _fresh[key] = time.monotonic()
        if chunks:
            _fresh[key + "#chunks"] = _fresh[key]


def forget_freshness(root: Path) -> None:
    with _fresh_lock:
        for k in [k for k in _fresh if k.startswith(str(Path(root).resolve()))]:
            _fresh.pop(k, None)


def _check_file(root: Path, conn: sqlite3.Connection, rel: str) -> bool:
    """Make sure one file's rows match the disk; returns False if the file is not indexed at all."""
    try:
        st = (root / rel).stat()
    except OSError:
        return conn.execute("SELECT 1 FROM ci_files WHERE path=?", (rel,)).fetchone() is not None
    row = conn.execute("SELECT mtime_ns, size FROM ci_files WHERE path=?", (rel,)).fetchone()
    if row is None or row["mtime_ns"] != st.st_mtime_ns or row["size"] != st.st_size:
        indexer.update(root, only=[rel])
        conn.commit()          # the connection may hold a stale read snapshot; start a new one
        return conn.execute("SELECT 1 FROM ci_files WHERE path=?", (rel,)).fetchone() is not None
    return True


def _open(root: Path) -> tuple[Path, sqlite3.Connection]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise CodeIntelError(f"{root} is not a folder")
    return root, store.connect(root)


def _rel(root: Path, path: str) -> str:
    """A repository-relative posix path from what the caller gave; refuses anything outside the root."""
    p = str(path or "").strip().replace("\\", "/")
    if not p:
        raise CodeIntelError("a path is required")
    candidate = Path(p)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise CodeIntelError(f"{path!r} is outside the repository") from exc
    norm = os.path.normpath(p).replace("\\", "/")
    if norm.startswith("../") or norm == "..":
        raise CodeIntelError(f"{path!r} is outside the repository")
    return "." if norm == "." else norm


def _defs(conn: sqlite3.Connection, symbol: str, kind: str = "") -> list[sqlite3.Row]:
    """Definitions a name may mean: by full name, by in-file qualified name, as a suffix of one, then by simple name."""
    q = symbol.strip().replace("::", ".")
    if not q:
        raise CodeIntelError("a symbol name is required")
    extra = " AND kind = ?" if kind else ""
    args = (kind,) if kind else ()
    cols = "id, file, kind, name, qname, fq, signature, doc, decorators, start_line, end_line, parent_id, depth"
    for where, a in (("fq = ?", (q,)), ("qname = ?", (q,)),
                     ("(substr(fq, -(length(?) + 1)) = '.' || ? OR substr(qname, -(length(?) + 1)) = '.' || ?)", (q, q, q, q)),
                     ("name = ?", (q.rsplit(".", 1)[-1],))):
        rows = conn.execute(f"SELECT {cols} FROM ci_symbols WHERE {where}{extra} ORDER BY file, start_line", (*a, *args)).fetchall()
        if rows:
            return rows
    return []


def _is_test(conn: sqlite3.Connection, rel: str) -> bool:
    row = conn.execute("SELECT is_test FROM ci_files WHERE path=?", (rel,)).fetchone()
    return bool(row and row["is_test"])


def _loc(row) -> str:
    return f"{row['file']}:{row['start_line']}-{row['end_line']}"


def _read_lines(root: Path, rel: str) -> list[str]:
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CodeIntelError(f"cannot read {rel}: {exc}") from exc
    return text.split("\n")


# ---------------------------------------------------------------- outline
def _visible(conn_rows: dict[int, sqlite3.Row], row) -> bool:
    """A symbol shows in an outline when all its ancestors are containers (so nested helper functions stay hidden)."""
    pid = row["parent_id"]
    while pid is not None:
        parent = conn_rows.get(pid)
        if parent is None:
            return True
        if parent["kind"] not in CONTAINERS:
            return False
        pid = parent["parent_id"]
    return True


def _outline_line(row) -> str:
    sig = row["signature"] or row["name"]
    if row["decorators"]:
        sig = f"{row['decorators']} {sig}"
    doc = f"  # {row['doc']}" if row["doc"] else ""
    return f"{'  ' * row['depth']}L{row['start_line']}-{row['end_line']} {sig}{doc}"


def outline(root: Path, path: str, max_tokens: int = 1500) -> Result:
    """Signatures and line ranges of a file (or, for a folder, the top-level names of each file)."""
    root, conn = _open(root)
    try:
        ensure(root)
        rel = _rel(root, path)
        target = root / rel if rel != "." else root
        if target.is_dir():
            prefix = "" if rel == "." else rel.rstrip("/") + "/"
            files = conn.execute("SELECT path, lines FROM ci_files WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
                                 (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
            lines = [f"{rel}/  ({len(files)} indexed files)"]
            for f in files:
                tops = conn.execute("SELECT name, kind FROM ci_symbols WHERE file=? AND depth=0 AND kind != 'variable' "
                                    "ORDER BY start_line LIMIT 9", (f["path"],)).fetchall()
                names = ", ".join(t["name"] for t in tops[:8]) + (", …" if len(tops) > 8 else "")
                lines.append(f"{f['path']} ({f['lines']} lines): {names}")
            return _result(root, "outline", lines, max_tokens, {"files": len(files)}, "narrow the folder or raise max_tokens")
        if not _check_file(root, conn, rel):
            raise CodeIntelError(f"{rel} is not an indexed source file (unknown, ignored, binary or too large); use read_file")
        f = conn.execute("SELECT * FROM ci_files WHERE path=?", (rel,)).fetchone()
        rows = conn.execute("SELECT * FROM ci_symbols WHERE file=? ORDER BY start_line, depth", (rel,)).fetchall()
        by_id = {r["id"]: r for r in rows}
        full_tokens = est_tokens("x" * int(f["size"]))
        lines = [f"{rel}  [{f['lang'] or 'text'}, {f['lines']} lines, ~{full_tokens} tokens if read whole]"]
        summary = summaries.summary_of(conn, rel)
        if summary:
            lines.append(f"summary: {summary}")
        shown = [r for r in rows if _visible(by_id, r)]
        lines += [_outline_line(r) for r in shown]
        if not shown:
            lines.append("(no declarations found)")
        return _result(root, "outline", lines, max_tokens, {"symbols": len(shown)},
                       "read_symbol(path:line) for a body, or outline a smaller scope")
    finally:
        conn.close()


# ---------------------------------------------------------------- find_symbol
_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _fts_words(query: str) -> list[str]:
    words: list[str] = []
    for w in _WORD.findall(query):
        words.append(w.lower())
        parts = [p.lower() for p in _CAMEL.split(w) if p]
        if len(parts) > 1:
            words.extend(parts)
        if "_" in w:
            words.extend(p.lower() for p in w.split("_") if len(p) > 1)
    return [w for w in dict.fromkeys(words) if len(w) > 1][:10]


def _symbol_search(conn: sqlite3.Connection, query: str, kind: str, limit: int) -> list[tuple[int, sqlite3.Row]]:
    """(rank, row) best first. rank 0 exact ... 4 fuzzy."""
    q = query.strip()
    found: dict[int, tuple[int, sqlite3.Row]] = {}
    kind_sql = " AND kind = ?" if kind else ""
    kargs = (kind,) if kind else ()

    def add(rank: int, rows) -> None:
        for r in rows:
            if r["id"] not in found or found[r["id"]][0] > rank:
                found[r["id"]] = (rank, r)

    base = "SELECT * FROM ci_symbols WHERE "
    if q:
        add(0, conn.execute(f"{base}(fq = ? OR qname = ? OR name = ?){kind_sql} LIMIT 200", (q, q, q, *kargs)))
        add(1, conn.execute(f"{base}(substr(fq, -(length(?) + 1)) = '.' || ?){kind_sql} LIMIT 200", (q, q, *kargs)))
        low = q.lower()
        add(2, conn.execute(f"{base}lower(name) LIKE ? ESCAPE '\\'{kind_sql} LIMIT 200",
                            (low.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", *kargs)))
        add(3, conn.execute(f"{base}lower(qname) LIKE ? ESCAPE '\\'{kind_sql} LIMIT 200",
                            ("%" + low.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", *kargs)))
        words = _fts_words(q)
        if words and len(found) < limit * 2:
            for mode in (" ", " OR "):
                match = mode.join(f'"{w}"*' for w in words)
                try:
                    rows = conn.execute(
                        "SELECT s.* FROM ci_symfts f JOIN ci_symbols s ON s.id = f.rowid WHERE ci_symfts MATCH ?"
                        f"{kind_sql.replace('kind', 's.kind')} ORDER BY bm25(ci_symfts, 6.0, 3.0, 2.0, 1.0) LIMIT 60",
                        (match, *kargs)).fetchall()
                except sqlite3.Error:
                    rows = []
                if rows:
                    add(4, rows)
                    break
    ranked = sorted(found.values(), key=lambda t: (t[0], 1 if indexer.is_test_path(t[1]["file"]) else 0, t[1]["depth"],
                                                   0 if t[1]["kind"] in CONTAINERS or t[1]["kind"] in ("function", "method") else 1,
                                                   len(t[1]["qname"]), t[1]["file"], t[1]["start_line"]))
    return ranked[: max(limit, 1)]


def find_symbol(root: Path, query: str, kind: str = "", limit: int = 15, max_tokens: int = 900) -> Result:
    """Symbols matching a name, a qualified name or a few words, best first."""
    root, conn = _open(root)
    try:
        ensure(root)
        if not (query or "").strip():
            raise CodeIntelError("a query is required")
        limit = max(1, min(int(limit or 15), 60))
        hits = _symbol_search(conn, query, kind or "", limit)
        if not hits:
            return _result(root, "find_symbol", [f"no symbol matches {query!r}" + (f" of kind {kind}" if kind else "")
                                                 + " - try code_search for text, or fewer/other words"], max_tokens, {"matches": []})
        lines = [f"{len(hits)} symbol(s) for {query!r}:"]
        for _rank, r in hits:
            sig = r["signature"] or r["name"]
            doc = f"  # {r['doc']}" if r["doc"] else ""
            lines.append(f"{_loc(r)} {r['kind']} {r['fq']}  {sig}{doc}")
        data = {"matches": [{"file": r["file"], "start": r["start_line"], "end": r["end_line"], "kind": r["kind"],
                             "qname": r["qname"], "fq": r["fq"], "rank": rank} for rank, r in hits]}
        return _result(root, "find_symbol", lines, max_tokens, data, "raise max_tokens or narrow with kind")
    finally:
        conn.close()


# ---------------------------------------------------------------- read_symbol
_PATH_LINE = re.compile(r"^(.+?):(\d+)(?:-\d+)?$")


def _innermost(conn: sqlite3.Connection, rel: str, line: int):
    return conn.execute("SELECT * FROM ci_symbols WHERE file=? AND start_line <= ? AND end_line >= ? "
                        "ORDER BY (end_line - start_line) ASC, depth DESC LIMIT 1", (rel, line, line)).fetchone()


def read_symbol(root: Path, target: str, context_lines: int = 0, max_tokens: int = 3000) -> Result:
    """The exact source of one symbol. `target` is a qualified name (`repo_map.build`, `Class.method`), a
    `path::qualified.name`, or `path:line` (the innermost symbol containing that line)."""
    root, conn = _open(root)
    try:
        ensure(root)
        t = (target or "").strip()
        if not t:
            raise CodeIntelError("a target is required: a qualified name or path:line")
        context_lines = max(0, min(int(context_lines or 0), 40))
        row = None
        m = _PATH_LINE.match(t.replace("\\", "/"))
        if "::" in t and (root / _rel(root, t.split("::", 1)[0])).is_file():
            rel, qn = t.replace("\\", "/").split("::", 1)
            rel = _rel(root, rel)
            _check_file(root, conn, rel)
            hits = conn.execute("SELECT * FROM ci_symbols WHERE file=? AND qname=? ORDER BY start_line", (rel, qn.replace("::", "."))).fetchall()
            if not hits:
                raise CodeIntelError(f"{rel} has no symbol {qn!r}; outline({rel!r}) lists them")
            row = hits[0]
        elif m and (root / _rel(root, m.group(1))).is_file():
            rel = _rel(root, m.group(1))
            line = int(m.group(2))
            if not _check_file(root, conn, rel):
                raise CodeIntelError(f"{rel} is not an indexed source file; use read_file with offset/limit")
            row = _innermost(conn, rel, line)
            if row is None:
                lines = _read_lines(root, rel)
                lo, hi = max(1, line - 12), min(len(lines), line + 12)
                body = [f"{rel}:{lo}-{hi}  (line {line} is outside any symbol; a window around it)"] + lines[lo - 1:hi]
                return _result(root, "read_symbol", body, max_tokens, {"file": rel, "start": lo, "end": hi})
        else:
            cands = _defs(conn, t)
            if not cands:
                near = _symbol_search(conn, t, "", 5)
                tip = ("; did you mean: " + ", ".join(f"{r['fq']} ({_loc(r)})" for _, r in near[:4])) if near else ""
                raise CodeIntelError(f"no symbol {t!r} in the index{tip}")
            if len(cands) > 1:
                non_test = [c for c in cands if not indexer.is_test_path(c["file"])]
                pick = non_test if len(non_test) == 1 else cands
                if len(pick) == 1:
                    cands = pick
            if len(cands) > 1:
                lines = [f"ambiguous: {len(cands)} symbols match {t!r}; call read_symbol again with path:line or path::qualified.name:"]
                lines += [f"{_loc(c)} {c['kind']} {c['fq']}  {c['signature']}" for c in cands[:20]]
                return _result(root, "read_symbol", lines, max_tokens, {"ambiguous": [_loc(c) for c in cands]})
            row = cands[0]
            _check_file(root, conn, row["file"])
            row = conn.execute("SELECT * FROM ci_symbols WHERE id=?", (row["id"],)).fetchone() or row
        rel = row["file"]
        lines = _read_lines(root, rel)
        lo, hi = max(1, row["start_line"] - context_lines), min(len(lines), row["end_line"] + context_lines)
        head = f"{rel}:{row['start_line']}-{row['end_line']} {row['kind']} {row['fq']}"
        body = lines[lo - 1:hi]
        budget_chars = int(max_tokens * CHARS_PER_TOKEN)
        if sum(len(x) + 1 for x in body) > budget_chars:
            if row["kind"] in CONTAINERS:
                kids = conn.execute("SELECT * FROM ci_symbols WHERE parent_id=? ORDER BY start_line", (row["id"],)).fetchall()
                out = [head + f"  (body is ~{est_tokens(chr(10).join(body))} tokens, over the {max_tokens} budget: members instead)"]
                out += [_outline_line({**dict(k), "depth": 1}) for k in kids]
                return _result(root, "read_symbol", out, max_tokens, {"file": rel, "start": row["start_line"], "end": row["end_line"], "members": len(kids)},
                               "read_symbol('path:line') of a member")
            shown, used = [], 0
            for x in body:
                if used + len(x) + 1 > budget_chars and shown:
                    break
                shown.append(x)
                used += len(x) + 1
            nxt = lo + len(shown)
            body = shown + [f"… {hi - nxt + 1} more line(s) of this symbol: read_file {rel} offset={nxt} limit={hi - nxt + 1}"]
            return _result(root, "read_symbol", [head] + body, max_tokens + 60,
                           {"file": rel, "start": row["start_line"], "end": row["end_line"]}, "")
        return _result(root, "read_symbol", [head] + body, max_tokens + 60,
                       {"file": rel, "start": row["start_line"], "end": row["end_line"], "kind": row["kind"], "fq": row["fq"]})
    finally:
        conn.close()


# ---------------------------------------------------------------- references / callers
def _linked_files(conn: sqlite3.Connection, def_files: set[str]) -> set[str]:
    """Files that are, or import, one of the files that define the name."""
    linked = set(def_files)
    for f in def_files:
        for r in conn.execute("SELECT DISTINCT file FROM ci_imports WHERE resolved = ?", (f,)):
            linked.add(r["file"])
    return linked


def _ref_rows(conn: sqlite3.Connection, name: str, kinds: tuple[str, ...]) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(kinds))
    return conn.execute(
        f"SELECT r.file, r.line, r.kind, r.sym_id, e.qname AS enclosing, e.kind AS ekind FROM ci_refs r "
        f"LEFT JOIN ci_symbols e ON e.id = r.sym_id WHERE r.name = ? AND r.kind IN ({marks}) ORDER BY r.file, r.line",
        (name, *kinds)).fetchall()


def _use_header(symbol: str, defs, what: str) -> list[str]:
    if not defs:
        return [f"{what} {symbol!r}: no definition in the index (an external or dynamic name); matches are by name only"]
    if len(defs) == 1:
        return [f"{what} {symbol!r}: defined at {_loc(defs[0])} ({defs[0]['kind']} {defs[0]['fq']})"]
    shown = "; ".join(f"{_loc(d)} {d['fq']}" for d in defs[:6]) + ("; …" if len(defs) > 6 else "")
    return [f"{what} {symbol!r}: AMBIGUOUS - {len(defs)} definitions ({shown}). Uses are attributed by import where possible; "
            "pass a qualified name (Class.method / module.name) to narrow."]


def references(root: Path, symbol: str, limit: int = 60, max_tokens: int = 1400) -> Result:
    """Every use of a name: import-linked uses first, then uses by name only. Best effort, not a type checker."""
    root, conn = _open(root)
    try:
        ensure(root)
        defs = _defs(conn, symbol)
        name = (defs[0]["name"] if defs else symbol.strip().rsplit(".", 1)[-1])
        def_files = {d["file"] for d in defs}
        linked = _linked_files(conn, def_files)
        rows = _ref_rows(conn, name, ("call", "name", "attr"))
        mine = [r for r in rows if not (r["file"] in def_files and False)]
        sure = [r for r in mine if r["file"] in linked]
        loose = [r for r in mine if r["file"] not in linked]
        lines = _use_header(symbol, defs, "references to")
        lines.append(f"{len(sure)} import-linked use(s), {len(loose)} by name only")

        def fmt(r) -> str:
            where = f" in {r['enclosing']}" if r["enclosing"] else " (module level)"
            return f"{r['file']}:{r['line']}{where} [{r['kind']}]"

        limit = max(1, min(int(limit or 60), 300))
        for r in sure[:limit]:
            lines.append(fmt(r))
        if loose:
            lines.append("by name only (not linked by an import to a definition):")
            lines += [fmt(r) for r in loose[: max(0, limit - len(sure))] or loose[:5]]
        data = {"definitions": [_loc(d) for d in defs], "linked": len(sure), "by_name": len(loose), "ambiguous": len(defs) > 1}
        return _result(root, "references", lines, max_tokens, data, "raise max_tokens/limit or qualify the name")
    finally:
        conn.close()


def callers(root: Path, symbol: str, limit: int = 40, max_tokens: int = 1200) -> Result:
    """The symbols that call a function/method/class, with the number of call sites in each."""
    root, conn = _open(root)
    try:
        ensure(root)
        defs = _defs(conn, symbol)
        name = (defs[0]["name"] if defs else symbol.strip().rsplit(".", 1)[-1])
        def_files = {d["file"] for d in defs}
        linked = _linked_files(conn, def_files)
        rows = _ref_rows(conn, name, ("call",))
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((r["file"], r["enclosing"] or "<module>"), []).append(r)
        ordered = sorted(groups.items(), key=lambda kv: (kv[0][0] not in linked, kv[0][0], kv[1][0]["line"]))
        sure = sum(1 for (f, _), _v in ordered if f in linked)
        lines = _use_header(symbol, defs, "callers of")
        lines.append(f"{len(ordered)} calling symbol(s): {sure} import-linked, {len(ordered) - sure} by name only")
        for (f, enc), rs in ordered[: max(1, min(int(limit or 40), 200))]:
            mark = "" if f in linked else " (name only)"
            lines.append(f"{f}:{rs[0]['line']} {enc}{mark}" + (f" x{len(rs)}" if len(rs) > 1 else ""))
        other = conn.execute("SELECT COUNT(*) FROM ci_refs WHERE name=? AND kind != 'call'", (name,)).fetchone()[0]
        if other:
            lines.append(f"({other} non-call reference(s) such as callbacks or annotations: references({symbol!r}))")
        data = {"definitions": [_loc(d) for d in defs], "callers": [{"file": f, "symbol": e, "sites": len(v), "linked": f in linked}
                                                                     for (f, e), v in ordered], "ambiguous": len(defs) > 1}
        return _result(root, "callers", lines, max_tokens, data, "raise max_tokens/limit")
    finally:
        conn.close()


# ---------------------------------------------------------------- related_tests
def _pytest_line(files: list[str]) -> str:
    py = [f for f in files if f.endswith(".py")]
    return ("pytest " + " ".join(py[:25])) if py else ""


def _tests_for_files(conn: sqlite3.Connection, files: list[str]) -> dict[str, dict]:
    """test_file -> {"weight": best, "how": set, "tests": set of test function names}."""
    out: dict[str, dict] = {}
    for f in files:
        for r in conn.execute("SELECT test_file, test_qname, how, weight FROM ci_testmap WHERE source_file = ?", (f,)):
            e = out.setdefault(r["test_file"], {"weight": 0, "how": set(), "tests": set()})
            e["weight"] = max(e["weight"], r["weight"])
            e["how"].add(r["how"])
            if r["test_qname"]:
                e["tests"].add(r["test_qname"])
    return out


def _format_tests(tests: dict[str, dict], per_file: int = 6) -> list[str]:
    lines = []
    for tf, e in sorted(tests.items(), key=lambda kv: (-kv[1]["weight"], kv[0])):
        names = sorted(e["tests"])
        shown = ", ".join(names[:per_file]) + (f", … (+{len(names) - per_file})" if len(names) > per_file else "")
        lines.append(f"{tf}  [{'+'.join(sorted(e['how']))}]" + (f"  {shown}" if shown else ""))
    return lines


def related_tests(root: Path, target: str, max_tokens: int = 700) -> Result:
    """Tests for a source file or a symbol: by import, by reference, by naming convention, and by coverage when
    coverage.json (with contexts) is present."""
    root, conn = _open(root)
    try:
        ensure(root)
        t = (target or "").strip().replace("\\", "/")
        if not t:
            raise CodeIntelError("a path or symbol is required")
        if (root / t).is_file() or conn.execute("SELECT 1 FROM ci_files WHERE path=?", (t,)).fetchone():
            rel = _rel(root, t)
            _check_file(root, conn, rel)
            if _is_test(conn, rel):
                srcs = [r["source_file"] for r in conn.execute("SELECT DISTINCT source_file FROM ci_testmap WHERE test_file=? ORDER BY source_file", (rel,))]
                lines = [f"{rel} is itself a test file; it exercises: " + (", ".join(srcs[:15]) or "(nothing resolvable)")]
                return _result(root, "related_tests", lines, max_tokens, {"test_file": True, "sources": srcs})
            tests = _tests_for_files(conn, [rel])
            lines = [f"tests for {rel}: {len(tests)} file(s)"] if tests else [f"no tests found for {rel} (no test file imports it or is named for it)"]
            lines += _format_tests(tests)
            cmd = _pytest_line(sorted(tests, key=lambda k: -tests[k]["weight"]))
            if cmd:
                lines.append("run: " + cmd)
            return _result(root, "related_tests", lines, max_tokens, {"tests": sorted(tests)}, "raise max_tokens")
        defs = _defs(conn, t)
        name = defs[0]["name"] if defs else t.rsplit(".", 1)[-1]
        def_files = {d["file"] for d in defs}
        linked = _linked_files(conn, def_files)
        rows = conn.execute(
            "SELECT r.file, e.qname FROM ci_refs r JOIN ci_files f ON f.path = r.file AND f.is_test = 1 "
            "LEFT JOIN ci_symbols e ON e.id = r.sym_id WHERE r.name = ?", (name,)).fetchall()
        tests: dict[str, dict] = {}
        for r in rows:
            e = tests.setdefault(r["file"], {"weight": 4 if r["file"] in linked else 2, "how": {"ref" if r["file"] in linked else "name-only"}, "tests": set()})
            if r["qname"]:
                e["tests"].add(r["qname"])
        file_level = {tf: e for tf, e in _tests_for_files(conn, sorted(def_files)).items()
                      if tf not in tests and e["weight"] >= 3}
        lines = _use_header(t, defs, "tests for")
        lines.append(f"{len(tests)} test file(s) that use the name" + (f"; {len(file_level)} more import its module" if file_level else ""))
        lines += _format_tests(tests)
        if file_level and not tests:
            lines.append("importing the defining module (no test mentions the name itself):")
            lines += _format_tests(file_level)[:6]
        cmd = _pytest_line(sorted(tests, key=lambda k: -tests[k]["weight"]) or sorted(file_level)[:6])
        if cmd:
            lines.append("run: " + cmd)
        return _result(root, "related_tests", lines, max_tokens, {"tests": sorted(tests), "ambiguous": len(defs) > 1})
    finally:
        conn.close()


# ---------------------------------------------------------------- impact
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_changes(changes: str) -> dict[str, list[tuple[int, int]]]:
    """file -> changed line ranges (new side). A plain list of paths gives the whole file (an empty range list)."""
    out: dict[str, list[tuple[int, int]]] = {}
    text = changes or ""
    if "\n@@" in "\n" + text or text.lstrip().startswith(("diff --git", "--- ")):
        cur = ""
        for line in text.splitlines():
            if line.startswith("+++ "):
                p = line[4:].strip().split("\t")[0]
                cur = "" if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
                if cur:
                    out.setdefault(cur, [])
            elif line.startswith("--- ") and not cur:
                continue
            elif line.startswith("diff --git"):
                parts = line.split()
                cur = parts[-1][2:] if len(parts) >= 4 and parts[-1].startswith("b/") else ""
                if cur:
                    out.setdefault(cur, [])
            elif cur and (m := _HUNK.match(line)):
                start = int(m.group(3))
                count = int(m.group(4)) if m.group(4) is not None else 1
                out[cur].append((start, start + max(count, 1) - 1))
        return out
    for token in re.split(r"[\n,;]+|\s{2,}", text):
        token = token.strip().strip("'\"")
        if token:
            out.setdefault(token.replace("\\", "/"), [])
    if len(out) == 1 and " " in next(iter(out)):
        out = {p: [] for p in next(iter(out)).split()}
    return out


def impact(root: Path, changes: str, max_tokens: int = 2200) -> Result:
    """From a unified diff (or paths): the symbols changed, what calls them, and the tests to run."""
    root, conn = _open(root)
    try:
        ensure(root)
        spec = parse_changes(changes)
        if not spec:
            raise CodeIntelError("pass a unified diff (git diff output) or a list of changed paths")
        changed_syms: list[sqlite3.Row] = []
        lines: list[str] = []
        files: list[str] = []
        unknown: list[str] = []
        for rel, ranges in spec.items():
            try:
                rel = _rel(root, rel)
            except CodeIntelError:
                unknown.append(rel)
                continue
            if not _check_file(root, conn, rel):
                unknown.append(rel)
                continue
            files.append(rel)
            if ranges:
                hit: dict[int, sqlite3.Row] = {}
                for lo, hi in ranges:
                    for r in conn.execute("SELECT * FROM ci_symbols WHERE file=? AND start_line <= ? AND end_line >= ?", (rel, hi, lo)):
                        hit[r["id"]] = r
                # innermost only: drop a symbol that has an overlapping child in the hit set
                parents = {r["parent_id"] for r in hit.values() if r["parent_id"] is not None}
                changed_syms += [r for r in hit.values() if r["id"] not in parents]
            else:
                changed_syms += conn.execute("SELECT * FROM ci_symbols WHERE file=? AND depth=0 AND kind != 'variable' ORDER BY start_line", (rel,)).fetchall()
        lines.append(f"impact of changes in {len(files)} file(s)" + (f" (not indexed: {', '.join(unknown[:5])})" if unknown else ""))
        lines.append(f"changed symbols ({len(changed_syms)}):")
        for r in changed_syms[:25]:
            lines.append(f"  {_loc(r)} {r['kind']} {r['fq']}")
        if len(changed_syms) > 25:
            lines.append(f"  … +{len(changed_syms) - 25} more")
        if not changed_syms and files:
            lines.append("  (changes sit outside any declaration - module-level code)")
        # callers of the changed callables
        lines.append("callers:")
        affected_files: set[str] = set()
        any_callers = False
        for r in [s for s in changed_syms if s["kind"] in ("function", "method", "class")][:20]:
            rows = _ref_rows(conn, r["name"], ("call",))
            def_linked = _linked_files(conn, {r["file"]})
            seen: dict[str, int] = {}
            for x in rows:
                if x["file"] not in def_linked or (x["file"] == r["file"] and x["enclosing"] == r["qname"]):
                    continue
                seen[f"{x['file']}:{x['line']} {x['enclosing'] or '<module>'}"] = 1
                affected_files.add(x["file"])
            if seen:
                any_callers = True
                names = list(seen)
                lines.append(f"  {r['qname']} <- " + "; ".join(names[:6]) + (f"; … +{len(names) - 6}" if len(names) > 6 else ""))
        if not any_callers:
            lines.append("  (none found)")
        importers = set()
        for f in files:
            for x in conn.execute("SELECT DISTINCT file FROM ci_imports WHERE resolved=?", (f,)):
                importers.add(x["file"])
        if importers:
            shown = sorted(importers)
            lines.append(f"imported by {len(shown)} file(s): " + ", ".join(shown[:10]) + (", …" if len(shown) > 10 else ""))
        # tests: those for the changed files, plus those that reference a changed name
        tests = _tests_for_files(conn, files)
        names = sorted({s["name"] for s in changed_syms if len(s["name"]) > 2})[:40]
        for nm in names:
            for x in conn.execute("SELECT r.file, e.qname FROM ci_refs r JOIN ci_files f ON f.path=r.file AND f.is_test=1 "
                                  "LEFT JOIN ci_symbols e ON e.id=r.sym_id WHERE r.name=?", (nm,)):
                e = tests.setdefault(x["file"], {"weight": 2, "how": set(), "tests": set()})
                e["how"].add("ref")
                if x["qname"]:
                    e["tests"].add(x["qname"])
        for f in files:
            if _is_test(conn, f):
                tests.setdefault(f, {"weight": 5, "how": {"changed"}, "tests": set()})
        lines.append(f"tests to run ({len(tests)} file(s)):")
        lines += ["  " + t for t in _format_tests(tests, per_file=4)[:30]]
        cmd = _pytest_line(sorted(tests, key=lambda k: -tests[k]["weight"]))
        if cmd:
            lines.append("run: " + cmd)
        data = {"files": files, "symbols": [r["fq"] for r in changed_syms], "tests": sorted(tests),
                "affected_files": sorted(affected_files | importers)}
        return _result(root, "impact", lines, max_tokens, data, "raise max_tokens or pass fewer files")
    finally:
        conn.close()


# ---------------------------------------------------------------- context_pack
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./][A-Za-z_][A-Za-z0-9_]*)*(?:\.[a-z]{1,4})?")
_STOP = frozenset("the a an and or of to in on for with from this that is are be it as at by how what where which who why when "
                  "do does did can should would add fix make use used using into out not no yes if then else we i you our their "
                  "code file files function functions class method methods test tests change changes update new".split())


def _rules_for(task: str, max_tokens: int) -> list[str]:
    """Rules, decisions and preferences from the context vault (bot/context_vault), when this install has one and it
    is on: `get_vault().recall(query, project, kinds, k, budget_tokens)`. Optional: no vault, nothing to add."""
    try:
        from bot import context_vault           # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return []
    get_vault = getattr(context_vault, "get_vault", None)
    try:
        vault = get_vault() if callable(get_vault) else None
        if vault is None:
            return []
        items = vault.recall(task, None, ["rule", "decision", "preference"], 8, max(64, int(max_tokens)))
    except Exception:  # noqa: BLE001 - rules are a bonus
        return []
    lines = []
    for item in items or []:
        text = " ".join(str((item or {}).get("text") or "").split())
        if text:
            lines.append(f"- [{item.get('kind', 'note')}] {text}")
    return lines


def _task_terms(task: str) -> tuple[list[str], list[str], list[str]]:
    """(identifiers, paths, plain words) mentioned by the task."""
    idents, paths, words = [], [], []
    for tok in _IDENT.findall(task):
        low = tok.lower()
        if "/" in tok or re.search(r"\.(py|ts|tsx|js|jsx|rs|go|java|kt|cs|rb|php|swift)$", tok):
            paths.append(tok)
        elif "_" in tok or "." in tok or _CAMEL.search(tok) or tok[:1].isupper():
            idents.append(tok)
            words.extend(_fts_words(tok))
        elif low not in _STOP and len(tok) > 2:
            words.append(low)
    return list(dict.fromkeys(idents)), list(dict.fromkeys(paths)), list(dict.fromkeys(words))


def context_pack(root: Path, task: str, budget_tokens: int = 4000) -> Result:
    """A ranked, budgeted bundle for a task: the relevant signatures, the top bodies, tests to know about, and rules."""
    budget_tokens = max(300, min(int(budget_tokens or 4000), 40000))
    root, conn = _open(root)
    try:
        ensure(root, chunks=True)
        if not (task or "").strip():
            raise CodeIntelError("describe the task in a sentence or two")
        wants_tests = bool(re.search(r"\btests?\b|pytest|spec", task, re.I))
        idents, paths, words = _task_terms(task)
        score: dict[int, float] = {}
        rows: dict[int, sqlite3.Row] = {}

        def bump(row, pts: float) -> None:
            score[row["id"]] = score.get(row["id"], 0.0) + pts
            rows[row["id"]] = row

        for ident in idents:
            for rank, r in _symbol_search(conn, ident, "", 8):
                bump(r, 12.0 / (1 + rank) if rank < 4 else 2.0)
        for p in paths:
            rel = p.replace("\\", "/")
            cand = [r["path"] for r in conn.execute("SELECT path FROM ci_files WHERE path = ? OR path LIKE ? ESCAPE '\\' LIMIT 4",
                                                    (rel, "%/" + rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")))]
            for f in cand:
                for r in conn.execute("SELECT * FROM ci_symbols WHERE file=? AND depth=0 AND kind != 'variable'", (f,)):
                    bump(r, 6.0)
        if words:
            for mode in (" ", " OR "):
                match = mode.join(f'"{w}"*' for w in words[:10])
                try:
                    found = conn.execute("SELECT s.* FROM ci_symfts f JOIN ci_symbols s ON s.id=f.rowid WHERE ci_symfts MATCH ? "
                                         "ORDER BY bm25(ci_symfts, 6.0, 3.0, 2.0, 1.0) LIMIT 40", (match,)).fetchall()
                except sqlite3.Error:
                    found = []
                if found:
                    for i, r in enumerate(found):
                        bump(r, 5.0 / math.sqrt(i + 1))
                    break
        # text chunks (code_search's FTS index): the symbols that contain the best-matching chunks
        try:
            from bot.agent_runtime import search_index

            query = " ".join(words + [w for i in idents for w in _fts_words(i)]) or task
            for i, c in enumerate(search_index._rank_query(conn, "chunks", "path, line, body", query, 16)):
                mid = int(c["line"]) + 4
                r = _innermost(conn, c["path"], mid)
                if r is not None:
                    bump(r, 3.0 / math.sqrt(i + 1))
        except Exception:  # noqa: BLE001 - a missing chunk index only loses this signal
            pass
        # optional semantic signal (ModelMistress embeddings through code_search), off unless configured
        try:
            from bot.config import config

            semantic = bool(((config.current.get("native_agent") or {}).get("codeintel") or {}).get("semantic", False))
        except Exception:  # noqa: BLE001
            semantic = False
        if semantic:
            try:
                from bot.agent_runtime import search_index

                hits, _mode = search_index.search_code_ex(root, task, 8)
                for i, h in enumerate(hits):
                    r = _innermost(conn, h["path"], int(h.get("line") or 1) + 3)
                    if r is not None:
                        bump(r, 4.0 / math.sqrt(i + 1))
            except Exception:  # noqa: BLE001
                pass
        if not score:
            return _result(root, "context_pack", ["nothing in the index matches this task; try code_search or name a file/symbol"], budget_tokens)
        # re-rank: centrality, tests, variables
        ranked = []
        for sid, pts in score.items():
            r = rows[sid]
            used_in = conn.execute("SELECT COUNT(DISTINCT file) FROM ci_refs WHERE name=?", (r["name"],)).fetchone()[0] if len(r["name"]) > 2 else 0
            pts += 0.6 * math.log1p(used_in)
            if indexer.is_test_path(r["file"]) and not wants_tests:
                pts *= 0.4
            if r["kind"] == "variable":
                pts *= 0.5
            if r["depth"] >= 1 and r["kind"] not in CONTAINERS:
                pts *= 1.05
            ranked.append((pts, r))
        ranked.sort(key=lambda t: (-t[0], t[1]["file"], t[1]["start_line"]))
        top = ranked[:40]

        sig_budget = int(budget_tokens * 0.22)
        body_budget = int(budget_tokens * 0.55)
        test_budget = int(budget_tokens * 0.10)
        rule_budget = int(budget_tokens * 0.13)
        out: list[str] = [f"context pack for: {task.strip()[:140]}"]

        # signatures, grouped by file in rank order
        order: list[str] = []
        per_file: dict[str, list[sqlite3.Row]] = {}
        for _pts, r in top[:28]:
            if r["file"] not in per_file:
                order.append(r["file"])
            per_file.setdefault(r["file"], []).append(r)
        sig_lines = ["## signatures"]
        for f in order:
            sig_lines.append(f)
            for r in sorted(per_file[f], key=lambda x: x["start_line"]):
                sig_lines.append("  " + _outline_line({**dict(r), "depth": 0}))
        sig_text = _fit_lines(sig_lines, sig_budget)
        out.append(sig_text)

        # bodies: leaf-most, highest ranked first, each capped
        body_lines = ["## code"]
        used = est_tokens("## code")
        shown_bodies = 0
        per_body_cap = max(120, int(body_budget * 0.45))
        for _pts, r in top[:14]:
            if r["kind"] in CONTAINERS and (r["end_line"] - r["start_line"]) > 60:
                continue                       # a big class: its signatures above are the useful part
            lines = _read_lines(root, r["file"])[r["start_line"] - 1:r["end_line"]]
            text = "\n".join(lines)
            tok = est_tokens(text)
            note = ""
            if tok > per_body_cap:
                keep = max(8, int(len(lines) * per_body_cap / tok))
                text = "\n".join(lines[:keep])
                note = f"\n… {len(lines) - keep} more line(s): read_symbol({r['file']}:{r['start_line']})"
                tok = est_tokens(text + note)
            if used + tok + 12 > body_budget and shown_bodies:
                continue
            if used + tok + 12 > body_budget:
                continue
            body_lines.append(f"### {_loc(r)} {r['fq']}")
            body_lines.append(text + note)
            used += tok + 12
            shown_bodies += 1
        out.append("\n".join(body_lines) if shown_bodies else "## code\n(no body fits the budget: use read_symbol on the signatures above)")

        # tests
        top_files = list(dict.fromkeys(r["file"] for _p, r in top[:6] if not indexer.is_test_path(r["file"])))
        tests = _tests_for_files(conn, top_files)
        if tests:
            tl = ["## related tests"] + _format_tests(tests, per_file=3)[:8]
            cmd = _pytest_line(sorted(tests, key=lambda k: -tests[k]["weight"])[:8])
            if cmd:
                tl.append("run: " + cmd)
            out.append(_fit_lines(tl, test_budget))
        rules = _rules_for(task, rule_budget)
        if rules:
            out.append(_fit_lines(["## rules and decisions"] + rules, rule_budget))
        text = "\n".join(out)
        truncated = False
        cap = int(budget_tokens * CHARS_PER_TOKEN)
        if len(text) > cap:
            text = text[: cap - 40].rsplit("\n", 1)[0] + "\n… (cut to the budget)"
            truncated = True
        res = Result(text, est_tokens(text), {"symbols": [r["fq"] for _p, r in top[:14]], "files": order}, truncated)
        _record(root, "context_pack", res.tokens)
        return res
    finally:
        conn.close()


def _fit_lines(lines: list[str], budget_tokens: int) -> str:
    cap = int(budget_tokens * CHARS_PER_TOKEN)
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > cap and len(out) > 1:
            out.append(f"… {len(lines) - i} more line(s)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------- stats / usage
def catalog_report(root: Path, folder: str = "", reason: str = "", max_tokens: int = 2500) -> Result:
    """Every listed file under `folder`: counts by status, per entry, the files only catalogued and why, broken links."""
    root, conn = _open(root)
    try:
        ensure(root)
        prefix = "" if folder in ("", ".") else _rel(root, folder)
        lines, data = catalog.report(conn, root, prefix, reason)
        return _result(root, "catalog", lines, max_tokens, data, "narrow the folder, filter with reason, or raise max_tokens")
    finally:
        conn.close()


def usage(root: Path) -> dict[str, dict]:
    root, conn = _open(root)
    try:
        return {r["tool"]: {"calls": r["calls"], "tokens": r["tokens"]} for r in conn.execute("SELECT * FROM ci_usage ORDER BY tool")}
    finally:
        conn.close()


def stats(root: Path) -> dict:
    root, conn = _open(root)
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("ci_files", "ci_symbols", "ci_refs", "ci_imports", "ci_testmap")}
        parsers_used = {r["parser"]: r["n"] for r in conn.execute("SELECT parser, COUNT(*) AS n FROM ci_files GROUP BY parser")}
        langs = {r["lang"] or "?": r["n"] for r in conn.execute("SELECT lang, COUNT(*) AS n FROM ci_files GROUP BY lang ORDER BY n DESC")}
        updated = store.meta(conn, "updated")
        chunks = 0
        try:
            chunks = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        except sqlite3.Error:
            pass
        db = store.db_path(root)
        return {"root": str(root), "db": str(db), "db_bytes": db.stat().st_size if db.exists() else 0, **counts,
                "parsers": parsers_used, "languages": langs, "text_files_chunked": chunks,
                "updated": float(updated) if updated else None,
                "usage": {r["tool"]: {"calls": r["calls"], "tokens": r["tokens"]} for r in conn.execute("SELECT * FROM ci_usage ORDER BY tool")},
                "process_usage": {k: {"calls": v[0], "tokens": v[1]} for k, v in sorted(_usage.items())}}
    finally:
        conn.close()
