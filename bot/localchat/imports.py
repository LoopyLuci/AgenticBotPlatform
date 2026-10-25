"""Import chats and other data from Hermes Agent and OpenCode into the Agentic Workspace.

Sessions and their messages land in Local Chat (bot/localchat/store.py), marked by `source` ("hermes" | "opencode") and filed
under a group per source, so they show in the Workspace's session list like any other. Everything is keyed by the source's own
ids: a session's id is a hash of "<source>:<its id>" and each message carries `ext_id` "<source>:<its id>", so a re-run adds only
what is new (and refreshes a message the source changed, as OpenCode does while a reply streams) and never duplicates. A cursor
per source makes the next run read only what changed since; `full` re-reads everything (still without duplicates).

Memories, skills and model / provider settings go to an import library in the agent state folder (data/agent/imports/<source>/):
memories as entries, skills as their files, settings as a summary with every key, token, secret or password left out. None of it
becomes active by itself: skills are not added to bots' prompts and providers are not added to ABP's provider config.

The sources are only ever read: each database is opened read-only (mode=ro) and, when it is locked, copied with SQLite's backup
API into the state folder and read there. Request dumps and auth files are never touched.

Settings (config local_chat.import, every one switchable in the Workspace's Import panel): <source>.enabled, <source>.path
("" = found automatically), <source>.<kind> for each of KINDS, and include_tools (tool calls and their output as system
messages, cut to TOOL_CHARS; off by default: Hermes alone has ~150k of them).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

SOURCES = ("hermes", "opencode")
KINDS = ("sessions", "memories", "skills", "config")
LABEL = {"hermes": "Hermes", "opencode": "OpenCode"}
TOOL_CHARS = 4000
SKILL_CHARS = 200_000
_SECRET = re.compile(r"(key|token|secret|password|passwd|auth|credential|cookie|bearer)", re.I)

_lock = threading.Lock()
_job: dict[str, Any] = {"running": False}
_cancel = threading.Event()


# ---- settings --------------------------------------------------------------------------------------------------------
def settings() -> dict[str, Any]:
    try:
        from bot.config import config

        got = (config.current.get("local_chat") or {}).get("import") or {}
    except Exception:  # noqa: BLE001 - config trouble must not stop the panel
        got = {}
    got = got if isinstance(got, dict) else {}
    out: dict[str, Any] = {"include_tools": bool(got.get("include_tools", False))}
    for src in SOURCES:
        s = got.get(src) if isinstance(got.get(src), dict) else {}
        out[src] = {"enabled": bool(s.get("enabled", True)), "path": str(s.get("path") or ""),
                    **{k: bool(s.get(k, True)) for k in KINDS}}
    return out


def setting_changes(payload: dict) -> dict[tuple[str, ...], Any]:
    """Validate a settings change from the panel into config key paths. Raises ValueError on anything unknown."""
    changes: dict[tuple[str, ...], Any] = {}
    for key, val in (payload or {}).items():
        if key == "include_tools":
            if not isinstance(val, bool):
                raise ValueError("include_tools must be true or false")
            changes[("local_chat", "import", "include_tools")] = val
        elif key in SOURCES and isinstance(val, dict):
            for k, v in val.items():
                if k == "path":
                    changes[("local_chat", "import", key, "path")] = str(v or "").strip()[:500]
                elif k in ("enabled", *KINDS):
                    if not isinstance(v, bool):
                        raise ValueError(f"{key}.{k} must be true or false")
                    changes[("local_chat", "import", key, k)] = v
                else:
                    raise ValueError(f"unknown setting {key}.{k}")
        else:
            raise ValueError(f"unknown setting {key}")
    if not changes:
        raise ValueError("nothing to change")
    return changes


# ---- where the sources are -------------------------------------------------------------------------------------------
def hermes_home(cfg: Optional[dict] = None) -> Path:
    p = ((cfg or settings())["hermes"]["path"] or "").strip()
    if p:
        return Path(p).expanduser()
    from bot.hermes_config import _default_hermes_home

    return _default_hermes_home()


def opencode_db(cfg: Optional[dict] = None) -> Path:
    p = ((cfg or settings())["opencode"]["path"] or "").strip()
    if p:
        p = Path(p).expanduser()
        return p / "opencode.db" if p.is_dir() else p
    for base in filter(None, [os.environ.get("XDG_DATA_HOME"), str(Path.home() / ".local" / "share")]):
        cand = Path(base) / "opencode" / "opencode.db"
        if cand.is_file():
            return cand
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def opencode_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opencode"


def library_dir(source: str) -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("imports", source)


@contextlib.contextmanager
def _read(db: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a source database; when it cannot be read in place (locked), a backup copy in the state folder."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("select count(*) from sqlite_master").fetchone()
    except sqlite3.OperationalError:
        con.close()
        copy = library_dir("_copies") / (hashlib.sha1(str(db).encode()).hexdigest()[:12] + ".db")
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(copy))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        con = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


# ---- ids and the Local Chat side -------------------------------------------------------------------------------------
def session_id(source: str, ext: str) -> str:
    return source[:2] + hashlib.sha1(f"{source}:{ext}".encode()).hexdigest()[:14]


def group_id(source: str) -> str:
    return "imp-" + source


def _target() -> sqlite3.Connection:
    from bot.localchat import store

    with store._db():                       # creates and migrates the schema (source / ext_id columns)
        pass
    con = sqlite3.connect(str(store.db_path()), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_ext ON messages(session_id, ext_id) WHERE ext_id != ''")
    return con


def _ensure_group(con: sqlite3.Connection, source: str) -> None:
    if not con.execute("select 1 from groups where id=?", (group_id(source),)).fetchone():
        con.execute("insert into groups (id, name, created, position) values (?,?,?,?)",
                    (group_id(source), f"Imported: {LABEL[source]}", time.time(), 900))


def _upsert_session(con: sqlite3.Connection, source: str, s: dict) -> tuple[str, bool]:
    """Insert the session when new; on a re-run refresh only what the import owns (updated, an empty title, the model) so the
    owner's own changes (title, pin, group, archive) stay."""
    sid = session_id(source, s["ext"])
    title = (s.get("title") or "").strip()[:500]
    row = con.execute("select title, updated from sessions where id=?", (sid,)).fetchone()
    if row is None:
        con.execute("insert into sessions (id, title, created, updated, model, mode, autonomy, workspace, cloud_assist, archived, "
                    "status, group_id, parent_id, link, source) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, title, s["created"], s["updated"], (s.get("model") or "")[:500], "chat", 0, "", "off",
                     1 if s.get("archived") else 0, "done", group_id(source),
                     session_id(source, s["parent"]) if s.get("parent") else "", "task" if s.get("parent") else "", source))
        return sid, True
    con.execute("update sessions set updated=max(updated, ?), title=case when title='' then ? else title end where id=?",
                (s["updated"], title, sid))
    return sid, False


