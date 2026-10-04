"""What changed in memory, as a git repository: every sync of a source is a commit, so "what's new since I last
looked?" is a diff.

    snapshot       one commit per sync: the source's items (one file each, under src_<id>/) rebuilt, every other source
                   carried forward unchanged; source, trigger, item count and time ride as commit trailers
    read marker    refs/abp/read/<source>: diff(source, since_read=True) shows what arrived since the last read, then
                   (commit=True) moves the marker, so an agent polling a source never reads the same news twice
    checkpoint     an annotated tag ckpt_<name>: diff(checkpoint=...) shows everything since then, across sources

The knowledge base (bot/memoryfabric/knowledge.py) stays the truth; this ledger is derived from it and costs no
model or network calls. Where: <vault>/../diff/repo (git must be installed).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from bot.memoryfabric import knowledge

_lock = threading.Lock()                   # git's HEAD bookkeeping is read-modify-write: one commit at a time
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def repo() -> Path:
    from bot.memoryfabric import vault
    p = vault.root().parent / "diff" / "repo"
    if not (p / ".git").exists():
        p.mkdir(parents=True, exist_ok=True)
        _git(p, "init", "-q", "-b", "main")
    return p


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    exe = shutil.which("git")
    if not exe:
        raise RuntimeError("git is not installed: the memory diff ledger needs it")
    r = subprocess.run([exe, "-c", "user.name=ABP memory", "-c", "user.email=memory@abp.local", "-c", "core.autocrlf=false",
                        "-c", "core.quotepath=false", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=_NO_WINDOW, timeout=120)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def _dir(source_id: str) -> str:
    return "src_" + re.sub(r"[^\w.-]+", "_", source_id)


def _item_file(item_id: str) -> str:
    return re.sub(r"[^\w.-]+", "_", item_id).strip("_")[:120] + ".md"


def _head(r: Path) -> Optional[str]:
    out = _git(r, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    return out or None


def snapshot(source_id: str, trigger: str = "manual") -> Optional[str]:
    """Commit the source's current items; returns the snapshot id (the commit sha), or the previous one when nothing
    changed."""
    c = knowledge._conn()
    items: dict[str, list[str]] = {}
    for item_id, title, body in c.execute("SELECT item_id, title, body FROM kb_chunks WHERE source_id=? ORDER BY item_id, seq",
                                          (source_id,)):
        items.setdefault(item_id, [f"# {title}\n" if title else ""]).append(body)
    with _lock:
        r = repo()
        d = r / _dir(source_id)
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        for item_id, parts in items.items():
            (d / _item_file(item_id)).write_text("\n\n".join(p for p in parts if p).strip() + "\n", encoding="utf-8", newline="\n")
        _git(r, "add", "-A", "--", _dir(source_id))
        if _git(r, "status", "--porcelain", "--", _dir(source_id)).strip() == "" and _head(r):
            return _head(r)
        msg = (f"Snapshot {source_id}\n\nSource-Id: {source_id}\nTrigger: {trigger}\nItem-Count: {len(items)}\n"
               f"Taken-At-Ms: {int(time.time() * 1000)}\n")
        _git(r, "commit", "-q", "--allow-empty", "-m", msg)
        return _head(r)


def snapshots(source_id: str = "", limit: int = 50) -> list[dict]:
    r = repo()
    if not _head(r):
        return []
    args = ["log", f"-n{limit}", "--format=%H%x1f%B%x1e"] + (["--", _dir(source_id)] if source_id else [])
    out = []
    for rec in _git(r, *args).split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, body = rec.strip().split("\x1f", 1)
        trailers = dict(re.findall(r"^([A-Z][\w-]+): (.+)$", body, re.M))
        out.append({"id": sha, "source_id": trailers.get("Source-Id"), "trigger": trailers.get("Trigger"),
                    "items": int(trailers.get("Item-Count", 0)), "taken_at_ms": int(trailers.get("Taken-At-Ms", 0))})
    return out


def checkpoint(name: str) -> str:
    if not re.fullmatch(r"[\w.-]{1,60}", name):
        raise ValueError("a checkpoint name is letters, digits, dots, dashes")
    r = repo()
    if not _head(r):
        raise ValueError("nothing has been snapshotted yet")
    _git(r, "tag", "-f", "-a", f"ckpt_{name}", "-m", f"checkpoint {name}")
    return name


def _changes(r: Path, base: Optional[str], head: str, path: str, text: bool) -> dict:
    if base == head:
        return {"added": [], "removed": [], "modified": []}
    if base:
        rows = _git(r, "diff", "--name-status", "--no-renames", base, head, "--", path)
    else:                                                     # nothing read yet: everything there is new
        rows = "".join(f"A\t{f}\n" for f in _git(r, "ls-tree", "-r", "--name-only", head, "--", path).splitlines())
    out: dict = {"added": [], "removed": [], "modified": []}
    for line in rows.splitlines():
        status, _, f = line.partition("\t")
        key = {"A": "added", "D": "removed", "M": "modified"}.get(status[:1])
        if not key:
            continue
        entry: dict = {"item": Path(f).stem, "source": f.split("/", 1)[0][4:]}
        if text and key == "modified" and base:
            entry["diff"] = _git(r, "diff", base, head, "--", f)[:2000]
        out[key].append(entry)
    return out


def diff(source_id: str = "", *, checkpoint_name: str = "", since_read: bool = True, commit: bool = True,
         include_text: bool = False) -> dict:
    """What changed: in one source since its read marker (or since its previous snapshot), or across all sources since
    a checkpoint. With no source and no checkpoint, the sources and their snapshot counts."""
    with _lock:
        r = repo()
        head = _head(r)
        if not head:
            return {"sources": [], "note": "nothing has been snapshotted yet"}
        if checkpoint_name:
            base = _git(r, "rev-parse", "--verify", "-q", f"ckpt_{checkpoint_name}^{{commit}}", check=False).strip()
            if not base:
                raise ValueError(f"no checkpoint {checkpoint_name!r}")
            return {"checkpoint": checkpoint_name, "head": head, **_changes(r, base, head, ".", include_text)}
        if not source_id:
            counts: dict[str, int] = {}
            for s in snapshots(limit=10_000):
                counts[s["source_id"]] = counts.get(s["source_id"], 0) + 1
            return {"sources": [{"source_id": k, "snapshots": v} for k, v in sorted(counts.items()) if k]}
        path = _dir(source_id)
        marker = f"refs/abp/read/{path}"
        if since_read:
            base = _git(r, "rev-parse", "--verify", "-q", marker, check=False).strip() or None
        else:
            shas = [s["id"] for s in snapshots(source_id, 2)]
            base = shas[1] if len(shas) > 1 else None
        out = {"source_id": source_id, "head": head, "base": base, **_changes(r, base, head, path, include_text)}
        out["unchanged"] = max(0, len(_git(r, "ls-tree", "-r", "--name-only", head, "--", path).splitlines())
                               - len(out["added"]) - len(out["modified"]))
        if since_read and commit:
            _git(r, "update-ref", marker, head)
        return out


def summary_text(d: dict) -> str:
    """A diff as short Markdown (what the memory_diff tool answers)."""
    if "sources" in d:
        return "\n".join(f"- {s['source_id']}: {s['snapshots']} snapshot(s)" for s in d["sources"]) or d.get("note", "no sources")
    head = f"Changes in {d.get('source_id') or 'every source since ' + d.get('checkpoint', '')}:"
    lines = [head]
    for key in ("added", "modified", "removed"):
        if d[key]:
            lines.append(f"{key.capitalize()} ({len(d[key])}): " + ", ".join(x["item"] for x in d[key][:30]))
    if len(lines) == 1:
        lines.append("nothing new.")
    return "\n".join(lines)
