"""The file server as a whole: its process, one overview, the schedules, and events.

Schedules (settings.json, each can be turned off with 0):
    sync_hour           parity sync once a day at this hour (local time)
    scrub_days/percent  every N days, scrub this share of the array (all of it over 100/percent runs)
    mover_hour          the mover once a day
    smart_minutes       drive health checks
    index_minutes       the content index, a budgeted slice at a time (index_budget_s)
    guard_minutes       the ransomware guard
    transfer jobs and backup jobs run when their own interval is due; the recycle bins are emptied daily
Events (alerts, warnings, finished jobs) are kept in events.json for the page, the CLI and agents.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Callable

from bot.fileserver import shares
from bot.fileserver.store import FsError, load, root, update

logger = logging.getLogger(__name__)
Log = Callable[[str], None]
DEFAULTS = {"port": 8790, "bind": "0.0.0.0", "autostart": False, "sync_hour": 3, "scrub_days": 7, "scrub_percent": 12.0,
            "mover_hour": 4, "smart_minutes": 60, "index_minutes": 30, "index_budget_s": 120, "describe_images": True,
            "guard_minutes": 5}


def settings() -> dict:
    return {**DEFAULTS, **load("settings", {})}


def set_settings(changes: dict) -> dict:
    bad = set(changes) - set(DEFAULTS)
    if bad:
        raise FsError(f"unknown setting(s): {', '.join(sorted(bad))}")
    update("settings", {}, lambda s: s.update(changes))
    return settings()


def event(kind: str, text: str, level: str = "info") -> None:
    def put(ev):
        ev.append({"at": int(time.time()), "kind": kind, "level": level, "text": text[:500]})
        del ev[:-300]
    update("events", [], put)
    (logger.warning if level != "info" else logger.info)("fileserver %s: %s", kind, text)


def events(limit: int = 100) -> list[dict]:
    return list(reversed(load("events", [])))[:limit]


# ---- the server process --------------------------------------------------------------------------------------------- #

def server_status() -> dict:
    from bot.hosting import procs
    st = settings()
    s = procs.status("fileserver", home=root())
    out = {**s, "port": st["port"], "bind": st["bind"]}
    if s["running"]:
        try:
            import httpx
            out["health"] = httpx.get(f"http://127.0.0.1:{st['port']}/api/health", timeout=3).json()
        except Exception as e:  # noqa: BLE001
            out["health"] = {"error": str(e)}
    from bot.hosting.netinfo import lan_ip
    out["urls"] = {"web": f"http://{lan_ip() or '127.0.0.1'}:{st['port']}/", "webdav": f"http://{lan_ip() or '127.0.0.1'}:{st['port']}/dav/"}
    return out


def server_start() -> dict:
    from bot.envfile import CODE_ROOT
    from bot.hosting import procs
    st = settings()
    env = {"PYTHONPATH": str(CODE_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""), "ABP_FILESERVER_DIR": str(root())}
    return procs.start("fileserver", [sys.executable, "-m", "bot.fileserver.server", "--port", str(st["port"]), "--bind", st["bind"]],
                       env=env, cwd=str(CODE_ROOT), home=root())


def server_stop() -> bool:
    from bot.hosting import procs
    return procs.stop("fileserver", home=root())


# ---- overview ------------------------------------------------------------------------------------------------------- #

def overview(deep: bool = False) -> dict:
    from bot.fileserver import array, backup, guard, transfer
    out = {"server": server_status(), "array": array.status(scan_changes=deep), "pools": shares.pools(),
           "shares": [{"name": n, **{k: v for k, v in s.items() if k in ("comment", "cache", "access", "path", "allocation")}}
                      for n, s in shares.shares().items()],
           "users": shares.users(), "frozen": sorted(guard.frozen()), "alerts": [a for a in guard.recent_alerts(10) if not a["cleared"]],
           "transfers": transfer.jobs(), "backups": backup.jobs(), "events": events(30), "settings": settings()}
    return out


# ---- schedules ------------------------------------------------------------------------------------------------------ #

def _last(key: str) -> float:
    return float(load("last_runs", {}).get(key, 0))


def _mark(key: str) -> None:
    update("last_runs", {}, lambda d: d.__setitem__(key, time.time()))


def _daily(key: str, hour: int) -> bool:
    if hour is None or hour < 0:
        return False
    now = time.localtime()
    return now.tm_hour == hour and time.time() - _last(key) > 20 * 3600


def tick() -> None:
    from bot.fileserver import array, backup, disks, guard, index, mover, transfer
    st = settings()
    now = time.time()
    if st["autostart"] and not server_status()["running"] and now - _last("server_restart") > 120:
        _mark("server_restart")
        try:
            server_start()
            event("server", "the file server was not running; started it")
        except Exception as e:  # noqa: BLE001
            event("server", f"could not start the file server: {e}", "warning")
    cfg = array.config()
    if cfg["disks"] and cfg["parity"]:
        if _daily("sync", st["sync_hour"]):
            _mark("sync")
            try:
                r = array.sync()
                event("parity", f"sync: {r['added']} added, {r['changed']} changed, {r['removed']} removed in {r['seconds']} s")
            except FsError as e:
                event("parity", f"sync did not run: {e}", "warning")
        if st["scrub_days"] and now - _last("scrub") > st["scrub_days"] * 86400:
            _mark("scrub")
            r = array.scrub(st["scrub_percent"])
            lvl = "warning" if r["data_errors"] or r["parity_errors"] else "info"
            event("parity", f"scrub: {r['checked']} stripes, {r['data_errors']} corrupted blocks, {r['parity_errors']} parity errors", lvl)
    if _daily("mover", st["mover_hour"]):
        _mark("mover")
        r = mover.run()
        if r["moved"] or r["errors"]:
            event("mover", f"moved {r['moved']} file(s), {r['bytes'] >> 20} MiB; {len(r['errors'])} error(s)", "warning" if r["errors"] else "info")
        n = shares.recycle_purge()
        if n:
            event("recycle", f"emptied {n} old recycle-bin folder(s)")
    if st["smart_minutes"] and now - _last("smart") > st["smart_minutes"] * 60:
        _mark("smart")
        try:
            for d in disks.inventory()["drives"]:
                if d["risk"]["band"] in ("watch", "replace soon"):
                    event("disk", f"{d['model']} ({d['device']}): {d['risk']['band']} — {'; '.join(d['risk']['reasons'])}", "warning")
        except Exception as e:  # noqa: BLE001
            logger.warning("disk check failed: %s", e)
    if st["guard_minutes"] and now - _last("guard") > st["guard_minutes"] * 60:
        _mark("guard")
        for a in guard.check():
            event("guard", f"possible ransomware on {a['share']} (score {a['score']}): {a['entropy_jumps']} files became random, "
                           f"{a['renamed_to_new_extensions']} renamed{'; the share is now read-only' if a['frozen'] else ''}", "alert")
    if st["index_minutes"] and now - _last("index") > st["index_minutes"] * 60:
        _mark("index")
        for name in shares.shares():
            try:
                index.index_share(name, describe_images=st["describe_images"], budget_s=st["index_budget_s"])
            except Exception as e:  # noqa: BLE001
                logger.warning("index %s: %s", name, e)
    for name in transfer.due():
        try:
            r = transfer.run(name)
            event("transfer", f"{name}: {r['done']}/{r['planned']} done, {len(r['failed'])} failed", "warning" if r["failed"] else "info")
        except Exception as e:  # noqa: BLE001
            event("transfer", f"{name}: {e}", "warning")
    for name in backup.due():
        try:
            r = backup.run_job(name)
            event("backup", f"{name}: snapshot {r['snapshot']}, {r['files']} files, {r['new_bytes_stored'] >> 20} MiB new",
                  "warning" if r["errors"] else "info")
        except Exception as e:  # noqa: BLE001
            event("backup", f"{name}: {e}", "warning")


async def run_forever(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=45)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        if shares.shares() or load("array", {}):          # nothing to look after until something is set up
            try:
                await asyncio.to_thread(tick)
            except Exception:  # noqa: BLE001
                logger.exception("file server tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