def _upsert_messages(con: sqlite3.Connection, sid: str, rows: list[dict]) -> tuple[int, int]:
    """rows: {ext, role, text, created, model?, tokens?, meta?, reverted?}, oldest first. Returns (added, updated)."""
    if not rows:
        return 0, 0
    have = {r[0]: r[1] for r in con.execute("select ext_id, text from messages where session_id=? and ext_id != ''", (sid,))}
    added = updated = 0
    for m in rows:
        text = m["text"]
        if m["ext"] in have:
            if have[m["ext"]] != text:
                con.execute("update messages set text=? where session_id=? and ext_id=?", (text, sid, m["ext"]))
                updated += 1
            continue
        con.execute("insert into messages (session_id, role, text, created, model, tokens, meta, reverted, ext_id) "
                    "values (?,?,?,?,?,?,?,?,?)",
                    (sid, m["role"], text, m["created"], (m.get("model") or "")[:200], int(m.get("tokens") or 0),
                     json.dumps(m.get("meta") or {}, ensure_ascii=False), 1 if m.get("reverted") else 0, m["ext"]))
        have[m["ext"]] = text
        added += 1
    return added, updated


def imported_counts() -> dict[str, dict[str, int]]:
    con = _target()
    try:
        out = {}
        for src in SOURCES:
            s = con.execute("select count(*) from sessions where source=?", (src,)).fetchone()[0]
            m = con.execute("select count(*) from messages where ext_id like ?", (src + ":%",)).fetchone()[0]
            out[src] = {"sessions": s, "messages": m}
        return out
    finally:
        con.close()


# ---- Hermes ----------------------------------------------------------------------------------------------------------
def _tool_names(raw: Any) -> list[str]:
    try:
        calls = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        return []
    out = []
    for c in calls if isinstance(calls, list) else []:
        fn = (c or {}).get("function") or c or {}
        if fn.get("name"):
            out.append(str(fn["name"]))
    return out


