"""What feeds the knowledge base: a typed registry of sources, a reader per kind, and the sync that keeps them fresh.

    folder         a local folder (glob, default **/*.md and *.txt; 10 MB a file; never outside the folder)
    notes          the vault's own notes/ folder (always present: hand-written notes are knowledge too)
    github         a repository's commits, issues and pull requests (the gh CLI; GitHub's public API without it)
    rss            an RSS or Atom feed's items
    web            a web page (optionally narrowed to an element: tag, #id or .class)
    conversation   ABP's own conversations (the memory fabric's threads), one item per thread
    nexusfoundry   a NexusFoundry checkout (or its Knowledge Module folder), one item per KM

Each source has budgets (max_items, max_chars a sync) so a chatty source cannot flood the base; each sync records
its stage and counts, and every source's freshness is derived from its newest chunk (active <= 30 s, recent <= 5 min,
idle). Items that vanish from a source are removed; a sync is followed by a diff snapshot (bot/memoryfabric/diff.py).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator, Optional

import httpx

from bot import db
from bot.memoryfabric import knowledge

KINDS = {"folder": ["path"], "notes": [], "github": ["repo"], "rss": ["url"], "web": ["url"], "conversation": [],
         "nexusfoundry": ["path"]}
MAX_FILE = 10 << 20
KM_STORE = ("storage", "knowledge_modules")       # where NexusFoundry keeps the KMs its app builds
_NO_WINDOW = 0x08000000


def _conn():
    c = knowledge._conn()
    with db._lock:
        c.execute("CREATE TABLE IF NOT EXISTS kb_sources (id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, "
                  "config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, last_sync REAL, status TEXT)")
        c.commit()
    return c


def _row(r) -> dict:
    return {"id": r[0], "kind": r[1], "label": r[2], **json.loads(r[3]), "enabled": bool(r[4]), "created": r[5],
            "last_sync": r[6], "status": json.loads(r[7]) if r[7] else None}


def listing() -> list[dict]:
    ensure_notes()
    return [_row(r) for r in _conn().execute("SELECT * FROM kb_sources ORDER BY created")]


def get(source_id: str) -> dict:
    r = _conn().execute("SELECT * FROM kb_sources WHERE id=?", (source_id,)).fetchone()
    if not r:
        raise ValueError(f"no memory source {source_id!r}")
    return _row(r)


def add(kind: str, label: str = "", **config) -> dict:
    if kind not in KINDS or kind == "notes":
        raise ValueError(f"a source is one of {', '.join(k for k in KINDS if k != 'notes')}")
    for f in KINDS[kind]:
        if not str(config.get(f) or "").strip():
            raise ValueError(f"a {kind} source needs {f}")
    if kind in ("folder", "nexusfoundry") and not Path(config["path"]).is_dir():
        raise ValueError(f"{config['path']} is not a folder")
    if kind == "github" and not re.fullmatch(r"[\w.-]+/[\w.-]+", config["repo"]):
        raise ValueError("repo is owner/name")
    if kind in ("rss", "web") and not re.match(r"https?://", config["url"]):
        raise ValueError("url is an http(s) address")
    sid = f"{kind}-" + hashlib.sha1(json.dumps([kind, config], sort_keys=True).encode()).hexdigest()[:8]
    cfg = {"max_items": 200, "max_chars": 2_000_000, **{k: v for k, v in config.items() if v not in (None, "")}}
    c = _conn()
    with db._lock:
        c.execute("INSERT OR REPLACE INTO kb_sources(id, kind, label, config, enabled, created) VALUES (?,?,?,?,1,?)",
                  (sid, kind, label or config.get("path") or config.get("repo") or config.get("url") or kind, json.dumps(cfg), time.time()))
        c.commit()
    return get(sid)


def update(source_id: str, **patch) -> dict:
    s = get(source_id)
    c = _conn()
    enabled = patch.pop("enabled", None)
    label = patch.pop("label", None)
    cfg = {k: v for k, v in s.items() if k not in ("id", "kind", "label", "enabled", "created", "last_sync", "status")}
    cfg.update(patch)
    with db._lock:
        c.execute("UPDATE kb_sources SET config=?, label=COALESCE(?, label), enabled=COALESCE(?, enabled) WHERE id=?",
                  (json.dumps(cfg), label, None if enabled is None else int(bool(enabled)), source_id))
        c.commit()
    return get(source_id)


def remove(source_id: str) -> bool:
    if source_id == "notes":
        raise ValueError("the vault's notes are always a source")
    c = _conn()
    with db._lock:
        n = c.execute("DELETE FROM kb_sources WHERE id=?", (source_id,)).rowcount
        c.commit()
    knowledge.remove_source(source_id)
    return bool(n)


def ensure_notes() -> None:
    c = _conn()
    if not c.execute("SELECT 1 FROM kb_sources WHERE id='notes'").fetchone():
        with db._lock:
            c.execute("INSERT OR IGNORE INTO kb_sources(id, kind, label, config, enabled, created) VALUES ('notes','notes','Vault notes',?,1,?)",
                      (json.dumps({"max_items": 2000, "max_chars": 20_000_000}), time.time()))
            c.commit()


# ---- readers: (item_id, title, text, ts, tags) --------------------------------------------------------------------- #

Item = tuple[str, str, str, float, list[str]]


def _read_folder(base: Path, pattern: str) -> Iterator[Item]:
    base = base.resolve()
    pats = [p.strip() for p in (pattern or "**/*.md,**/*.txt").split(",") if p.strip()]
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.stat().st_size > MAX_FILE or any(part.startswith(".") for part in p.relative_to(base).parts):
            continue
        rel = p.relative_to(base).as_posix()
        if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat.split("/")[-1]) for pat in pats):
            continue
        try:
            p.resolve().relative_to(base)                       # a link out of the folder is not followed
        except ValueError:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        yield rel, p.stem, text, p.stat().st_mtime, ["note", "authored"] if p.suffix == ".md" else ["document"]


def _gh(args: list[str]) -> Optional[list]:
    exe = shutil.which("gh")
    if not exe:
        return None
    r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
                       creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
    if r.returncode:
        return None
    try:
        return json.loads(r.stdout or "[]")
    except ValueError:
        return None


def _iso_ts(s: str) -> float:
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return time.time()


def _read_github(repo: str, limit: int) -> Iterator[Item]:
    commits = _gh(["api", f"repos/{repo}/commits?per_page={min(limit, 100)}"])
    issues = _gh(["api", f"repos/{repo}/issues?state=all&per_page={min(limit, 100)}"])
    if commits is None or issues is None:                      # no gh (or not signed in): GitHub's public API
        with httpx.Client(timeout=30, headers={"Accept": "application/vnd.github+json", "User-Agent": "ABP"}) as c:
            commits = c.get(f"https://api.github.com/repos/{repo}/commits", params={"per_page": min(limit, 100)}).json()
            issues = c.get(f"https://api.github.com/repos/{repo}/issues", params={"state": "all", "per_page": min(limit, 100)}).json()
    for cm in commits if isinstance(commits, list) else []:
        info = cm.get("commit") or {}
        author = (info.get("author") or {})
        yield (f"commit/{cm['sha']}", (info.get("message") or "").split("\n")[0][:120],
               f"Commit {cm['sha'][:10]} by {author.get('name', '?')} <{author.get('email', '')}>\n\n{info.get('message', '')}",
               _iso_ts(author.get("date", "")), ["github", "priority_high", "authored"])
    for it in issues if isinstance(issues, list) else []:
        kind = "pr" if it.get("pull_request") else "issue"
        tags = ["github"] + (["priority_high"] if it.get("state") == "closed" else [])
        yield (f"{kind}/{it['number']}", f"#{it['number']} {it.get('title', '')}",
               f"{'Pull request' if kind == 'pr' else 'Issue'} #{it['number']} ({it.get('state')}) by "
               f"@{(it.get('user') or {}).get('login', '?')}: {it.get('title', '')}\n\n{it.get('body') or ''}",
               _iso_ts(it.get("updated_at", "")), tags)


def _read_rss(url: str, limit: int) -> Iterator[Item]:
    r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "ABP"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//a:entry", ns)
    for it in items[:limit]:
        def t(*names, it=it):
            for n in names:
                v = it.findtext(n, namespaces=ns)
                if v:
                    return v
            return ""
        link = t("link") or ((it.find("a:link", ns).get("href") if it.find("a:link", ns) is not None else "") or "")
        ident = t("guid", "a:id") or link or t("title", "a:title")
        import email.utils as eu
        when = t("pubDate", "a:updated", "a:published")
        try:
            ts = eu.parsedate_to_datetime(when).timestamp() if when and "," in when else _iso_ts(when)
        except (TypeError, ValueError):
            ts = time.time()
        yield (hashlib.sha1(ident.encode()).hexdigest()[:16], t("title", "a:title"),
               f"{t('title', 'a:title')}\n{link}\n\n{t('description', 'a:summary', 'a:content')}", ts, ["rss"])


def _select(html: str, selector: str) -> str:
    """The inner HTML of the first element matching a tag, #id or .class selector (the whole page without one)."""
    if not selector:
        return html
    if selector.startswith("#"):
        pat = rf"<(\w+)[^>]*\bid=[\"']{re.escape(selector[1:])}[\"'][^>]*>"
    elif selector.startswith("."):
        pat = rf"<(\w+)[^>]*\bclass=[\"'][^\"']*\b{re.escape(selector[1:])}\b[^\"']*[\"'][^>]*>"
    else:
        pat = rf"<({re.escape(selector)})\b[^>]*>"
    m = re.search(pat, html, re.I)
    if not m:
        return html
    tag, depth, i = m.group(1).lower(), 1, m.end()
    for t in re.finditer(rf"<(/?){tag}\b[^>]*>", html[i:], re.I):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return html[i:i + t.start()]
    return html[i:]


