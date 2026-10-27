"""A thin, never-crashing client over CacheIt for the conductor's proofs and test runs.

A result is keyed by (tree hash of the worktree, the exact command, the test ids). Only PASSES are stored, so a hit means
"this exact tree already passed this exact command". CacheIt is reached through ABP's module client (bot/modules/client.py):
the `cacheit` module's hub operations `cache.get` / `cache.put`, namespace `conductor`. When the module is unknown, its hub is
not running, or any call fails, every lookup is a miss and every store does nothing - the caller just runs the command.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

NAMESPACE = "conductor"
TTL_S = 7 * 24 * 3600
_DOWN_FOR_S = 60.0
Backend = Callable[[str, dict], Any]       # (operation id, args) -> result; may raise


def _git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), "-c", "core.autocrlf=false", *args], capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise OSError(proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout


def tree_hash(root: Path) -> str:
    """A hash of what is in the worktree: HEAD's tree, the uncommitted diff and the untracked files. '' if it cannot be told."""
    root = Path(root)
    try:
        h = hashlib.sha256(_git(root, "rev-parse", "HEAD^{tree}"))
        h.update(_git(root, "diff", "HEAD", "--binary", "--no-ext-diff"))
        for rel in sorted(_git(root, "ls-files", "-o", "--exclude-standard", "-z").decode("utf-8", "replace").split("\0")):
            if rel:
                h.update(rel.encode())
                try:
                    h.update((root / rel).read_bytes())
                except OSError:
                    pass
        return h.hexdigest()
    except (OSError, subprocess.SubprocessError):
        return ""


def make_key(tree: str, command: Sequence[str] | str, tests: Sequence[str] = ()) -> str:
    cmd = command if isinstance(command, str) else json.dumps(list(command))
    return hashlib.sha256(json.dumps([tree, cmd, sorted(tests)]).encode()).hexdigest()


def _module_backend() -> Backend:
    from bot.modules import client, registry

    m = registry.find("cacheit")
    if m is None or client.find(m) is None:
        raise OSError("the CacheIt hub is not running")
    return lambda op, args: client.call(m, op, args, timeout=10)


class Cache:
    """`backend` is injectable (tests); by default CacheIt's hub, looked up lazily and remembered as down for a minute."""

    def __init__(self, enabled: bool = True, backend: Optional[Backend] = None):
        self.enabled = bool(enabled)
        self._backend = backend
        self._injected = backend is not None
        self._down_until = 0.0

    def _call(self, op: str, args: dict) -> Any:
        if not self.enabled or time.time() < self._down_until:
            return None
        try:
            if self._backend is None:
                self._backend = _module_backend()
            return self._backend(op, args)
        except Exception:  # noqa: BLE001 - a cache must never stop a run
            if not self._injected:
                self._backend = None
            self._down_until = time.time() + _DOWN_FOR_S
            return None

    def lookup(self, tree: str, command: Sequence[str] | str, tests: Sequence[str] = ()) -> Optional[dict]:
        """The stored passing result for this exact (tree, command, tests), or None."""
        if not tree:
            return None
        r = self._call("cache.get", {"namespace": NAMESPACE, "key": make_key(tree, command, tests), "encoding": "utf8"})
        if not isinstance(r, dict) or not r.get("found"):
            return None
        try:
            val = json.loads(r.get("value") or "")
        except ValueError:
            return None
        return val if isinstance(val, dict) else None

    def store(self, tree: str, command: Sequence[str] | str, tests: Sequence[str], result: dict) -> bool:
        if not tree:
            return False
        r = self._call("cache.put", {"namespace": NAMESPACE, "key": make_key(tree, command, tests), "value": json.dumps(result),
                                     "ttl_s": TTL_S})
        return r is not None

    # Plain values in any namespace (bot/conductor/buildcache.py): None / False whenever CacheIt is off, down or says no.
    def get_json(self, namespace: str, key: str) -> Optional[dict]:
        r = self._call("cache.get", {"namespace": namespace, "key": key, "encoding": "utf8"})
        if not isinstance(r, dict) or not r.get("found"):
            return None
        try:
            val = json.loads(r.get("value") or "")
        except ValueError:
            return None
        return val if isinstance(val, dict) else None

    def put_json(self, namespace: str, key: str, value: dict, ttl_s: int = TTL_S) -> bool:
        return self._call("cache.put", {"namespace": namespace, "key": key, "value": json.dumps(value), "ttl_s": ttl_s}) is not None

    def get_bytes(self, namespace: str, key: str) -> Optional[bytes]:
        if not key:
            return None
        r = self._call("cache.get", {"namespace": namespace, "key": key, "encoding": "base64"})
        if not isinstance(r, dict) or not r.get("found"):
            return None
        try:
            return base64.b64decode(r.get("value_b64") or "")
        except ValueError:
            return None

    def put_cas(self, namespace: str, data: bytes, ttl_s: int = TTL_S) -> str:
        """Store bytes under their own hash; the key CacheIt returns, or '' on any failure."""
        r = self._call("cache.put_cas", {"namespace": namespace, "value_b64": base64.b64encode(data).decode(), "ttl_s": ttl_s})
        return str(r.get("key") or "") if isinstance(r, dict) else ""


def from_settings(cfg: Optional[dict] = None) -> Cache:
    if cfg is None:
        from bot import conductor

        cfg = conductor.settings()
    c = cfg.get("cache") if isinstance(cfg.get("cache"), dict) else {}
    return Cache(enabled=bool(c.get("enabled", True)))