def _hermes_sessions(home: Path, cursor: int, include_tools: bool, emit: Callable[[str, dict, list], None],
                     stop: Callable[[], bool]) -> int:
    """Read Hermes's state.db from message id `cursor` on; emit(session_ext, session, messages) per session touched.
    Returns the newest message id read."""
    db = home / "state.db"
    newest = cursor
    with _read(db) as con:
        cols = {r[1] for r in con.execute("pragma table_info(sessions)")}
        pick = [c for c in ("id", "source", "title", "display_name", "model", "parent_session_id", "started_at", "ended_at",
                            "last_activity_at", "archived") if c in cols]
        sessions = {r["id"]: dict(r) for r in con.execute(f"select {', '.join(pick)} from sessions")}
        mcols = {r[1] for r in con.execute("pragma table_info(messages)")}
        active = "active" if "active" in mcols else "1 as active"
        touched = [r[0] for r in con.execute("select distinct session_id from messages where id > ?", (cursor,))]
        for ext in touched:
            if stop():
                break
            s = sessions.get(ext) or {"id": ext}
            rows, first_user = [], ""
            for m in con.execute(f"select id, role, content, tool_calls, tool_name, timestamp, token_count, {active} "
                                 "from messages where session_id=? and id > ? order by id", (ext, cursor)):
                newest = max(newest, int(m["id"]))
                role, content, ts = m["role"], m["content"] or "", float(m["timestamp"] or 0)
                base = {"ext": f"hermes:{m['id']}", "created": ts, "reverted": not m["active"]}
                if role == "user" and content:
                    first_user = first_user or content
                    rows.append({**base, "role": "user", "text": content})
                elif role == "assistant":
                    tools = _tool_names(m["tool_calls"])
                    if content:
                        rows.append({**base, "role": "assistant", "text": content, "model": s.get("model") or "",
                                     "tokens": m["token_count"] or 0, "meta": {"source": "hermes", "tools": tools} if tools else {"source": "hermes"}})
                    elif tools and include_tools:
                        rows.append({**base, "role": "system", "text": "Tool calls: " + ", ".join(tools), "meta": {"source": "hermes", "tools": tools}})
                elif role == "tool" and include_tools:
                    rows.append({**base, "role": "system", "text": f"[{m['tool_name'] or 'tool'} result]\n{content[:TOOL_CHARS]}",
                                 "meta": {"source": "hermes", "tool": m["tool_name"] or ""}})
            started = float(s.get("started_at") or (rows[0]["created"] if rows else time.time()))
            title = s.get("title") or s.get("display_name") or (first_user.strip().splitlines()[0][:80] if first_user else "") \
                or f"Hermes {s.get('source') or ''} session".replace("  ", " ")
            emit(ext, {"ext": ext, "title": title, "created": started,
                       "updated": float(s.get("last_activity_at") or s.get("ended_at") or (rows[-1]["created"] if rows else started)),
                       "model": s.get("model") or "", "archived": bool(s.get("archived")),
                       "parent": s.get("parent_session_id") or ""}, rows)
    return newest


def _hermes_counts(home: Path) -> dict[str, int]:
    with _read(home / "state.db") as con:
        return {"sessions": con.execute("select count(*) from sessions").fetchone()[0],
                "messages": con.execute("select count(*) from messages where role in ('user','assistant')").fetchone()[0],
                "max_id": con.execute("select coalesce(max(id),0) from messages").fetchone()[0]}


def _hermes_memories(home: Path) -> list[dict]:
    out = []
    for name in ("MEMORY.md", "USER.md"):
        p = home / "memories" / name
        if p.is_file():
            for part in p.read_text(encoding="utf-8", errors="replace").split("§"):
                part = part.strip()
                if part:
                    out.append({"file": name, "text": part})
    return out


def _skill_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*/SKILL.md") if p.is_file()) if root.is_dir() else []