def _read_web(url: str, selector: str) -> Iterator[Item]:
    r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "ABP"})
    r.raise_for_status()
    title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
    yield "page", (title.group(1).strip() if title else url), _select(r.text, selector), time.time(), ["web"]


def _read_conversations(limit: int) -> Iterator[Item]:
    from bot.memoryfabric import store
    for t in store.threads(limit=limit):
        turns = store.turns(t["thread"], limit=400)
        if not turns:
            continue
        text = "\n\n".join(f"{'Person' if x['role'] == 'user' else 'Assistant (' + (x['model'] or x['backend'] or 'model') + ')'}: {x['text']}"
                           for x in turns)
        tags = ["conversation", "reply"] if any(x["role"] == "user" for x in turns) else ["conversation"]
        yield t["thread"], f"Conversation {t['thread']}", text, turns[-1]["created"], tags


def km_root(path: str) -> Path:
    """Where a nexusfoundry source's Knowledge Modules are: `path` itself, or a NexusFoundry
    checkout's own store (the folder its app builds them in, storage/knowledge_modules)."""
    p = Path(path)
    store = p.joinpath(*KM_STORE)
    return store if store.is_dir() else p


def _km_meta(d: Path) -> Optional[dict]:
    """A Knowledge Module's metadata, or None when the folder is not one: NexusFoundry's registry
    (core/registry.py) skips a directory without meta.json and tolerates an unreadable one."""
    if not d.is_dir() or not (d / "meta.json").is_file():
        return None
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _km_chunks(d: Path) -> list[str]:
    """The fact bank: chunks.json, a JSON array of strings (NexusFoundry's core/km.py save()). A
    LoRA-only module has none, and a half-written one may not parse; both are no chunks, not a failure."""
    try:
        raw = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(c).strip() for c in raw if str(c).strip()] if isinstance(raw, list) else []


