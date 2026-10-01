"""Variants: overlays of ABP's UI files, kept in data/studio/variants/<id>/ (only the files a variant changes, at their
paths relative to ABP's code root, plus meta.json). Nothing here writes to the live UI except apply(), which backs up
first; revert() puts a backup back."""
from __future__ import annotations

import difflib
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from bot.envfile import CODE_ROOT, PROJECT_ROOT

# The UI a variant may change: the dashboard and the desktop app's pages, scripts and styles.
UI_ROOTS = ("bot/dashboard/static", "desktop-app/ui")
UI_EXTS = (".html", ".js", ".css", ".svg", ".json", ".md")
MAX_FILE = 4 * 1024 * 1024
_ID = re.compile(r"^[a-z0-9-]{4,40}$")
_lock = threading.Lock()


class StudioError(ValueError):
    pass


def studio_dir() -> Path:
    import os
    d = Path(os.environ.get("ABP_STUDIO_DIR") or PROJECT_ROOT / "data" / "studio")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _variants_dir() -> Path:
    d = studio_dir() / "variants"
    d.mkdir(parents=True, exist_ok=True)
    return d


def code_root() -> Path:
    import os
    return Path(os.environ.get("ABP_STUDIO_CODE_ROOT") or CODE_ROOT)


def check_rel(rel: str) -> str:
    """A UI file's path relative to the code root, normalised, or StudioError."""
    r = str(rel or "").replace("\\", "/").lstrip("/")
    parts = r.split("/")
    if not r or ".." in parts or any(p.startswith(".") for p in parts) or ":" in r:
        raise StudioError(f"{rel!r}: not a UI file path")
    if not any(r == root or r.startswith(root + "/") for root in UI_ROOTS) or not r.endswith(UI_EXTS):
        raise StudioError(f"{rel}: variants change only the UI ({', '.join(UI_ROOTS)}; {' '.join(UI_EXTS)})")
    return r


def _dir(vid: str) -> Path:
    if not _ID.match(vid or ""):
        raise StudioError(f"no variant {vid!r}")
    d = _variants_dir() / vid
    if not (d / "meta.json").is_file():
        raise StudioError(f"no variant {vid!r}")
    return d


def meta(vid: str) -> dict:
    return json.loads((_dir(vid) / "meta.json").read_text(encoding="utf-8"))


def _save_meta(vid: str, m: dict) -> None:
    p = _dir(vid) / "meta.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
    tmp.replace(p)


def create(name: str = "", *, note: str = "", base: str = "", group: str = "", origin: str = "person") -> dict:
    """A new variant (a copy of `base`'s files if given). group ties the variants made for one request together."""
    vid = uuid.uuid4().hex[:10]
    d = _variants_dir() / vid
    (d / "files").mkdir(parents=True)
    if base:
        shutil.copytree(_dir(base) / "files", d / "files", dirs_exist_ok=True)
    m = {"id": vid, "name": (name or f"Variant {vid[:4]}")[:80], "note": note[:2000], "group": group or vid,
         "origin": origin, "base": base or None, "created": time.time(), "updated": time.time(), "state": "open",
         "rating": None}
    (d / "meta.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    from bot.studio import log
    log.event("create", vid=vid, name=m["name"], group=m["group"], origin=origin, base=base or None)
    return m


def files(vid: str) -> list[str]:
    root = _dir(vid) / "files"
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())


def read(vid: Optional[str], rel: str) -> str:
    """A UI file as the variant has it (its own copy, else the live one). vid None or "" = live."""
    r = check_rel(rel)
    if vid:
        own = _dir(vid) / "files" / r
        if own.is_file():
            return own.read_text(encoding="utf-8")
    live = code_root() / r
    if not live.is_file():
        raise StudioError(f"{r}: no such file")
    return live.read_text(encoding="utf-8")


def resolve(vid: str, rel: str) -> Optional[Path]:
    """The file to serve for `rel` in a variant's preview: its own copy, else the live one, else None."""
    r = check_rel(rel)
    own = _dir(vid) / "files" / r
    if own.is_file():
        return own
    live = code_root() / r
    return live if live.is_file() else None


def write(vid: str, rel: str, content: str, *, why: str = "edit", source: str = "person") -> dict:
    r = check_rel(rel)
    if len(content.encode("utf-8")) > MAX_FILE:
        raise StudioError(f"{r}: larger than {MAX_FILE >> 20} MB")
    m = meta(vid)
    if m["state"] != "open":
        raise StudioError(f"variant {vid} is {m['state']}; make a new one from it to keep changing it")
    before = read(vid, r) if (code_root() / r).is_file() or (_dir(vid) / "files" / r).is_file() else ""
    p = _dir(vid) / "files" / r
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        p.write_text(content, encoding="utf-8", newline="")
        m["updated"] = time.time()
        _save_meta(vid, m)
    from bot.studio import log
    log.event(why, vid=vid, file=r, source=source, diff=_diff(before, content, r)[:20000])
    return {"variant": vid, "file": r, "bytes": len(content.encode("utf-8"))}