# ---- OpenCode --------------------------------------------------------------------------------------------------------
def _opencode_sessions(db: Path, cursor: float, include_tools: bool, emit: Callable[[str, dict, list], None],
                       stop: Callable[[], bool]) -> float:
    newest = cursor
    with _read(db) as con:
        sessions = {r["id"]: dict(r) for r in con.execute(
            "select id, parent_id, title, time_created, time_updated, time_archived from session")}
        touched = {r[0] for r in con.execute("select session_id from message where time_updated > ?", (cursor,))}
        touched |= {r[0] for r in con.execute("select session_id from part where time_updated > ?", (cursor,))}
        for ext in sorted(touched, key=lambda e: (sessions.get(e) or {}).get("time_created") or 0):
            if stop():
                break
            s = sessions.get(ext) or {"id": ext}
            msgs = {r["id"]: r for r in con.execute(
                "select id, time_created, time_updated, data from message where session_id=? order by time_created, id", (ext,))}
            parts: dict[str, list] = {}
            for p in con.execute("select id, message_id, time_created, time_updated, data from part where session_id=? "
                                 "order by time_created, id", (ext,)):
                newest = max(newest, float(p["time_updated"] or 0))
                parts.setdefault(p["message_id"], []).append(p)
            rows = []
            for mid, m in msgs.items():
                newest = max(newest, float(m["time_updated"] or 0))
                try:
                    d = json.loads(m["data"]) or {}
                except ValueError:
                    d = {}
                role = "user" if d.get("role") == "user" else "assistant"
                model = d.get("modelID") or ((d.get("model") or {}).get("modelID") if isinstance(d.get("model"), dict) else d.get("model")) or ""
                created = float(m["time_created"] or 0) / 1000
                texts, tokens = [], 0
                for p in parts.get(mid, []):
                    try:
                        pd = json.loads(p["data"]) or {}
                    except ValueError:
                        continue
                    t = pd.get("type")
                    if t == "text" and pd.get("text") and not pd.get("synthetic"):
                        texts.append(pd["text"])
                    elif t == "step-finish":
                        tok = pd.get("tokens") or {}
                        tokens += int(tok.get("total") or sum(int(tok.get(k) or 0) for k in ("input", "output", "reasoning")))
                    elif t == "tool" and include_tools:
                        st = pd.get("state") or {}
                        out = str(st.get("output") or st.get("error") or "")
                        rows.append({"ext": f"opencode:{p['id']}", "role": "system", "created": float(p["time_created"] or 0) / 1000,
                                     "text": f"[{pd.get('tool') or 'tool'}] {st.get('title') or ''}\n{out[:TOOL_CHARS]}".rstrip(),
                                     "meta": {"source": "opencode", "tool": pd.get("tool") or ""}})
                if texts:
                    rows.append({"ext": f"opencode:{mid}", "role": role, "text": "\n\n".join(texts), "created": created,
                                 "model": model if role == "assistant" else "", "tokens": tokens, "meta": {"source": "opencode"}})
            rows.sort(key=lambda r: r["created"])
            first_user = next((r["text"] for r in rows if r["role"] == "user"), "")
            created = float(s.get("time_created") or 0) / 1000 or (rows[0]["created"] if rows else time.time())
            emit(ext, {"ext": ext, "title": s.get("title") or (first_user.strip().splitlines()[0][:80] if first_user else "OpenCode session"),
                       "created": created, "updated": float(s.get("time_updated") or 0) / 1000 or created,
                       "model": next((r.get("model") for r in rows if r.get("model")), ""), "archived": bool(s.get("time_archived")),
                       "parent": s.get("parent_id") or ""}, rows)
    return newest


def _opencode_counts(db: Path) -> dict[str, int]:
    with _read(db) as con:
        return {"sessions": con.execute("select count(*) from session").fetchone()[0],
                "messages": con.execute("select count(*) from message").fetchone()[0]}