def _read_kms(root: Path, limit: int) -> Iterator[Item]:
    """One item per Knowledge Module: its name and id as the document, its metadata as the entities
    (domain, subdomain and tags) and its chunks as the body, so every model can recall what it holds."""
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        meta = _km_meta(d)
        if meta is None:
            continue
        km_id = str(meta.get("km_id") or d.name)
        name = str(meta.get("name") or km_id)
        domain, subdomain = str(meta.get("domain") or ""), str(meta.get("subdomain") or "")
        km_tags = [str(t) for t in (meta.get("tags") or []) if str(t).strip()]
        chunks = _km_chunks(d)
        if limit > 0:
            chunks = chunks[:limit]
        head = [f"{name} - Knowledge Module {km_id}, from NexusFoundry."]
        if domain or subdomain:
            head.append(f"Domain: {domain}. Subdomain: {subdomain}.")
        if km_tags:
            head.append("Tags: " + ", ".join(km_tags) + ".")
        if str(meta.get("description") or "").strip():
            head.append(str(meta["description"]).strip())
        try:
            ts = float(meta.get("created_at") or 0.0) or (d / "meta.json").stat().st_mtime
        except (OSError, TypeError, ValueError):
            ts = time.time()
        tags = list(dict.fromkeys(["nexusfoundry", "km"] + [t for t in (domain, subdomain) if t] + km_tags))
        yield km_id, name, "\n\n".join(head + chunks), ts, tags


def read(source: dict) -> Iterator[Item]:
    k, limit = source["kind"], int(source.get("max_items", 200))
    if k == "folder":
        yield from _read_folder(Path(source["path"]), source.get("glob", ""))
    elif k == "notes":
        from bot.memoryfabric import vault
        yield from _read_folder(vault.root() / "notes", "**/*.md")
    elif k == "github":
        yield from _read_github(source["repo"], limit)
    elif k == "rss":
        yield from _read_rss(source["url"], limit)
    elif k == "web":
        yield from _read_web(source["url"], source.get("selector", ""))
    elif k == "conversation":
        yield from _read_conversations(limit)
    elif k == "nexusfoundry":
        yield from _read_kms(km_root(source["path"]), int(source.get("max_chunks", 0)))


