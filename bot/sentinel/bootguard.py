"""Crash-loop detection, safe mode, and last-known-good configuration.

Every start writes a boot record; once the process has run healthily for
HEALTHY_AFTER_S, the record is marked good and the current configuration is
saved as last-known-good. So "the last N boots never became healthy" means a
crash loop, whatever the cause: a bad config edit, a plugin that crashes on
import, a corrupted file.

On the CRASH_LOOP_BOOTS-th consecutive unhealthy boot, ABP starts in safe
mode:

- config/*.yaml and .env are rolled back to last-known-good (the current
  files are kept beside them, never deleted);
- plugins, which are arbitrary local code (ADR-0007), are not loaded for this
  boot.

A person is told about it every time. Safe mode lasts one boot. If the next
boot is healthy, the rollback worked; if not, the loop continues and
eventually the supervisor gives up with the evidence in the journal.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT
from bot.sentinel import journal

logger = logging.getLogger("bot.sentinel.bootguard")

BOOTS_PATH = journal.SENTINEL_DIR / "boots.json"
LKG_DIR = journal.SENTINEL_DIR / "last-known-good"
HEALTHY_AFTER_S = 120
CRASH_LOOP_BOOTS = 3
KEEP_BOOTS = 50

_state: dict[str, Any] = {"safe_mode": False, "boot_id": None, "reason": None}


def _load() -> list[dict[str, Any]]:
    try:
        return json.loads(BOOTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(boots: list[dict[str, Any]]) -> None:
    try:
        BOOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = BOOTS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(boots[-KEEP_BOOTS:], indent=1), encoding="utf-8")
        tmp.replace(BOOTS_PATH)
    except OSError:
        pass


def _lkg_sources() -> dict[str, Path]:
    from bot import envfile

    out = {f"config/{p.name}": p for p in sorted((PROJECT_ROOT / "config").glob("*.yaml"))}
    out[".env"] = Path(envfile.resolve())
    return out


def begin_boot() -> dict[str, Any]:
    """Call first thing at startup. Decides whether this boot is safe mode
    and, if so, restores last-known-good config before anything reads it."""
    boots = _load()
    unhealthy_run = 0
    for b in reversed(boots):
        if b.get("healthy"):
            break
        unhealthy_run += 1
    boot = {"id": int(time.time() * 1000), "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pid": os.getpid(), "healthy": False, "safe_mode": False}
    forced = os.environ.get("ABP_SAFE_MODE") == "1"
    if forced or unhealthy_run + 1 >= CRASH_LOOP_BOOTS:
        boot["safe_mode"] = True
        reason = "ABP_SAFE_MODE=1" if forced else f"{unhealthy_run} consecutive boot(s) never became healthy"
        _state.update(safe_mode=True, reason=reason)
        restored = restore_last_known_good()
        boot["restored"] = restored
        journal.alert("bootguard.safe-mode", f"starting in SAFE MODE ({reason}): plugins disabled for this boot; "
                      + (f"restored last-known-good {', '.join(restored)}" if restored else "no last-known-good config to restore"),
                      level="critical")
    _state["boot_id"] = boot["id"]
    boots.append(boot)
    _save(boots)
    return boot


def mark_healthy() -> None:
    """Call once the process has been up and serving for HEALTHY_AFTER_S."""
    boots = _load()
    for b in reversed(boots):
        if b.get("id") == _state["boot_id"]:
            b["healthy"] = True
            b["healthy_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            break
    _save(boots)
    save_last_known_good()
    if _state["safe_mode"]:
        journal.alert("bootguard.recovered", "recovered: the safe-mode boot is healthy", level="warning")
    journal.clear("bootguard.safe-mode")


def save_last_known_good() -> list[str]:
    saved = []
    for name, src in _lkg_sources().items():
        if src.is_file():
            dest = LKG_DIR / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            saved.append(name)
    return saved


def restore_last_known_good() -> list[str]:
    if not LKG_DIR.exists():
        return []
    restored = []
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for name, live in _lkg_sources().items():
        good = LKG_DIR / name
        if not good.is_file():
            continue
        if live.is_file():
            if live.read_bytes() == good.read_bytes():
                continue
            shutil.copy2(live, live.with_name(f"{live.name}.pre-safe-mode-{stamp}"))
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(good, live)
        restored.append(name)
    return restored


def is_safe_mode() -> bool:
    return bool(_state["safe_mode"])


def status() -> dict[str, Any]:
    boots = _load()
    return {"safe_mode": _state["safe_mode"], "reason": _state["reason"], "boot_id": _state["boot_id"],
            "recent_boots": boots[-10:][::-1],
            "last_known_good": sorted(str(p.relative_to(LKG_DIR)).replace("\\", "/")
                                      for p in LKG_DIR.rglob("*") if p.is_file()) if LKG_DIR.exists() else []}


def _reset_for_tests() -> None:
    _state.update(safe_mode=False, boot_id=None, reason=None)


def current_boot() -> Optional[int]:
    return _state["boot_id"]
