"""Tier backends: where an item's body lives. A backend stores opaque bytes by key.

`TierBackend` is the interface; a CacheIt N-tier store (or any other) can replace the file tiers by implementing it
and being named in `make_backend`. `volatile` backends (RAM) are caches: the vault keeps the durable copy in a
non-volatile tier and the RAM copy can be dropped at any time.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional


class TierBackend:
    name = ""
    volatile = False

    def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get(self, key: str) -> Optional[bytes]:
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        raise NotImplementedError

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def keys(self) -> list[str]:
        raise NotImplementedError

    def clear_partial(self) -> int:
        return 0


class RamBackend(TierBackend):
    """In-process dict. When bot/cache.py (the CacheIt client) is `use_cacheit` and answering, bodies go there too
    (namespace `ctxvault`), so a CacheIt hub can hold the RAM tier; the local dict stays the source for `keys()`."""
    volatile = True

    def __init__(self, name: str, use_cacheit: bool = False, cache_module=None):
        self.name = name
        self._data: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._cache = None
        if use_cacheit:
            try:
                if cache_module is None:
                    from bot import cache as cache_module
                if cache_module.available():
                    self._cache = cache_module
            except Exception:  # noqa: BLE001 - optional
                self._cache = None

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._data[key] = data
        if self._cache is not None:
            try:
                self._cache.put("ctxvault", f"{self.name}:{key}", data)
            except Exception:  # noqa: BLE001
                pass

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            return self._data.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            found = self._data.pop(key, None) is not None
        if self._cache is not None:
            try:
                self._cache.delete("ctxvault", f"{self.name}:{key}")
            except Exception:  # noqa: BLE001
                pass
        return found

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data)


class DirBackend(TierBackend):
    """One file per item under `<path>/<first two hex>/<key>`. Writes go to `<key>.part` and are renamed into place."""

    def __init__(self, name: str, path: Path):
        self.name = name
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c for c in key if c.isalnum() or c in "-_") or "x"
        return self.root / safe[:2] / safe

    def put(self, key: str, data: bytes) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        with open(part, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(part, target)

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self._path(key).read_bytes()
        except OSError:
            return None

    def has(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink()
            return True
        except OSError:
            return False

    def keys(self) -> list[str]:
        out = []
        for p in self.root.glob("*/*"):
            if p.is_file() and not p.name.endswith(".part"):
                out.append(p.name)
        return out

    def clear_partial(self) -> int:
        n = 0
        for p in self.root.glob("*/*.part"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n


def make_backend(spec: dict, default_dir: Path) -> TierBackend:
    kind = str(spec.get("kind") or "dir").lower()
    name = str(spec.get("name") or kind)
    if kind == "ram":
        return RamBackend(name, use_cacheit=bool(spec.get("use_cacheit")))
    if kind == "dir":
        return DirBackend(name, Path(spec.get("path") or default_dir))
    raise ValueError(f"unknown tier kind {kind!r} (ram or dir)")