KIND_OF = {"folder": "document", "notes": "note", "github": "github", "rss": "rss", "web": "web",
           "conversation": "conversation", "nexusfoundry": "note"}


def sync(source_id: str, log=lambda m: None) -> dict:
    """Read the source, ingest what is new or changed (by content hash), remove what is gone, snapshot the result."""
    s = get(source_id)
    status = {"stage": "fetching", "started": time.time()}
    _set_status(source_id, status)
    c = knowledge._conn()
    have = {r[0] for r in c.execute("SELECT DISTINCT item_id FROM kb_chunks WHERE source_id=?", (source_id,))}
    seen, added, changed, chars = set(), 0, 0, 0
    try:
        for i, (item_id, title, text, ts, tags) in enumerate(read(s)):
            if i >= int(s.get("max_items", 200)) or chars > int(s.get("max_chars", 2_000_000)):
                break
            seen.add(item_id)
            chars += len(text)
            digest = hashlib.sha1(text.encode()).hexdigest()[:12]
            if item_id in have and _digest_of(source_id, item_id) == digest:
                continue
            status.update(stage="ingesting", item=item_id)
            knowledge.ingest(source_id, item_id, title, text, kind=KIND_OF[s["kind"]], ts=ts, tags=tags)
            _set_digest(source_id, item_id, digest)
            added += item_id not in have
            changed += item_id in have
        removed = 0
        if s["kind"] in ("folder", "notes", "conversation", "web", "nexusfoundry"):  # complete listings: gone = deleted
            for item_id in set(have) - seen:
                knowledge.remove_item(source_id, item_id)
                removed += 1
        knowledge.seal(source_id)
        status = {"stage": "completed", "added": added, "changed": changed, "removed": removed, "items": len(seen),
                  "finished": time.time()}
    except (httpx.HTTPError, OSError, ET.ParseError, ValueError) as e:
        status = {"stage": "failed", "error": str(e)[:400], "finished": time.time()}
    _set_status(source_id, status, synced=status["stage"] == "completed")
    log(f"memory source {source_id}: {status}")
    if status["stage"] == "completed":
        try:
            from bot.memoryfabric import diff, vault
            diff.snapshot(source_id, trigger="sync")
            vault.write_source(source_id)
        except Exception as e:  # noqa: BLE001 - the sync itself succeeded
            status["snapshot_error"] = str(e)[:200]
    return {"source_id": source_id, **status}


def _set_status(source_id: str, status: dict, synced: bool = False) -> None:
    c = _conn()
    with db._lock:
        c.execute("UPDATE kb_sources SET status=?" + (", last_sync=?" if synced else "") + " WHERE id=?",
                  (json.dumps(status), *([time.time()] if synced else []), source_id))
        c.commit()


def _digest_of(source_id: str, item_id: str) -> Optional[str]:
    c = _conn()
    with db._lock:
        c.execute("CREATE TABLE IF NOT EXISTS kb_item_digests (source_id TEXT, item_id TEXT, digest TEXT, PRIMARY KEY (source_id, item_id))")
        c.commit()
    r = c.execute("SELECT digest FROM kb_item_digests WHERE source_id=? AND item_id=?", (source_id, item_id)).fetchone()
    return r[0] if r else None


def _set_digest(source_id: str, item_id: str, digest: str) -> None:
    _digest_of(source_id, item_id)
    c = _conn()
    with db._lock:
        c.execute("INSERT OR REPLACE INTO kb_item_digests VALUES (?,?,?)", (source_id, item_id, digest))
        c.commit()


def freshness(source_id: str) -> str:
    r = knowledge._conn().execute("SELECT MAX(created) FROM kb_chunks WHERE source_id=?", (source_id,)).fetchone()
    age = time.time() - (r[0] or 0)
    return "active" if age <= 30 else "recent" if age <= 300 else "idle"


def status_list() -> list[dict]:
    st = knowledge.stats()["sources"]
    return [{**s, "freshness": freshness(s["id"]), **st.get(s["id"], {"chunks": 0, "kept": 0})} for s in listing()]


def due(minutes: float) -> list[str]:
    now = time.time()
    return [s["id"] for s in listing() if s["enabled"] and now - (s["last_sync"] or 0) >= minutes * 60]
