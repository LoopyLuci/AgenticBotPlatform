"""The memory as a folder of Markdown you can open in Obsidian (or any editor), edit, and link by hand.

    <vault>/memories/shared.md, memories/bot-<id>.md   every approved memory, one bullet each (edit, add or delete
                                                       lines: read back by read_memories(), a person's edit counts)
    <vault>/summaries/<source>/L<level>/<id>.md        the summary trees, frontmatter with provenance (source, level,
                                                       time range) and [[links]] to the children
    <vault>/entities/<kind>/<name>.md                  each entity: mentions and [[links]] to related ones
    <vault>/notes/                                     your own notes: a source of their own (sources.py, "notes")

Where: ABP_MEMORY_DIR, else <ABP data>/memory/vault. Links are [[wiki-links]], so Obsidian's graph, backlinks and tags
work as they are.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from bot import db, memory
from bot.memoryfabric import knowledge

_LINE = re.compile(r"^- (?P<text>.+?)(?:\s*<!--\s*m(?P<id>\d+)\s*-->)?\s*$")


def root() -> Path:
    env = os.environ.get("ABP_MEMORY_DIR", "").strip()
    if env:
        p = Path(env)
    else:
        from bot.envfile import PROJECT_ROOT
        p = PROJECT_ROOT / "data" / "memory" / "vault"
    for sub in ("notes", "memories", "summaries", "entities"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def obsidian_link() -> str:
    return "obsidian://open?path=" + quote(str(root()))


def _slug(s: str) -> str:
    return re.sub(r"[^\w.-]+", "-", s).strip("-.")[:80] or "item"


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ---- memories (two-way) -------------------------------------------------------------------------------------------- #

def _memory_file(instance_id: int) -> Path:
    return root() / "memories" / ("shared.md" if instance_id == 0 else f"bot-{instance_id}.md")


def write_memories(instance_id: int) -> Path:
    rows = [dict(r) for r in db.list_memory_entries(instance_id, status="approved")]
    title = "Shared memories (every bot and model)" if instance_id == 0 else f"Memories of bot {instance_id}"
    lines = ["---", f"scope: {'shared' if instance_id == 0 else 'bot-' + str(instance_id)}", f"updated: {_iso(time.time())}", "---",
             f"# {title}", "", "Edit, add or delete lines; ABP reads this file back. Keep the <!-- mN --> markers on edited lines.", ""]
    for kind in memory.KINDS:
        group = sorted((r for r in rows if r.get("kind", "fact") == kind), key=lambda r: r["id"])
        if group:
            lines += [f"## {memory._KIND_TITLES[kind]}", ""]
            lines += [f"- {r['content']} <!-- m{r['id']} -->" for r in group]
            lines.append("")
    path = _memory_file(instance_id)
    _atomic(path, "\n".join(lines))
    _mark_written(path)
    return path


def _mark_written(path: Path) -> None:
    st = _state()
    st[str(path)] = path.stat().st_mtime
    _save_state(st)


def _state() -> dict:
    try:
        return json.loads((root() / ".abp-vault.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _atomic(root() / ".abp-vault.json", json.dumps(st))


def read_memories(instance_id: int) -> dict:
    """A person's edits to a memories file, applied: changed lines update their memory, new lines become approved
    memories (a person wrote them), removed lines are deleted. Nothing happens unless the file changed since ABP wrote it."""
    path = _memory_file(instance_id)
    if not path.exists() or _state().get(str(path)) == path.stat().st_mtime:
        return {"changed": 0, "added": 0, "removed": 0}
    rows = {r["id"]: dict(r) for r in db.list_memory_entries(instance_id, status="approved")}
    kind, seen, out = "fact", set(), {"changed": 0, "added": 0, "removed": 0}
    titles = {v: k for k, v in memory._KIND_TITLES.items()}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            kind = titles.get(line[3:].strip(), "fact")
            continue
        m = _LINE.match(line)
        if not m:
            continue
        text, mid = m.group("text").strip(), m.group("id")
        if mid and int(mid) in rows:
            seen.add(int(mid))
            r = rows[int(mid)]
            if r["content"] != text or r.get("kind", "fact") != kind:
                c = db.get_conn()
                with db._lock:
                    c.execute("UPDATE memory_entries SET content=?, kind=? WHERE id=?", (text, kind, int(mid)))
                    c.commit()
                out["changed"] += 1
        elif text:
            dup = memory.find_duplicate(instance_id, text)
            if dup:
                seen.add(dup["id"])
                continue
            new = db.create_memory_entry(instance_id, text, source="vault", status="approved", kind=kind)
            seen.add(new)
            out["added"] += 1
    for mid in set(rows) - seen:
        memory.forget(instance_id, mid)
        out["removed"] += 1
    write_memories(instance_id)
    return out


def sync_memories() -> dict:
    """Every memories file read back, then rewritten from the database."""
    ids = {0} | {r[0] for r in db.get_conn().execute("SELECT DISTINCT instance_id FROM memory_entries")}
    for f in (root() / "memories").glob("bot-*.md"):
        try:
            ids.add(int(f.stem[4:]))
        except ValueError:
            pass
    total = {"changed": 0, "added": 0, "removed": 0}
    for iid in sorted(ids):
        for k, v in read_memories(iid).items():
            total[k] += v
        write_memories(iid)
    return total


# ---- summaries and entities (written from the knowledge base) ------------------------------------------------------ #

def write_source(source_id: str) -> int:
    c = knowledge._conn()
    base = root() / "summaries" / _slug(source_id)
    keep = set()
    n = 0
    for r in c.execute("SELECT * FROM kb_summaries WHERE source_id=? ORDER BY level, t0", (source_id,)):
        h = knowledge._hit_summary(r)
        ents = h["entities"]
        body = ["---", f"id: {h['node_id']}", f"source: {source_id}", f"level: {h['level']}",
                f"time_start: {h['time_range_start']}", f"time_end: {h['time_range_end']}",
                f"entities: [{', '.join(json.dumps(e) for e in ents[:40])}]", "---",
                f"# {source_id} L{h['level']} {h['time_range_start'][:10]}", "", h["content"], ""]
        kids = [k for k in h["child_ids"] if k.startswith("s")]
        if kids:
            body += ["Children: " + " ".join(f"[[{k}]]" for k in kids), ""]
        if ents:
            body += ["Entities: " + " ".join(f"[[{_entity_note(e)}]]" for e in ents[:40]), ""]
        path = base / f"L{h['level']}" / f"{h['node_id']}.md"
        _atomic(path, "\n".join(body))
        keep.add(path)
        n += 1
    for old in base.rglob("*.md") if base.exists() else []:
        if old not in keep:
            old.unlink()
    write_entities()
    return n


def _entity_note(entity: str) -> str:
    kind, _, name = entity.partition(":")
    return f"{kind}-{_slug(name)}"


def write_entities(limit: int = 2000) -> int:
    c = knowledge._conn()
    rows = c.execute("SELECT entity, kind, MIN(display), COUNT(DISTINCT node_id) FROM kb_entities GROUP BY entity, kind "
                     "ORDER BY 4 DESC LIMIT ?", (limit,)).fetchall()
    for e, kind, display, n in rows:
        rel = knowledge.neighbors(e, 15)
        body = ["---", f"id: {e}", f"kind: {kind}", f"display_name: {json.dumps(display)}", f"mentions: {n}", "---", f"# {display}", ""]
        if rel:
            body += ["Related: " + " ".join(f"[[{_entity_note(x['object'])}]] ({x['weight']})" for x in rel), ""]
        _atomic(root() / "entities" / kind / f"{_entity_note(e)}.md", "\n".join(body))
    return len(rows)


def write_all() -> dict:
    from bot.memoryfabric import sources
    out = {"memories": sync_memories(), "sources": 0}
    for s in sources.listing():
        write_source(s["id"])
        out["sources"] += 1
    out["entities"] = write_entities()
    return out
