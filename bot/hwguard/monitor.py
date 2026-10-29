"""The hardware monitor thread in ABP: a reading every `interval_s`, the verdict written to the shared folder for tools
outside ABP, a short history for the dashboard, and alerts through the Sentinel journal (which also pushes them).

    monitor.start() / monitor.stop()
    monitor.state()   -> {"running", "latest", "history": [...], "slot", "settings"}
    monitor.sample()  -> one reading now (also what the thread does)
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional

from bot.hwguard import governor, slot

log = logging.getLogger(__name__)
HISTORY_KEEP = 240
_lock = threading.RLock()
_history: deque = deque(maxlen=HISTORY_KEEP)
_latest: Optional[dict] = None
_last_level: Optional[str] = None
_seen_record: Optional[int] = None
_stop = threading.Event()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None


def _compact(st: dict) -> dict:
    s, v = st["snapshot"], st["verdict"]
    return {"ts": st["ts"], "level": v["level"], "cpu_pct": s["cpu"].get("load_pct"), "cpu_c": s["cpu"].get("temp_c"),
            "gpu_pct": s["gpu"].get("load_pct"), "gpu_c": s["gpu"].get("temp_c"), "vram_gb": s["gpu"].get("vram_used_gb"),
            "ram_free_gb": s["memory"].get("available_gb"), "commit_pct": (s["memory"].get("commit") or {}).get("pct")}


def _alerts(st: dict, cfg: dict) -> None:
    global _last_level, _seen_record
    from bot.sentinel import journal

    v = st["verdict"]
    events = st.get("events") or []
    top = max((e["record_id"] for e in events), default=0)
    if _seen_record is not None:
        for e in sorted((e for e in events if e["record_id"] > _seen_record), key=lambda e: e["record_id"]):
            journal.record("hardware", f"WHEA {e['event_id']}: {e['kind']} ({e.get('error_type') or 'type unknown'}, "
                           f"APIC {e.get('apic_id')}) at {e['time']}", level="critical" if e["severity"] == "fatal" else "warning",
                           whea=e)
    _seen_record = max(top, _seen_record or 0)
    if v["level"] != _last_level:
        if _last_level is not None:
            journal.record("hardware", f"hardware guard: {_last_level} -> {v['level']}", level="info", reasons=v["reasons"])
        _last_level = v["level"]
    if not cfg.get("alerts", True):
        return
    if v["level"] == governor.PAUSE:
        journal.alert("hwguard.pause", "Heavy work paused: " + "; ".join(v["reasons"]), level="warning")
    else:
        journal.clear("hwguard.pause")
    if v["level"] == governor.CAUTION:
        journal.alert("hwguard.caution", "Heavy work limited: " + "; ".join(v["reasons"]), level="info", notify=False)
    else:
        journal.clear("hwguard.caution")


def sample() -> dict:
    global _latest
    cfg = governor.settings()
    st = governor.evaluate(cfg=cfg)
    try:
        governor.write_json(governor.shared_dir() / "state.json", st)
    except OSError:
        log.debug("hwguard: could not write the shared verdict", exc_info=True)
    with _lock:
        _latest = st
        _history.append(_compact(st))
    try:
        _alerts(st, cfg)
    except Exception:  # noqa: BLE001 - alerting must never stop the monitor
        log.debug("hwguard alerts", exc_info=True)
    return st


def _loop() -> None:
    while not _stop.is_set():
        cfg = governor.settings()
        if cfg.get("monitor", True):
            try:
                sample()
            except Exception:  # noqa: BLE001
                log.debug("hwguard sample failed", exc_info=True)
        _wake.wait(max(5.0, float(cfg.get("interval_s") or 15)))
        _wake.clear()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="hwguard-monitor", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()


def poke() -> None:
    _wake.set()


def state() -> dict:
    with _lock:
        latest, hist = _latest, list(_history)
    return {"running": bool(_thread and _thread.is_alive()), "latest": latest, "history": hist, "slot": slot.holder(),
            "settings": governor.settings(), "ack": {"ts": governor.ack_ts()}}


def _reset_for_tests() -> None:
    global _latest, _last_level, _seen_record
    with _lock:
        _history.clear()
        _latest = _last_level = _seen_record = None