def reset_file(vid: str, rel: str) -> dict:
    """Drop the variant's copy of a file (the live one shows again)."""
    r = check_rel(rel)
    p = _dir(vid) / "files" / r
    if p.is_file():
        p.unlink()
    return {"variant": vid, "file": r, "reset": True}


def _diff(a: str, b: str, rel: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True),
                                        fromfile=f"{rel} (live)", tofile=f"{rel} (variant)"))


def diff(vid: str) -> dict:
    out = {}
    for r in files(vid):
        live = code_root() / r
        out[r] = _diff(live.read_text(encoding="utf-8") if live.is_file() else "", read(vid, r), r)
    return out


def listing() -> list[dict]:
    out = []
    for d in sorted(_variants_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if (d / "meta.json").is_file():
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            m["files"] = files(m["id"])
            out.append(m)
    return out


def rate(vid: str, rating: int, why: str = "") -> dict:
    """The person's verdict on a variant (1..5), for the datasets: which of a group's variants people keep."""
    if not 1 <= int(rating) <= 5:
        raise StudioError("rating is 1..5")
    m = meta(vid)
    m["rating"] = int(rating)
    _save_meta(vid, m)
    from bot.studio import log
    log.event("rate", vid=vid, group=m["group"], rating=int(rating), why=why[:1000])
    return m


def discard(vid: str) -> dict:
    m = meta(vid)
    from bot.studio import log
    log.event("discard", vid=vid, group=m["group"])
    shutil.rmtree(_dir(vid))
    return {"discarded": vid}


def _backups() -> Path:
    d = studio_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


_TWINS = ("bot/dashboard/static/", "desktop-app/ui/")


def _twin(rel: str) -> Optional[str]:
    """A panel script that exists in both UIs (they must stay identical; tests check it): the other copy's path."""
    a, b = _TWINS
    if not rel.endswith("-panel.js"):
        return None
    if rel.startswith(a):
        return b + rel[len(a):]
    if rel.startswith(b):
        return a + rel[len(b):]
    return None


def apply(vid: str) -> dict:
    """Write the variant's files into the live UI. Each live file is backed up first; revert(backup_id) undoes it.
    A panel script that exists in both UIs, identical, is changed in both."""
    m = meta(vid)
    own = files(vid)
    if not own:
        raise StudioError("this variant changes nothing")
    sources = {r: _dir(vid) / "files" / r for r in own}
    for r in own:
        t = _twin(r)
        live_r, live_t = code_root() / r, code_root() / t if t else None
        if t and t not in sources and live_t.is_file() and live_r.is_file() and \
                live_t.read_bytes() == live_r.read_bytes():
            sources[t] = sources[r]
    changed = sorted(sources)
    bid = time.strftime("%Y%m%d-%H%M%S") + "-" + vid
    bdir = _backups() / bid
    record = {"id": bid, "variant": vid, "name": m["name"], "at": time.time(), "files": {}}
    with _lock:
        for r in changed:
            live = code_root() / r
            saved = bdir / r
            saved.parent.mkdir(parents=True, exist_ok=True)
            if live.is_file():
                shutil.copy2(live, saved)
                record["files"][r] = "replaced"
            else:
                record["files"][r] = "created"
        for r in changed:
            live = code_root() / r
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sources[r], live)
        (bdir / "backup.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
        m["state"] = "applied"
        m["applied_backup"] = bid
        _save_meta(vid, m)
    from bot.studio import log
    log.event("apply", vid=vid, group=m["group"], files=changed, backup=bid)
    return {"applied": vid, "files": changed, "backup": bid}


def backups() -> list[dict]:
    out = []
    for d in sorted(_backups().iterdir(), reverse=True):
        f = d / "backup.json"
        if f.is_file():
            out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def revert(backup_id: str) -> dict:
    if not re.fullmatch(r"[\w-]{10,80}", backup_id or ""):
        raise StudioError("bad backup id")
    bdir = _backups() / backup_id
    f = bdir / "backup.json"
    if not f.is_file():
        raise StudioError(f"no backup {backup_id}")
    rec = json.loads(f.read_text(encoding="utf-8"))
    with _lock:
        for r, how in rec["files"].items():
            live = code_root() / check_rel(r)
            if how == "created":
                live.unlink(missing_ok=True)
            else:
                shutil.copy2(bdir / r, live)
    from bot.studio import log
    log.event("revert", vid=rec["variant"], backup=backup_id, files=list(rec["files"]))
    return {"reverted": backup_id, "files": list(rec["files"])}


def version(vid: str) -> float:
    """The newest mtime among the variant's files (the preview reloads when it changes)."""
    root = _dir(vid) / "files"
    stamps = [p.stat().st_mtime for p in root.rglob("*") if p.is_file()]
    return max(stamps, default=0.0)
