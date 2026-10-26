"""The video model registry: bot/video/models.json names each model's files with their size, SHA-256 and a pinned
download URL. Files live in `video.models_dir` (default data/video/models); a model is usable only when every file is
there and its SHA-256 matches.

Hashing a 6 GB file takes a while, so a verified hash is remembered (data/video/verified.json) with the file's size and
modification time; a changed file is hashed again. `download` fetches missing files on demand (to <name>.part, then
verified and renamed), after checking that the disk holding models_dir has room for them."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from bot.video import store

SPEC = Path(__file__).with_name("models.json")
HEADROOM_BYTES = 2 << 30
_lock = threading.Lock()


def models() -> list[dict]:
    return list(json.loads(SPEC.read_text(encoding="utf-8"))["models"])


def get(model_id: str) -> dict:
    for m in models():
        if m["id"] == model_id:
            return m
    raise ValueError(f"no model {model_id!r} in the registry")


def models_dir() -> Path:
    from bot.video import jobs
    d = str(jobs.settings().get("models_dir") or "").strip()
    p = Path(d).expanduser() if d else store.root() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_path() -> Path:
    return store.root() / "verified.json"


def _cache() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(p: Path, sha: str) -> None:
    with _lock:
        c = _cache()
        st = p.stat()
        c[str(p)] = {"bytes": st.st_size, "mtime": st.st_mtime, "sha256": sha}
        _cache_path().write_text(json.dumps(c, indent=1), encoding="utf-8")


def _known_sha(p: Path) -> Optional[str]:
    e = _cache().get(str(p))
    if not e:
        return None
    st = p.stat()
    return e["sha256"] if e.get("bytes") == st.st_size and e.get("mtime") == st.st_mtime else None


def file_state(f: dict, base: Path, *, hash_now: bool = False) -> dict:
    p = base / f["name"]
    out = {"role": f["role"], "name": f["name"], "bytes": f["bytes"], "path": str(p)}
    if not p.is_file():
        return out | {"state": "missing"}
    if p.stat().st_size != f["bytes"]:
        return out | {"state": "wrong_size", "on_disk": p.stat().st_size}
    sha = _known_sha(p)
    if sha is None and hash_now:
        sha = store.sha256_file(p)
        _remember(p, sha)
    if sha is None:
        return out | {"state": "unverified"}
    return out | {"state": "ok" if sha == f["sha256"] else "sha_mismatch"}


def status(model_id: str | None = None, *, hash_now: bool = False) -> list[dict]:
    base = models_dir()
    out = []
    for m in models():
        if model_id and m["id"] != model_id:
            continue
        files = [file_state(f, base, hash_now=hash_now) for f in m["files"]]
        states = {f["state"] for f in files}
        state = "ok" if states == {"ok"} else "missing" if "missing" in states else \
                "bad" if states & {"wrong_size", "sha_mismatch"} else "unverified"
        out.append({"id": m["id"], "title": m.get("title", ""), "license": m.get("license", ""),
                    "vram_gb": m.get("vram_gb"), "bytes": sum(f["bytes"] for f in m["files"]), "state": state,
                    "files": files, "dir": str(base)})
    return out


def verify(model_id: str) -> dict:
    """Hash every file of the model (slow the first time) and say whether it is usable."""
    get(model_id)
    return status(model_id, hash_now=True)[0]


def resolve(model_id: str, *, hash_now: bool = True) -> dict[str, str]:
    """{role: path} of a usable model, verified; raises ValueError naming what is missing or wrong."""
    st = status(model_id, hash_now=hash_now)
    if not st:
        raise ValueError(f"no model {model_id!r} in the registry")
    bad = [f"{f['name']} ({f['state']})" for f in st[0]["files"] if f["state"] != "ok"]
    if bad:
        raise ValueError(f"model {model_id} is not ready: {', '.join(bad)}; download or verify it first")
    return {f["role"]: f["path"] for f in st[0]["files"]}


def disk(model_id: str) -> dict:
    """Free space on the disk holding models_dir against what the missing files need."""
    base = models_dir()
    need = sum(f["bytes"] for f in get(model_id)["files"] if not (base / f["name"]).is_file())
    free = shutil.disk_usage(base).free
    return {"dir": str(base), "free_bytes": free, "need_bytes": need, "fits": free >= need + HEADROOM_BYTES}


def download(model_id: str, *, progress: Callable[[str, int, int], Any] | None = None,
             fetch: Callable[[str, Path, Callable[[int], Any]], None] | None = None) -> dict:
    """Fetch the model's missing files and verify each one. `fetch(url, dest, on_bytes)` is replaceable (tests)."""
    m = get(model_id)
    d = disk(model_id)
    if not d["fits"]:
        raise ValueError(f"{d['dir']} has {d['free_bytes'] >> 30} GB free; {model_id} needs {d['need_bytes'] >> 30} GB more "
                         "plus headroom; choose another models folder in the Video Studio settings")
    base = models_dir()
    fetch = fetch or _http_fetch
    for f in m["files"]:
        dest = base / f["name"]
        if dest.is_file() and dest.stat().st_size == f["bytes"]:
            continue
        part = dest.with_name(dest.name + ".part")
        fetch(f["url"], part, lambda n, f=f: progress and progress(f["name"], n, f["bytes"]))
        sha = store.sha256_file(part)
        if sha != f["sha256"]:
            part.unlink(missing_ok=True)
            raise ValueError(f"{f['name']}: SHA-256 {sha[:12]}... does not match the registry ({f['sha256'][:12]}...)")
        part.replace(dest)
        _remember(dest, sha)
    return verify(model_id)


def _http_fetch(url: str, dest: Path, on_bytes: Callable[[int], Any]) -> None:
    import httpx
    done = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0)) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                on_bytes(done)
