"""The content index: what is in every file of every share, so people and agents can find things by name, by words
inside them, by what a picture shows, and by meaning; and duplicates (exact, and near-identical photos).

Per file: kind (image, video, audio, document, text, code, archive, other), size, time, text (plain and code files,
Word/OpenDocument/HTML, the words OCR reads in images), tags (what bot/vision's detector sees in a picture, faces,
QR codes), a perceptual hash (images) and an embedding vector.

Search
    words     SQLite FTS5 over names, paths, text and tags (BM25 ranking)
    meaning   cosine similarity of embedding vectors, all computed on this machine:
                - ABP's own local model runtime when it serves embeddings (ABP_EMBED_URL, Ollama-compatible /api/embed)
                - else a local Ollama with an embedding model (nomic-embed-text, mxbai-embed-large, all-minilm...)
                - else the built-in hashed TF-IDF vectors (words, word pairs, character trigrams): lexical, but
                  tolerant of word forms and typos, and needing no model at all
    auto      both, merged by reciprocal rank fusion

Image analysis runs OpenCV's models with at most two threads, in small batches in the background: it is meant to go
unnoticed, never to load every core.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from bot.fileserver import shares
from bot.fileserver.store import FsError, db

Log = Callable[[str], None]
KINDS = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic", "avif"},
    "video": {"mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv", "flv", "ts"},
    "audio": {"mp3", "flac", "wav", "ogg", "m4a", "aac", "opus", "wma"},
    "document": {"pdf", "doc", "docx", "odt", "rtf", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "epub"},
    "text": {"txt", "md", "csv", "tsv", "log", "json", "xml", "yaml", "yml", "ini", "toml", "html", "htm", "srt", "vtt"},
    "code": {"py", "js", "ts", "tsx", "jsx", "java", "kt", "kts", "rs", "go", "c", "h", "cpp", "hpp", "cs", "rb", "php",
             "sh", "ps1", "sql", "lua", "swift", "css", "scss", "nix"},
    "archive": {"zip", "7z", "rar", "tar", "gz", "bz2", "xz", "zst", "iso"},
}
TEXT_MAX = 200_000
DIM = 1024


def kind_of(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for k, exts in KINDS.items():
        if ext in exts:
            return k
    return "other"


def _con():
    con = db("index")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files(share TEXT, path TEXT, size INT, mtime_ns INT, kind TEXT, phash INT, sha256 TEXT,
                                         tags TEXT, chars INT, vec BLOB, vec_backend TEXT, indexed INT, error TEXT,
                                         PRIMARY KEY(share, path));
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(share UNINDEXED, path, name, text, tags, tokenize='unicode61 remove_diacritics 2');
        CREATE INDEX IF NOT EXISTS files_size ON files(size);
    """)
    return con


# ---- extraction ----------------------------------------------------------------------------------------------------- #

def _strip_xml(data: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", data)).strip()


def extract_text(path: Path, kind: str) -> str:
    ext = path.suffix.lower().lstrip(".")
    try:
        if kind in ("text", "code"):
            raw = path.open("rb").read(TEXT_MAX)
            if b"\0" in raw[:4096]:
                return ""
            text = raw.decode("utf-8", errors="replace")
            return _strip_xml(text) if ext in ("html", "htm", "xml") else text
        if ext in ("docx", "pptx", "xlsx", "odt", "odp", "ods", "epub"):
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n in ("word/document.xml", "content.xml") or
                         n.startswith(("ppt/slides/slide", "xl/sharedStrings")) or n.endswith((".xhtml", ".html"))]
                return _strip_xml(" ".join(z.read(n).decode("utf-8", errors="replace") for n in names[:200]))[:TEXT_MAX]
    except (OSError, zipfile.BadZipFile, KeyError):
        return ""
    return ""


def phash(path: Path) -> Optional[int]:
    """64-bit DCT perceptual hash (robust to resizing, recompression, small edits)."""
    import cv2
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    small = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(small)[:8, :8].flatten()
    med = np.median(d[1:])
    bits = 0
    for i, v in enumerate(d):
        if v > med:
            bits |= 1 << i
    return bits - (1 << 64) if bits >= 1 << 63 else bits        # SQLite integers are signed


