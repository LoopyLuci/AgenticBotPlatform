"""A stable, random identity for THIS ABP install.

The Android app stores several addresses for its paired server (LAN,
Tailscale, a Funnel URL) and refreshes them from the server and from mDNS
discovery. Without a way to tell "the same server at a new address" from "some
OTHER ABP that happens to be on the network" (a second machine, a test
instance, a stale advertisement of an old one), that refresh could silently
repoint the phone at the wrong server — and then send its API key there.

So every install has a random id, served (unauthenticated — it is not a
secret, only an identifier) in /healthz. The phone learns it at pairing and
only ever adopts an address whose /healthz reports the same id.

It lives under the state root (ABP_HOME/data), so it follows the data: two
installs never share one, and restoring a backup keeps the phone's pairing
valid.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.server_identity")

ID_PATH = PROJECT_ROOT / "data" / "server_id"

_lock = threading.Lock()
_cached: str | None = None


def _valid(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def get_server_id(path: Path | None = None) -> str:
    """This install's id, created on first use. If the file can't be written
    (read-only state dir) a per-process id is returned instead — the phone then
    simply re-learns it after a restart rather than the server failing."""
    global _cached
    path = path or ID_PATH
    with _lock:
        if path == ID_PATH and _cached is not None:
            return _cached
        try:
            existing = path.read_text(encoding="utf-8").strip().lower()
            if _valid(existing):
                if path == ID_PATH:
                    _cached = existing
                return existing
        except OSError:
            pass
        new_id = uuid.uuid4().hex
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(new_id, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("couldn't persist the server id (%s) — using a per-process id", exc)
        if path == ID_PATH:
            _cached = new_id
        return new_id
