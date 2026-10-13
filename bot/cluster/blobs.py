"""The cluster's content-addressed blob store: job inputs and outputs, moved between nodes by hash.

A blob is a byte string named by the SHA-256 of its content, stored in chunks (8 MiB) that each have their own
hash. The same bytes are therefore stored, advertised and transferred once however many jobs use them:

    put_file / put_bytes   store content, get its hash                      (the submitter, for a job's inputs)
    ensure(hash, sources)  have it here: already stored, else fetched from the first node that holds it
    materialize(hash, p)   copy it into a job's folder
    manifest / read_chunk  what a peer asks for, over the cluster API (GET /api/cluster/blobs/{hash}[/chunks/{i}])

A fetch is resumable (verified chunks of an interrupted fetch are kept and not asked for again), checks every chunk
against the manifest's chunk hash, and the whole file against the hash it was asked for, so a peer that lies or a
disk that flips a bit costs a refetch or a refusal, never a wrong input. Only hashes travel: no peer ever supplies a
path. The store has a size cap and evicts the least recently used blob that no running job has pinned.

When CacheIt runs, single-chunk blobs are also mirrored there and read back from there; the store works alone.
Transport is a callable `remote(node, method, path) -> dict` (the default goes through bot.peers.proxy, so peers
authenticate exactly as for the rest of the cluster); TransferDaemon can replace it later without touching callers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

CHUNK_BYTES = 8 * 1024 * 1024
MIN_CHUNK_BYTES, MAX_CHUNK_BYTES = 64 * 1024, 64 * 1024 * 1024
DEFAULT_CAP_GB = 20.0
MAX_BLOB_BYTES = 1 << 40                  # nothing larger is ever accepted from a peer's manifest
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ADVERTISE_PREFIX = 12                     # hex characters of each held hash a node reports (48 bits)
ADVERTISE_MAX = 1024                      # most recently used blobs reported
MIRROR_MAX_BYTES = CHUNK_BYTES            # CacheIt mirrors only single-chunk blobs
FETCH_RETRIES = 3

Remote = Callable[[str, str, str], Any]


class BlobError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 400) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def valid_hash(h: Any) -> str:
    h = str(h or "").lower()
    if not HASH_RE.match(h):
        raise BlobError("a blob is named by the 64 hex characters of its SHA-256", code="invalid")
    return h


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def peer_remote(node: str, method: str, path: str) -> Any:
    """The default transport: the cluster API of a linked server, reached the way every cluster call is.
    Runs on a worker thread (the executor's), so it may own an event loop for the call."""
    import asyncio

    from bot import peers
    from bot.cluster import membership
    row = membership.peer_row(node)
    if row is None:
        raise BlobError(f"{node} is not a linked server", code="no_peer", status=404)
    return asyncio.run(peers.proxy(row, method, "/api/cluster" + path, None, timeout=300))


class BlobStore:
    def __init__(self, root: Path, cap_bytes: Optional[int] = None) -> None:
        self.root = Path(root)
        self._cap = cap_bytes
        self._lock = threading.RLock()
        self._index: dict[str, dict] = {}                 # hash -> {size, chunk, chunks, used}
        self._pins: dict[str, set] = {}                   # hash -> owners that need it kept
        self._fetching: dict[str, threading.Lock] = {}
        self.stats = {"hits": 0, "misses": 0, "fetches": 0, "chunks_fetched": 0, "chunks_refetched": 0,
                      "bytes_fetched": 0, "chunks_served": 0, "bytes_served": 0, "evictions": 0, "cache_hits": 0}
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "partial").mkdir(parents=True, exist_ok=True)
        self._scan()

    # ---- layout ---------------------------------------------------------------------------------------------------
    def _data(self, h: str) -> Path:
        return self.root / "objects" / h[:2] / h

    def _meta(self, h: str) -> Path:
        return self.root / "objects" / h[:2] / (h + ".json")

    def _scan(self) -> None:
        for m in (self.root / "objects").glob("??/*.json"):
            h = m.stem
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
                if HASH_RE.match(h) and self._data(h).is_file() and self._data(h).stat().st_size == meta["size"]:
                    self._index[h] = {"size": int(meta["size"]), "chunk": int(meta["chunk"]),
                                      "chunks": list(meta["chunks"]), "used": float(meta.get("used") or 0)}
            except (OSError, ValueError, KeyError):
                continue

    def cap_bytes(self) -> int:
        if self._cap is not None:
            return int(self._cap)
        try:
            from bot.config import config
            gb = float((((config.current or {}).get("cluster") or {}).get("blobs") or {}).get("max_gb") or DEFAULT_CAP_GB)
        except Exception:  # noqa: BLE001
            gb = DEFAULT_CAP_GB
        return int(gb * (1 << 30))

    # ---- reading --------------------------------------------------------------------------------------------------
    def has(self, h: str) -> bool:
        with self._lock:
            return valid_hash(h) in self._index

    def manifest(self, h: str) -> dict:
        h = valid_hash(h)
        with self._lock:
            e = self._index.get(h)
            if e is None:
                raise BlobError(f"no blob {h[:12]}", code="not_found", status=404)
            return {"hash": h, "size": e["size"], "chunk_bytes": e["chunk"], "chunks": list(e["chunks"])}

    def read_chunk(self, h: str, index: int) -> bytes:
        m = self.manifest(h)
        if not 0 <= index < len(m["chunks"]):
            raise BlobError("no such chunk", code="not_found", status=404)
        with open(self._data(m["hash"]), "rb") as f:
            f.seek(index * m["chunk_bytes"])
            data = f.read(m["chunk_bytes"])
        if _sha(data) != m["chunks"][index]:                 # the disk changed under us: do not serve it
            self.drop(m["hash"])
            raise BlobError("the stored blob is damaged and was dropped", code="damaged", status=410)
        with self._lock:
            self.stats["chunks_served"] += 1
            self.stats["bytes_served"] += len(data)
            self._touch(m["hash"])
        return data

    def chunk_doc(self, h: str, index: int) -> dict:
        """One chunk as the API sends it (JSON: base64 data, so it crosses the peer proxy like every cluster call)."""
        data = self.read_chunk(h, index)
        return {"hash": valid_hash(h), "index": index, "chunk_hash": _sha(data), "size": len(data),
                "data": base64.b64encode(data).decode("ascii")}

    def path(self, h: str) -> Path:
        h = valid_hash(h)
        with self._lock:
            if h not in self._index:
                raise BlobError(f"no blob {h[:12]}", code="not_found", status=404)
            self._touch(h)
        return self._data(h)

    def materialize(self, h: str, dest: Path) -> Path:
        """Copy a blob to `dest` (a copy, never a link: a job must not be able to damage the store)."""
        src = self.path(h)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        return dest

    def _touch(self, h: str) -> None:
        e = self._index.get(h)
        if e is not None:
            e["used"] = time.time()

    # ---- writing --------------------------------------------------------------------------------------------------
    def put_file(self, path: Path, chunk_bytes: int = CHUNK_BYTES) -> dict:
        """Store a file (one streaming pass: whole hash, chunk hashes and the copy). Returns {hash, size}."""
        path = Path(path)
        if not path.is_file():
            raise BlobError(f"{path.name} is not a file", code="invalid")
        size = path.stat().st_size
        self._check_size(size)
        tmp = self.root / "partial" / f"put-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        whole, chunks = hashlib.sha256(), []
        try:
            with open(path, "rb") as src, open(tmp, "wb") as out:
                while True:
                    data = src.read(chunk_bytes)
                    if not data:
                        break
                    whole.update(data)
                    chunks.append(_sha(data))
                    out.write(data)
            return self._commit(tmp, whole.hexdigest(), chunk_bytes, chunks)
        finally:
            tmp.unlink(missing_ok=True)

    def put_bytes(self, data: bytes, chunk_bytes: int = CHUNK_BYTES) -> dict:
        self._check_size(len(data))
        tmp = self.root / "partial" / f"put-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        try:
            tmp.write_bytes(data)
            chunks = [_sha(data[i:i + chunk_bytes]) for i in range(0, len(data), chunk_bytes)]
            return self._commit(tmp, _sha(data), chunk_bytes, chunks)
        finally:
            tmp.unlink(missing_ok=True)

    def _check_size(self, size: int) -> None:
        if size > self.cap_bytes():
            raise BlobError(f"{size} bytes is more than this node's blob store holds ({self.cap_bytes()})", code="too_big",
                            status=413)

    def _commit(self, tmp: Path, h: str, chunk_bytes: int, chunks: list) -> dict:
        size = tmp.stat().st_size
        with self._lock:
            if h not in self._index:
                dest = self._data(h)
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, dest)
                meta = {"size": size, "chunk": chunk_bytes, "chunks": chunks, "used": time.time()}
                self._meta(h).write_text(json.dumps(meta), encoding="utf-8")
                self._index[h] = meta
                self._evict(keep=h)
            else:
                self._touch(h)
        self._mirror(h)
        return {"hash": h, "size": size}

    def drop(self, h: str) -> None:
        with self._lock:
            self._index.pop(h, None)
            self._data(h).unlink(missing_ok=True)
            self._meta(h).unlink(missing_ok=True)

    # ---- pins and eviction ----------------------------------------------------------------------------------------
    def pin(self, h: str, owner: str) -> None:
        with self._lock:
            self._pins.setdefault(valid_hash(h), set()).add(owner)

    def unpin_owner(self, owner: str) -> None:
        with self._lock:
            for h in list(self._pins):
                self._pins[h].discard(owner)
                if not self._pins[h]:
                    del self._pins[h]
            self._evict()

    def pinned(self, h: str) -> bool:
        with self._lock:
            return bool(self._pins.get(h))

    def total_bytes(self) -> int:
        with self._lock:
            return sum(e["size"] for e in self._index.values())

    def _evict(self, keep: Optional[str] = None) -> None:
        """Drop least-recently-used blobs until the store fits its cap. Pinned blobs (a running job uses them) and the
        blob just stored are never dropped, so a store full of pinned blobs runs over its cap rather than break a job."""
        cap = self.cap_bytes()
        with self._lock:
            total = self.total_bytes()
            for h, e in sorted(self._index.items(), key=lambda kv: kv[1]["used"]):
                if total <= cap:
                    break
                if h == keep or self._pins.get(h):
                    continue
                total -= e["size"]
                self.drop(h)
                self.stats["evictions"] += 1

    # ---- CacheIt (optional) ---------------------------------------------------------------------------------------
    def _mirror(self, h: str) -> None:
        try:
            from bot import cache
            e = self._index.get(h)
            if e and e["size"] <= MIRROR_MAX_BYTES and cache.available():
                cache.put("cluster-blob", h, self._data(h).read_bytes())
        except Exception:  # noqa: BLE001  (the cache can only help)
            pass

    def _from_cache(self, h: str) -> bool:
        try:
            from bot import cache
            if not cache.available():
                return False
            data = cache.get("cluster-blob", h)
        except Exception:  # noqa: BLE001
            return False
        if data is None or _sha(data) != h:
            return False
        self.put_bytes(data)
        self.stats["cache_hits"] += 1
        return True

    # ---- getting a blob from another node -------------------------------------------------------------------------
    def ensure(self, h: str, sources: list, remote: Optional[Remote] = None, *, size: Optional[int] = None) -> str:
        """Make sure blob `h` is stored here, fetching it from the first of `sources` (node names) that has it."""
        h = valid_hash(h)
        with self._lock:
            lock = self._fetching.setdefault(h, threading.Lock())
        with lock:                                        # a second job wanting it waits for the first fetch
            if self.has(h):
                with self._lock:
                    self.stats["hits"] += 1
                    self._touch(h)
                return h
            with self._lock:
                self.stats["misses"] += 1
            if self._from_cache(h):
                return h
            remote = remote or peer_remote
            errors = []
            for node in sources:
                try:
                    self._fetch_from(h, node, remote, size)
                    return h
                except BlobError as e:
                    errors.append(f"{node}: {e}")
                except Exception as e:  # noqa: BLE001  (an unreachable peer)
                    errors.append(f"{node}: {str(e)[:200]}")
            raise BlobError(f"blob {h[:12]} could not be fetched ({'; '.join(errors) or 'no node holds it'})",
                            code="unavailable", status=502)

    def _fetch_from(self, h: str, node: str, remote: Remote, size: Optional[int]) -> None:
        m = remote(node, "GET", f"/blobs/{h}")
        if not isinstance(m, dict) or m.get("hash") != h:
            raise BlobError("the node answered for a different blob")
        chunk = int(m.get("chunk_bytes") or 0)
        total = int(m.get("size", -1))
        hashes = m.get("chunks")
        if not (0 < chunk <= MAX_CHUNK_BYTES and (chunk >= MIN_CHUNK_BYTES or total <= chunk)) or not 0 <= total <= MAX_BLOB_BYTES:
            raise BlobError("the node's manifest is out of bounds")
        if size is not None and total != size:
            raise BlobError("the node's blob is not the size the job was submitted with")
        if (not isinstance(hashes, list) or len(hashes) != -(-total // chunk) or
                not all(isinstance(x, str) and HASH_RE.match(x) for x in hashes)):
            raise BlobError("the node's manifest is inconsistent")
        self._check_size(total)
        part, state = self.root / "partial" / (h + ".part"), self.root / "partial" / (h + ".json")
        done: set = set()
        if state.is_file() and part.is_file():                # resume: keep only chunks this manifest agrees with
            try:
                old = json.loads(state.read_text(encoding="utf-8"))
                if old.get("chunks") == hashes:
                    done = set(int(i) for i in old.get("done") or [])
            except (OSError, ValueError):
                done = set()
        if not done and part.exists():
            part.unlink()
        if not part.exists():
            with open(part, "wb") as f:
                f.truncate(total)
        with self._lock:
            self.stats["fetches"] += 1
        for i in range(len(hashes)):
            if i in done:
                continue
            data = self._fetch_chunk(h, node, remote, i, hashes[i], min(chunk, total - i * chunk))
            with open(part, "r+b") as f:
                f.seek(i * chunk)
                f.write(data)
            done.add(i)
            state.write_text(json.dumps({"chunks": hashes, "done": sorted(done)}), encoding="utf-8")
        whole = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                whole.update(block)
        if whole.hexdigest() != h:                            # a manifest that lied: nothing of it can be trusted
            part.unlink(missing_ok=True)
            state.unlink(missing_ok=True)
            raise BlobError("the assembled blob does not match its hash; discarded", code="corrupt", status=502)
        self._commit(part, h, chunk, list(hashes))
        part.unlink(missing_ok=True)
        state.unlink(missing_ok=True)

    def _fetch_chunk(self, h: str, node: str, remote: Remote, i: int, want: str, length: int) -> bytes:
        last = "no answer"
        for _ in range(FETCH_RETRIES):
            try:
                doc = remote(node, "GET", f"/blobs/{h}/chunks/{i}")
                data = base64.b64decode(str((doc or {}).get("data") or ""), validate=True)
                if len(data) == length and _sha(data) == want:
                    with self._lock:
                        self.stats["chunks_fetched"] += 1
                        self.stats["bytes_fetched"] += len(data)
                    return data
                last = "the chunk does not match its hash"
            except BlobError as e:
                last = str(e)
            except Exception as e:  # noqa: BLE001 (a dropped connection, bad base64)
                last = str(e)[:200]
            with self._lock:
                self.stats["chunks_refetched"] += 1
        raise BlobError(f"chunk {i} of {h[:12]}: {last} (after {FETCH_RETRIES} tries)", code="corrupt", status=502)

    # ---- what the status shows, and what the scheduler reads -----------------------------------------------------
    def summary(self) -> dict:
        with self._lock:
            return {"count": len(self._index), "bytes": self.total_bytes(), "cap_bytes": self.cap_bytes(),
                    "pinned": sum(1 for h in self._pins if self._pins[h]), **self.stats}

    def advertised(self) -> list:
        """Short prefixes of the most recently used blobs held: enough for the scheduler to find who has an input."""
        with self._lock:
            newest = sorted(self._index.items(), key=lambda kv: kv[1]["used"], reverse=True)[:ADVERTISE_MAX]
            return [h[:ADVERTISE_PREFIX] for h, _ in newest]


def holds(advertised: Any, h: str) -> bool:
    return isinstance(advertised, (list, tuple, set)) and h[:ADVERTISE_PREFIX] in advertised


_default: Optional[BlobStore] = None
_default_lock = threading.Lock()


def get_store() -> BlobStore:
    global _default
    with _default_lock:
        if _default is None:
            from bot.envfile import PROJECT_ROOT
            _default = BlobStore(Path(PROJECT_ROOT) / "data" / "cluster" / "blobs")
        return _default