def describe_image(path: Path) -> tuple[list[str], str]:
    """Tags (objects, 'face', 'qr') and any text, from bot/vision on this machine, with OpenCV limited to two threads."""
    import cv2
    cv2.setNumThreads(2)
    from bot.vision import service
    res = service.analyze(str(path), ["objects", "faces", "codes", "text"], min_score=0.5, annotate=False)
    tags = sorted({o.get("label", "") for o in res.get("objects") or [] if o.get("label")})
    if res.get("faces"):
        tags.append("face" if len(res["faces"]) == 1 else "faces")
    if res.get("codes"):
        tags.append("qr")
    text = " ".join(c.get("text", "") for c in res.get("codes") or []) + " " + (res.get("text_joined") or "")
    return tags, text.strip()


# ---- embeddings ----------------------------------------------------------------------------------------------------- #

_backend_cache: dict = {}


def _http_embed(url: str, model: str, texts: list[str]) -> Optional[np.ndarray]:
    import httpx
    try:
        r = httpx.post(url.rstrip("/") + "/api/embed", json={"model": model, "input": texts}, timeout=120)
        r.raise_for_status()
        v = np.asarray(r.json()["embeddings"], dtype=np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    except Exception:  # noqa: BLE001 - not running / no such model: fall back
        return None


def embed_backend() -> tuple[str, Callable[[list[str]], np.ndarray]]:
    """The best local embedder available: ABP's own runtime, a local Ollama embedding model, or hashed TF-IDF."""
    hit = _backend_cache.get("b")
    if hit and time.time() - hit[2] < 300:
        return hit[0], hit[1]
    import httpx
    choice = None
    url = os.environ.get("ABP_EMBED_URL", "").strip()
    model = os.environ.get("ABP_EMBED_MODEL", "nomic-embed-text")
    if url and _http_embed(url, model, ["probe"]) is not None:
        choice = (f"abp:{model}", lambda t, u=url, m=model: _http_embed(u, m, t))
    if not choice:
        try:
            tags = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2).json().get("models", [])
            emb = [m["name"] for m in tags if any(k in m["name"] for k in ("embed", "minilm", "bge", "e5", "gte"))]
            if emb and _http_embed("http://127.0.0.1:11434", emb[0], ["probe"]) is not None:
                choice = (f"ollama:{emb[0]}", lambda t, m=emb[0]: _http_embed("http://127.0.0.1:11434", m, t))
        except Exception:  # noqa: BLE001
            pass
    if not choice:
        choice = ("hash-tfidf", hash_vectors)
    _backend_cache["b"] = (choice[0], choice[1], time.time())
    return choice


_WORD = re.compile(r"[\w']+", re.UNICODE)


def _features(text: str) -> dict[str, float]:
    words = [w.lower() for w in _WORD.findall(text)][:20000]
    feats: dict[str, float] = {}
    for w in words:
        feats["w:" + w] = feats.get("w:" + w, 0) + 1
        padded = f"#{w}#"
        for i in range(len(padded) - 2):
            g = "c:" + padded[i:i + 3]
            feats[g] = feats.get(g, 0) + 0.3
    for a, b in zip(words, words[1:]):
        k = f"b:{a} {b}"
        feats[k] = feats.get(k, 0) + 0.7
    return feats