# ---- settings / config summaries (never a secret) --------------------------------------------------------------------
def scrub(value: Any) -> Any:
    """A copy with every key that names a secret (key, token, password, auth, ...) dropped, and env references kept as names."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not _SECRET.search(str(k))}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _hermes_config(home: Path) -> dict:
    p = home / "config.yaml"
    if not p.is_file():
        return {}
    import yaml

    doc = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace")) or {}
    return scrub({k: doc.get(k) for k in ("model", "providers", "fallback_providers", "delegation", "auxiliary") if k in doc})


def _strip_jsonc(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _opencode_config() -> dict:
    for name in ("opencode.jsonc", "opencode.json"):
        p = opencode_config_dir() / name
        if p.is_file():
            try:
                doc = json.loads(_strip_jsonc(p.read_text(encoding="utf-8", errors="replace")))
            except ValueError:
                return {"error": f"{name} is not valid JSON"}
            return scrub({k: doc.get(k) for k in ("model", "small_model", "provider", "agent", "mcp") if k in doc})
    return {}


def _write_json(path: Path, doc: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _cursors() -> dict:
    return _read_json(library_dir("_state") / "cursors.json", {})


# ---- preview ---------------------------------------------------------------------------------------------------------
def preview() -> dict[str, Any]:
    """Per source: where it is, whether it is there, and counts per kind - in the source, already imported, and new."""
    cfg = settings()
    have = imported_counts()
    cur = _cursors()
    out: dict[str, Any] = {}
    for src in SOURCES:
        info: dict[str, Any] = {"label": LABEL[src], "settings": cfg[src], "imported": have[src], "cursor": cur.get(src)}
        try:
            if src == "hermes":
                home = hermes_home(cfg)
                info["path"] = str(home)
                info["found"] = (home / "state.db").is_file()
                if info["found"]:
                    c = _hermes_counts(home)
                    info["sessions"] = {"total": c["sessions"], "messages": c["messages"],
                                        "new_messages_since_last": max(0, c["max_id"] - int(cur.get(src) or 0))}
                mem = _hermes_memories(home)
                lib = _read_json(library_dir(src) / "memories.json", [])
                known = {e.get("hash") for e in lib}
                info["memories"] = {"total": len(mem), "new": sum(1 for m in mem if _h(m["text"]) not in known)}
                skills = _skill_files(home / "skills")
                info["skills"] = {"total": len(skills), "new": sum(1 for p in skills if not (library_dir(src) / "skills" / p.parent.name / "SKILL.md").is_file())}
                info["config"] = {"total": 1 if (home / "config.yaml").is_file() else 0}
            else:
                db = opencode_db(cfg)
                info["path"] = str(db)
                info["found"] = db.is_file()
                if info["found"]:
                    c = _opencode_counts(db)
                    info["sessions"] = {"total": c["sessions"], "messages": c["messages"],
                                        "new_sessions": max(0, c["sessions"] - have[src]["sessions"])}
                info["memories"] = {"total": 1 if (opencode_config_dir() / "AGENTS.md").is_file() else 0}
                skills = _skill_files(opencode_config_dir() / "skill") + _skill_files(opencode_config_dir() / "skills")
                info["skills"] = {"total": len(skills), "new": sum(1 for p in skills if not (library_dir(src) / "skills" / p.parent.name / "SKILL.md").is_file())}
                info["config"] = {"total": 1 if any((opencode_config_dir() / n).is_file() for n in ("opencode.jsonc", "opencode.json")) else 0}
        except Exception as e:  # noqa: BLE001 - one unreadable source must not hide the other
            info["error"] = f"{type(e).__name__}: {e}"
        out[src] = info
    return {"sources": out, "include_tools": cfg["include_tools"]}


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


# ---- the run ---------------------------------------------------------------------------------------------------------
def status() -> dict[str, Any]:
    with _lock:
        job = json.loads(json.dumps(_job))
    job["last"] = _read_json(library_dir("_state") / "last_run.json", None)
    return job


def _log(msg: str) -> None:
    with _lock:
        _job.setdefault("log", []).append({"at": time.time(), "text": msg})
        _job["log"] = _job["log"][-200:]


def _bump(src: str, **kw: int) -> None:
    with _lock:
        tot = _job.setdefault("counts", {}).setdefault(src, {})
        for k, v in kw.items():
            tot[k] = tot.get(k, 0) + v


def start(*, full: bool = False, sources: Optional[list[str]] = None) -> dict[str, Any]:
    """Start a run in the background; refused (ValueError) while one is running."""
    with _lock:
        if _job.get("running"):
            raise ValueError("an import is already running")
        _cancel.clear()
        _job.clear()
        _job.update({"running": True, "started": time.time(), "phase": "starting", "full": full, "counts": {}, "log": []})
    threading.Thread(target=_run_safe, kwargs={"full": full, "sources": sources}, name="import-run", daemon=True).start()
    return status()


def cancel() -> bool:
    _cancel.set()
    return bool(_job.get("running"))


def _run_safe(**kw: Any) -> None:
    try:
        run(**kw)
    except Exception as e:  # noqa: BLE001 - the panel shows it
        _log(f"failed: {type(e).__name__}: {e}")
        with _lock:
            _job["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _lock:
            _job["running"] = False
            _job["phase"] = "cancelled" if _cancel.is_set() else ("failed" if _job.get("error") else "done")
            _job["finished"] = time.time()
            last = {k: _job.get(k) for k in ("started", "finished", "phase", "counts", "error", "full")}
        _write_json(library_dir("_state") / "last_run.json", last)


def run(*, full: bool = False, sources: Optional[list[str]] = None) -> dict[str, Any]:
    """Import every enabled source and kind. Safe to repeat: nothing is duplicated."""
    cfg = settings()
    cursors = {} if full else _cursors()
    stop = _cancel.is_set
    with _lock:
        _job["counts"] = {}
    for src in sources or SOURCES:
        if src not in SOURCES or not cfg[src]["enabled"] or stop():
            continue
        sc = cfg[src]
        with _lock:
            _job["phase"] = f"{LABEL[src]}"
        if sc["sessions"]:
            con = _target()
            try:
                _ensure_group(con, src)
                def emit(ext: str, sess: dict, rows: list, _src: str = src, _con: sqlite3.Connection = con) -> None:
                    sid, new = _upsert_session(_con, _src, sess)
                    a, u = _upsert_messages(_con, sid, rows)
                    _con.commit()
                    _bump(_src, sessions_new=int(new), sessions_seen=1, messages_added=a, messages_updated=u)
                    if new or a:
                        _log(f"{LABEL[_src]}: {sess['title'][:60] or ext} (+{a} messages{', new' if new else ''})")

                if src == "hermes":
                    home = hermes_home(cfg)
                    if (home / "state.db").is_file():
                        newest = _hermes_sessions(home, int(cursors.get(src) or 0), cfg["include_tools"], emit, stop)
                    else:
                        newest = None
                        _log("Hermes: no state.db found")
                else:
                    db = opencode_db(cfg)
                    if db.is_file():
                        newest = _opencode_sessions(db, float(cursors.get(src) or 0), cfg["include_tools"], emit, stop)
                    else:
                        newest = None
                        _log("OpenCode: no opencode.db found")
                if newest is not None and not stop():
                    cur = _cursors()
                    cur[src] = newest
                    _write_json(library_dir("_state") / "cursors.json", cur)
            finally:
                con.close()
        if sc["memories"] and not stop():
            _import_memories(src)
        if sc["skills"] and not stop():
            _import_skills(src)
        if sc["config"] and not stop():
            _import_config(src)
    return status()


def _import_memories(src: str) -> None:
    if src == "hermes":
        found = _hermes_memories(hermes_home())
    else:
        p = opencode_config_dir() / "AGENTS.md"
        found = [{"file": "AGENTS.md", "text": p.read_text(encoding="utf-8", errors="replace")}] if p.is_file() else []
    path = library_dir(src) / "memories.json"
    lib = _read_json(path, [])
    known = {e.get("hash") for e in lib}
    added = 0
    for m in found:
        h = _h(m["text"])
        if h not in known:
            lib.append({"hash": h, "file": m["file"], "text": m["text"], "imported": time.time()})
            known.add(h)
            added += 1
    _write_json(path, lib)
    _bump(src, memories_added=added)
    _log(f"{LABEL[src]}: {added} new memories ({len(lib)} in the library)")


def _import_skills(src: str) -> None:
    root = hermes_home() / "skills" if src == "hermes" else opencode_config_dir()
    files = _skill_files(root) if src == "hermes" else _skill_files(root / "skill") + _skill_files(root / "skills")
    dest = library_dir(src) / "skills"
    added = updated = 0
    for i, f in enumerate(files):
        if _cancel.is_set():
            break
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:SKILL_CHARS]
        except OSError:
            continue
        out = dest / f.parent.name / "SKILL.md"
        old = out.read_text(encoding="utf-8", errors="replace") if out.is_file() else None
        if old == text:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        added, updated = (added + 1, updated) if old is None else (added, updated + 1)
        if i % 500 == 0:
            with _lock:
                _job["phase"] = f"{LABEL[src]} skills {i}/{len(files)}"
    _bump(src, skills_added=added, skills_updated=updated)
    _log(f"{LABEL[src]}: {added} new skills, {updated} changed ({len(files)} found; kept in the import library, not active)")


def _import_config(src: str) -> None:
    doc = _hermes_config(hermes_home()) if src == "hermes" else _opencode_config()
    _write_json(library_dir(src) / "config.json", {"imported": time.time(), "settings": doc})
    _bump(src, config=1 if doc else 0)
    _log(f"{LABEL[src]}: model / provider settings summarized ({', '.join(doc) or 'none found'}; secrets left out)")


def library() -> dict[str, Any]:
    """What the import library holds per source: memories, skill names, the settings summary."""
    out = {}
    for src in SOURCES:
        d = library_dir(src)
        skills = sorted(p.parent.name for p in (d / "skills").glob("*/SKILL.md")) if (d / "skills").is_dir() else []
        out[src] = {"memories": _read_json(d / "memories.json", []), "skills": skills,
                    "config": _read_json(d / "config.json", {}).get("settings") or {}}
    return out
