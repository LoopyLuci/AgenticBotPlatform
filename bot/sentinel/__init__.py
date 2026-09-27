"""Sentinel: ABP's self-preservation system (ADR-0011).

One background loop ties together every way ABP protects itself:

  detect   repair.check_database  integrity (quick_check), disk space, WAL growth
           cve                    OSV.dev scan of everything installed and shipped
           security               exposure, weak token, file permissions, leaked
                                  secrets in logs, code tampering (installed copies)
           bug_hunter             every error fingerprinted; new, regressed and
                                  spiking issues surfaced
           watchdog               event-loop stalls (with the blocking stack),
                                  hangs, memory and thread growth
  preserve backup                 verified, rotated, optionally mirrored backups
           bootguard              last-known-good config, saved after each healthy boot
  repair   repair.repair_database REINDEX, then salvage, then restore
           checkpoint_wal         keeps the WAL bounded
           security.tighten_*     owner-only permissions; log redaction
           cve.fix_python         upgrade, smoke-test, roll back (opt-in)
           bootguard              crash loop -> safe mode + config rollback
           watchdog + guardian    hang -> supervised restart

Everything it notices goes to the journal (bot/sentinel/journal.py) and, when a
person needs to know, out as an alert: log, dashboard and phone push.
Configuration lives under `sentinel:` in config/backends.yaml. Every key has a
safe default, so an older config file still gets full protection.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any, Optional

from bot.sentinel import bootguard, bug_hunter, journal
from bot.sentinel.watchdog import Watchdog

logger = logging.getLogger("bot.sentinel")

TICK_S = 60

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "integrity": {"every_minutes": 60, "auto_repair": True},
    "backup": {"every_hours": 24, "keep_recent": 7, "keep_daily": 14, "keep_weekly": 8, "mirror_dir": ""},
    "cve": {"enabled": True, "every_hours": 24, "auto_fix": False, "include_checkout": True},
    "security": {"every_hours": 6, "auto_fix": True},
    "bug_hunter": {"enabled": True, "kanban_instance_id": None},
    "watchdog": {"enabled": True, "lag_warn_s": 2.0, "hang_after_s": 180, "memory_limit_mb": 4096},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def settings() -> dict[str, Any]:
    try:
        from bot.config import config

        return _merge(DEFAULTS, config.current.get("sentinel") or {})
    except Exception:  # noqa: BLE001 — a broken config must not disable self-preservation
        return copy.deepcopy(DEFAULTS)


class Sentinel:
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.watchdog: Optional[Watchdog] = None
        self.last_run: dict[str, float] = {}
        self.last_result: dict[str, Any] = {}
        self.healthy_marked = False
        self._running: set[str] = set()

    # ------------------------------------------------------------ lifecycle
    def start(self, loop: asyncio.AbstractEventLoop, stop_event: Optional[asyncio.Event] = None) -> None:
        journal.load_recent_from_disk()
        cfg = settings()
        if cfg["bug_hunter"]["enabled"]:
            bug_hunter.install()
        if cfg["watchdog"]["enabled"]:
            w = cfg["watchdog"]
            self.watchdog = Watchdog(lag_warn_s=float(w["lag_warn_s"]), hang_after_s=float(w["hang_after_s"]),
                                     memory_limit_mb=int(w["memory_limit_mb"]))
            if stop_event is not None:
                self.watchdog.on_orphaned = lambda: loop.call_soon_threadsafe(stop_event.set)
            self.watchdog.start(loop)
        journal.record("start", "sentinel started", safe_mode=bootguard.is_safe_mode())

    def stop(self) -> None:
        if self.watchdog is not None:
            self.watchdog.stop()
        bug_hunter.flush()

    # ------------------------------------------------------------ scheduling
    def _due(self, name: str, every_s: float, first_after_s: float) -> bool:
        last = self.last_run.get(name)
        if last is None:
            return time.monotonic() - self.started_at >= first_after_s
        return time.monotonic() - last >= every_s

    async def _run(self, name: str, fn, *args) -> Any:
        if name in self._running:
            return None
        self._running.add(name)
        self.last_run[name] = time.monotonic()
        try:
            result = await asyncio.to_thread(fn, *args)
            self.last_result[name] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": True, "result": result}
            return result
        except Exception as exc:  # noqa: BLE001 — one failing duty never stops the others
            logger.exception("sentinel %s failed", name)
            self.last_result[name] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": False,
                                      "error": f"{type(exc).__name__}: {exc}"}
            return None
        finally:
            self._running.discard(name)

    async def tick(self) -> None:
        cfg = settings()
        if not cfg["enabled"]:
            return
        if not self.healthy_marked and time.monotonic() - self.started_at >= bootguard.HEALTHY_AFTER_S:
            self.healthy_marked = True
            await self._run("healthy", bootguard.mark_healthy)
        if cfg["bug_hunter"]["enabled"]:
            await self._run("bugs", bug_hunter.review, cfg["bug_hunter"].get("kanban_instance_id"))
        if self._due("integrity", cfg["integrity"]["every_minutes"] * 60, 90):
            await self._run("integrity", self.integrity_duty, cfg)
        if self._due("backup", cfg["backup"]["every_hours"] * 3600, 600) and self._backup_stale(cfg):
            await self._run("backup", self.backup_duty, cfg)
        elif "backup" not in self.last_run and time.monotonic() - self.started_at >= 600:
            self.last_run["backup"] = time.monotonic()  # a fresh one exists already; start the clock
        if cfg["cve"]["enabled"] and self._due("cve", cfg["cve"]["every_hours"] * 3600, 300):
            await self._run("cve", self.cve_duty, cfg)
        if self._due("security", cfg["security"]["every_hours"] * 3600, 120):
            await self._run("security", self.security_duty, cfg)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        logger.info("sentinel running (tick %ds)", TICK_S)
        while not stop_event.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("sentinel tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass
        self.stop()

    # ------------------------------------------------------------ duties (run in a worker thread)
    @staticmethod
    def _backup_stale(cfg: dict[str, Any]) -> bool:
        from datetime import datetime, timezone

        from bot.sentinel import backup

        latest = backup.latest_verified()
        if latest is None:
            return True
        made = datetime.fromisoformat(latest["created_at"])
        return (datetime.now(timezone.utc) - made).total_seconds() >= cfg["backup"]["every_hours"] * 3600 * 0.9

    def integrity_duty(self, cfg: dict[str, Any]) -> dict[str, Any]:
        from bot.sentinel import backup, repair

        result = repair.check_database()
        if result["quick_check"] != "ok":
            journal.alert("db.integrity", f"database integrity check failed: {result['quick_check']}", level="critical")
            if cfg["integrity"]["auto_repair"]:
                result["repair"] = repair.repair_database()
        else:
            journal.clear("db.integrity")
        if result.get("wal_bytes", 0) > repair.WAL_WARN_BYTES:
            repair.checkpoint_wal()
            result["checkpointed"] = True
        low = [p for p in result["problems"] if p.startswith("low disk")]
        if low:
            removed = backup.prune(keep_recent=2, keep_daily=3, keep_weekly=2)
            journal.alert("disk.low", f"{low[0]} — pruned {len(removed)} old backup set(s) to make room", level="warning")
        else:
            journal.clear("disk.low")
        return result

    def backup_duty(self, cfg: dict[str, Any]) -> dict[str, Any]:
        from pathlib import Path

        from bot.sentinel import backup

        b = cfg["backup"]
        manifest = backup.create_backup("scheduled")
        if not manifest["verified"]:
            journal.alert("backup.verify", f"backup {manifest['name']} failed verification: {manifest['problems']}",
                          level="critical")
        else:
            journal.clear("backup.verify")
            journal.record("backup", f"verified backup {manifest['name']} ({len(manifest['files'])} files, "
                                     f"{manifest['seconds']}s)")
        removed = backup.prune(b["keep_recent"], b["keep_daily"], b["keep_weekly"])
        out = {"name": manifest["name"], "verified": manifest["verified"], "pruned": removed}
        if b.get("mirror_dir"):
            problems = backup.mirror(manifest, Path(b["mirror_dir"]))
            if problems:
                journal.alert("backup.mirror", f"backup mirror to {b['mirror_dir']} failed: {problems}", level="warning")
            else:
                journal.clear("backup.mirror")
                backup.prune(b["keep_recent"], b["keep_daily"], b["keep_weekly"], root=Path(b["mirror_dir"]))
            out["mirror_problems"] = problems
        # Re-verify the oldest kept set too: bit rot on old backups is only noticed when checked.
        sets = backup.list_backups()
        if len(sets) > 1:
            old = sets[-1]
            rot = backup.verify_backup(Path(old["path"]))
            if rot:
                journal.alert(f"backup.rot:{old['name']}", f"backup {old['name']} no longer verifies: {rot}", level="warning")
        return out

    def cve_duty(self, cfg: dict[str, Any]) -> dict[str, Any]:
        from bot.sentinel import cve

        try:
            result = cve.scan(cve.inventory(include_checkout=bool(cfg["cve"]["include_checkout"])))
        except Exception as exc:  # noqa: BLE001 — offline is normal; retry next cycle, keep the last result
            journal.record("cve-scan", f"vulnerability database unreachable ({type(exc).__name__}); will retry")
            self.last_run["cve"] = time.monotonic() - cfg["cve"]["every_hours"] * 3600 + 3600
            return {"offline": True}
        cve.report(result)
        out: dict[str, Any] = {"packages": result["packages"], "findings": len(result["findings"])}
        if cfg["cve"]["auto_fix"]:
            fixable = [f for f in result["findings"] if f["source"] == "python-env" and f["fixed"]]
            if fixable:
                out["fixes"] = cve.fix_python(fixable)
        return out

    def security_duty(self, cfg: dict[str, Any]) -> dict[str, Any]:
        from pathlib import Path

        from bot.sentinel import security

        log_path = _log_path()
        findings = security.run_all(log_path)
        fixed: list[str] = []
        if cfg["security"]["auto_fix"]:
            if any(f["key"].startswith("security.perms:") for f in findings):
                fixed += security.tighten_permissions()
            if any(f["key"] == "security.log-leak" for f in findings) and log_path is not None:
                n = security.redact_log(Path(log_path))
                if n:
                    fixed.append(f"redacted {n} secret(s) in {Path(log_path).name}")
        live = set()
        for f in findings:
            live.add(f["key"])
            if f["level"] != "info":
                journal.alert(f["key"], f["message"], level=f["level"])
        for key in list(journal._last_alert):
            if key.startswith("security.") and key not in live:
                journal.clear(key)
        return {"findings": findings, "fixed": fixed}

    # ------------------------------------------------------------ reporting
    def status(self) -> dict[str, Any]:
        from bot.sentinel import backup, cve

        latest = backup.latest_verified()
        cve_last = cve.last_result()
        return {
            "enabled": settings()["enabled"],
            "uptime_s": round(time.monotonic() - self.started_at),
            "boot": bootguard.status(),
            "watchdog": self.watchdog.status() if self.watchdog else None,
            "last_run": self.last_result,
            "latest_backup": {k: latest[k] for k in ("name", "created_at", "size_bytes", "verified")} if latest else None,
            "backups": len(backup.list_backups()),
            "cve": {"scanned_at": cve_last["scanned_at"], "packages": cve_last["packages"],
                    "findings": [f for f in cve_last["findings"] if f["severity"] != "INFO"]} if cve_last else None,
            "issues_open": len(bug_hunter.issues(limit=10000, status="open")),
            "alerts": journal.recent(50, min_level="warning"),
        }


def _log_path():
    for h in logging.getLogger().handlers:
        name = getattr(h, "baseFilename", None)
        if name and name.endswith("bot.log"):
            return name
    return None


sentinel = Sentinel()
