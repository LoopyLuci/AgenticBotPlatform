"""One poller per bot on this machine, whichever copy of ABP asks.

The leader lease (bot/lease.py) keeps two processes off ONE data directory. It
cannot see a second copy of ABP with its own data directory - another checkout,
an agent's clone, a restored backup - that holds the same bot credentials. On
2026-10-05 exactly that happened: something outside the live data directory
polled two Telegram tokens at once, the live bot and the Hermes gateway both got
409 Conflict, and the user's bot went quiet.

So before a platform connection starts, the supervisor takes a claim on the bot
itself: an OS file lock (the lease's own mechanics) named after a hash of the
instance's platform and credentials, in a per-user folder that does not depend
on HOME/USERPROFILE/LOCALAPPDATA - a copy started with a fake home must still
find the same lock. The kernel drops the lock when the holder dies, so there is
no stale-lock cleanup; a parked copy takes over the moment the holder is gone.
The token itself is never written anywhere: only its hash names the file.

    ABP_POLLER_CLAIM_DIR   overrides the folder (tests, or several Windows users
                           sharing one bot, who then need one shared folder).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from bot.lease import Lease

logger = logging.getLogger("bot.poller_claim")

CLAIM_DIR_ENV = "ABP_POLLER_CLAIM_DIR"


def _windows_local_appdata() -> Optional[Path]:
    """The account's real Local AppData, from the shell's known-folder record:
    unlike %LOCALAPPDATA% it cannot be redirected by a process's environment."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        raw = uuid.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le  # FOLDERID_LocalAppData
        guid = _GUID.from_buffer_copy(raw)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001 - fall back to the environment
        return None


def claim_dir() -> Path:
    override = os.environ.get(CLAIM_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = _windows_local_appdata() or Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / "AgenticBotPlatform" / "pollers"
    try:
        import pwd

        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # noqa: BLE001
        home = Path.home()
    return home / ".local" / "state" / "agenticbotplatform" / "pollers"


def claim_key(row: dict[str, Any]) -> Optional[str]:
    """What identifies the bot itself: its platform and credentials. None for an
    instance with no credentials (the in-app platform), which polls nothing."""
    creds = row.get("credentials") or {}
    if not any(str(v or "").strip() for v in creds.values()):
        return None
    blob = json.dumps({"platform": row.get("platform"), "credentials": creds}, sort_keys=True)
    return f"{row.get('platform')}-{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:20]}"


def for_instance(row: dict[str, Any]) -> Optional[Lease]:
    key = claim_key(row)
    if key is None:
        return None
    return Lease(path=claim_dir() / f"{key}.lock")


def holder_info(row: dict[str, Any]) -> dict[str, Any]:
    from bot.envfile import PROJECT_ROOT

    return {"instance_id": row.get("id"), "instance": row.get("name"), "state_root": str(PROJECT_ROOT)}


def held_text(holder: dict[str, Any]) -> str:
    """The status line for an instance parked behind another process's claim."""
    if not holder:
        return "another ABP on this machine is already running this bot; waiting for it to stop"
    where = holder.get("state_root") or holder.get("code_root") or "?"
    return (f"already running in another ABP on this machine (pid {holder.get('pid', '?')}, {where}, "
            f"as {holder.get('instance', '?')!r}); this copy waits and takes over when that one stops")