def hash_vectors(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for f, c in _features(t).items():
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            out[i, h % DIM] += (1.0 if (h >> 63) & 1 else -1.0) * (1 + math.log(c))
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


def _doc_text(path: str, text: str, tags: list[str]) -> str:
    name = path.rsplit("/", 1)[-1]
    words = re.sub(r"[_\-.]+", " ", name.rsplit(".", 1)[0])
    return f"{words} {words} {' '.join(tags)} {' '.join(tags)} {text[:4000]}"


# ---- indexing ------------------------------------------------------------------------------------------------------- #

def index_share(name: str, log: Log = lambda m: None, describe_images: bool = True, budget_s: float = 0,
                hash_dups: bool = True) -> dict:
    """Bring a share's index up to date. budget_s > 0 stops after that long (the background job does a slice at a
    time); the next call carries on."""
    s = shares.get(name)
    con = _con()
    t0 = time.time()
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime_ns FROM files WHERE share=?", (s["name"],))}
    seen = set()
    out = {"indexed": 0, "removed": 0, "unchanged": 0, "errors": 0, "complete": True}
    backend, embed = embed_backend()
    batch: list[tuple] = []

    def flush():
        if not batch:
            return
        vecs = embed([b[-1] for b in batch])
        with con:
            for (path, size, mtime, kind, ph, tags, text, err, _doc), v in zip(batch, vecs):
                con.execute("INSERT OR REPLACE INTO files(share, path, size, mtime_ns, kind, phash, sha256, tags, chars, vec, "
                            "vec_backend, indexed, error) VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,?)",
                            (s["name"], path, size, mtime, kind, ph, json.dumps(tags), len(text), v.astype(np.float32).tobytes(),
                             backend, int(time.time()), err))
                con.execute("DELETE FROM fts WHERE share=? AND path=?", (s["name"], path))
                con.execute("INSERT INTO fts(share, path, name, text, tags) VALUES(?,?,?,?,?)",
                            (s["name"], path, path.rsplit("/", 1)[-1], text, " ".join(tags)))
        batch.clear()

    for rel, real in shares.walk(s):
        seen.add(rel)
        try:
            st = real.stat()
        except OSError:
            continue
        if known.get(rel) == (st.st_size, st.st_mtime_ns):
            out["unchanged"] += 1
            continue
        if budget_s and time.time() - t0 > budget_s:
            out["complete"] = False
            break
        kind = kind_of(rel)
        tags, text, ph, err = [], "", None, None
        try:
            text = extract_text(real, kind)
            if kind == "image":
                ph = phash(real)
                if describe_images and st.st_size < 40 << 20:
                    tags, img_text = describe_image(real)
                    text = (text + " " + img_text).strip()
        except Exception as e:  # noqa: BLE001 - one unreadable file never stops the index
            err = f"{type(e).__name__}: {e}"[:300]
            out["errors"] += 1
        batch.append((rel, st.st_size, st.st_mtime_ns, kind, ph, tags, text, err, _doc_text(rel, text, tags)))
        out["indexed"] += 1
        if len(batch) >= 32:
            flush()
    flush()
    if out["complete"]:
        gone = [p for p in known if p not in seen]
        with con:
            for p in gone:
                con.execute("DELETE FROM files WHERE share=? AND path=?", (s["name"], p))
                con.execute("DELETE FROM fts WHERE share=? AND path=?", (s["name"], p))
        out["removed"] = len(gone)
    if hash_dups and out["complete"]:
        _hash_same_size(con, s)
    con.close()
    out["backend"] = backend
    out["seconds"] = round(time.time() - t0, 2)
    log(f"index {s['name']}: {out}")
    return out


def _hash_same_size(con, s: dict) -> None:
    """SHA-256 only for files that share their size with another file (the only ones that can be exact duplicates)."""
    rows = con.execute("SELECT share, path, size FROM files WHERE sha256 IS NULL AND size > 0 AND size IN "
                       "(SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1)").fetchall()
    for share_name, path, _size in rows:
        try:
            sh = shares.get(share_name)
            hit = shares.locate(sh, path)
            if not hit:
                continue
            h = hashlib.sha256()
            with open(hit[1], "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            con.execute("UPDATE files SET sha256=? WHERE share=? AND path=?", (h.hexdigest(), share_name, path))
        except (OSError, FsError):
            continue
    con.commit()


# ---- search, duplicates ------------------------------------------------------------------------------------------- #

def _fts_query(q: str) -> str:
    words = [w for w in _WORD.findall(q) if w]
    return " ".join(f'"{w}"*' for w in words) if words else '""'


def search(q: str, shares: Optional[list[str]] = None, mode: str = "auto", limit: int = 50, kind: str = "") -> list[dict]:
    con = _con()
    allowed = set(shares) if shares is not None else None
    ranked: dict[str, list[tuple[str, str]]] = {}
    if mode in ("auto", "words"):
        rows = con.execute("SELECT share, path, bm25(fts, 0, 2.0, 4.0, 1.0, 3.0) AS r FROM fts WHERE fts MATCH ? ORDER BY r LIMIT ?",
                           (_fts_query(q), limit * 4)).fetchall()
        ranked["words"] = [(r[0], r[1]) for r in rows if allowed is None or r[0] in allowed]
    if mode in ("auto", "meaning"):
        backend, embed = embed_backend()
        qv = embed([q])[0]
        rows = con.execute("SELECT share, path, vec FROM files WHERE vec_backend=?", (backend,)).fetchall()
        rows = [r for r in rows if allowed is None or r[0] in allowed]
        if rows:
            m = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            sims = m @ qv
            order = np.argsort(-sims)[: limit * 4]
            ranked["meaning"] = [(rows[i][0], rows[i][1]) for i in order if sims[i] > 0.05]
    score: dict[tuple[str, str], float] = {}
    for lst in ranked.values():
        for rank, key in enumerate(lst):
            score[key] = score.get(key, 0) + 1 / (60 + rank)
    best = sorted(score, key=lambda k: -score[k])
    out = []
    for share_name, path in best:
        r = con.execute("SELECT size, mtime_ns, kind, tags FROM files WHERE share=? AND path=?", (share_name, path)).fetchone()
        if not r or (kind and r[2] != kind):
            continue
        snippet = ""
        if "words" in ranked:
            sn = con.execute("SELECT snippet(fts, 3, '[', ']', '…', 12) FROM fts WHERE fts MATCH ? AND share=? AND path=?",
                             (_fts_query(q), share_name, path)).fetchone()
            snippet = sn[0] if sn else ""
        out.append({"share": share_name, "path": path, "size": r[0], "mtime": r[1] / 1e9, "kind": r[2],
                    "tags": json.loads(r[3] or "[]"), "snippet": snippet, "score": round(score[(share_name, path)], 5)})
        if len(out) >= limit:
            break
    con.close()
    return out


def duplicates(shares_: Optional[list[str]] = None, near: bool = True, max_distance: int = 6) -> dict:
    """Exact duplicates (same SHA-256) and, for images, near-duplicates (perceptual hashes within max_distance bits:
    the same photo resized, recompressed, lightly edited)."""
    con = _con()
    allowed = set(shares_) if shares_ else None
    exact: dict[str, list[dict]] = {}
    for share_name, path, size, sha in con.execute("SELECT share, path, size, sha256 FROM files WHERE sha256 IS NOT NULL"):
        if allowed is None or share_name in allowed:
            exact.setdefault(sha, []).append({"share": share_name, "path": path, "size": size})
    groups = [{"kind": "exact", "files": v, "wasted": v[0]["size"] * (len(v) - 1)} for v in exact.values() if len(v) > 1]
    if near:
        imgs = [(r[0], r[1], r[2], r[3] & 0xFFFFFFFFFFFFFFFF) for r in con.execute(
            "SELECT share, path, size, phash FROM files WHERE phash IS NOT NULL") if allowed is None or r[0] in allowed]
        buckets: dict[tuple[int, int], list[int]] = {}
        for i, (_s, _p, _z, h) in enumerate(imgs):
            for band in range(4):                         # a near-duplicate shares at least one 16-bit band
                buckets.setdefault((band, (h >> (16 * band)) & 0xFFFF), []).append(i)
        parent = list(range(len(imgs)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for idxs in buckets.values():
            for a in range(len(idxs)):
                for b in range(a + 1, len(idxs)):
                    i, j = idxs[a], idxs[b]
                    if bin(imgs[i][3] ^ imgs[j][3]).count("1") <= max_distance:
                        parent[find(i)] = find(j)
        clusters: dict[int, list[int]] = {}
        for i in range(len(imgs)):
            clusters.setdefault(find(i), []).append(i)
        exact_paths = {(f["share"], f["path"]) for g in groups for f in g["files"]}
        for members in clusters.values():
            if len(members) > 1:
                files = [{"share": imgs[i][0], "path": imgs[i][1], "size": imgs[i][2]} for i in members]
                if all((f["share"], f["path"]) in exact_paths for f in files):
                    continue
                files.sort(key=lambda f: -f["size"])
                groups.append({"kind": "similar images", "files": files, "wasted": sum(f["size"] for f in files[1:])})
    con.close()
    groups.sort(key=lambda g: -g["wasted"])
    return {"groups": groups, "wasted_bytes": sum(g["wasted"] for g in groups)}


def stats() -> dict:
    con = _con()
    rows = con.execute("SELECT share, kind, COUNT(*), COALESCE(SUM(size),0) FROM files GROUP BY share, kind").fetchall()
    backend = con.execute("SELECT vec_backend, COUNT(*) FROM files GROUP BY vec_backend").fetchall()
    errs = con.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
    con.close()
    by_share: dict[str, dict] = {}
    for share_name, kind, n, size in rows:
        d = by_share.setdefault(share_name, {"files": 0, "bytes": 0, "kinds": {}})
        d["files"] += n
        d["bytes"] += size
        d["kinds"][kind] = {"files": n, "bytes": size}
    return {"shares": by_share, "embeddings": dict(backend), "errors": errs, "embedder": embed_backend()[0]}
