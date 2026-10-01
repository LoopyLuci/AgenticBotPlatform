"""What the system models learn from: this machine's own measurements.

    topology()      logical processors -> APIC ids (CPUID on each processor on Windows; /proc/cpuinfo on Linux), which
                    physical core and CCD/die each belongs to
    hw_errors()     machine-check (WHEA) errors per APIC id and unexpected power losses, from the system event log
    sample()        one reading: per-processor load, clock, memory, swap, per-disk throughput and busy time, network,
                    GPU engine load and dedicated memory (Windows performance counters), ABP's own CPU share
    Recorder        samples every `interval` seconds into <lab>/telemetry.db (kept 30 days); cheap: one psutil pass
                    and one counter query per sample, on one background thread

Reading only: nothing here changes a system setting.
"""
from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import psutil

WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000 if WIN else 0
KEEP_DAYS = 30


def db_path() -> Path:
    from bot.neurallab import lab
    return lab.root() / "telemetry.db"


# ---- processors ---------------------------------------------------------------------------------------------------- #

_CPUID_CODE = bytes([0x53,                    # push rbx
                     0x89, 0xC8,              # mov eax, ecx      (leaf, first argument)
                     0x31, 0xC9,              # xor ecx, ecx
                     0x0F, 0xA2,              # cpuid
                     0x89, 0xD8,              # mov eax, ebx      (EBX[31:24] = this processor's initial APIC id)
                     0x5B,                    # pop rbx
                     0xC3])                   # ret
_topo_cache: Optional[list[dict]] = None


def _apic_ids_windows() -> dict[int, int]:
    """Run CPUID leaf 1 on each logical processor (the thread pinned to it in turn) and read its APIC id."""
    import platform
    if platform.machine().lower() not in ("amd64", "x86_64"):
        return {}
    k32 = ctypes.windll.kernel32
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
    k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    k32.SetThreadAffinityMask.restype = ctypes.c_size_t
    k32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    k32.GetCurrentThread.restype = ctypes.c_void_p
    mem = k32.VirtualAlloc(None, 4096, 0x3000, 0x40)          # MEM_COMMIT|MEM_RESERVE, PAGE_EXECUTE_READWRITE
    if not mem:
        return {}
    out = {}
    try:
        ctypes.memmove(mem, _CPUID_CODE, len(_CPUID_CODE))
        fn = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32)(mem)
        th = k32.GetCurrentThread()
        n = min(psutil.cpu_count() or 1, 64)
        old = None
        for i in range(n):
            prev = k32.SetThreadAffinityMask(th, 1 << i)
            if old is None:
                old = prev
            if not prev:
                continue
            time.sleep(0)                                       # let the scheduler move the thread
            out[i] = (fn(1) >> 24) & 0xFF
        if old:
            k32.SetThreadAffinityMask(th, old)
    finally:
        k32.VirtualFree(mem, 0, 0x8000)                       # MEM_RELEASE
    return out


def _apic_ids_linux() -> dict[int, int]:
    out, cur = {}, None
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            k, _, v = line.partition(":")
            k = k.strip()
            if k == "processor":
                cur = int(v)
            elif k == "apicid" and cur is not None:
                out[cur] = int(v)
    except OSError:
        pass
    return out


def topology() -> list[dict]:
    """[{logical, apic, core, ccd}]: SMT siblings share a core (APIC id >> 1 with 2 threads per core); on AMD Zen the
    CCD is APIC id >> 4 (16 APIC ids per CCD whatever the core count)."""
    global _topo_cache
    if _topo_cache is not None:
        return _topo_cache
    ids = _apic_ids_windows() if WIN else _apic_ids_linux()
    n = psutil.cpu_count() or 1
    smt = 2 if (psutil.cpu_count(logical=False) or n) < n else 1
    rows = []
    for i in range(n):
        a = ids.get(i)
        rows.append({"logical": i, "apic": a, "core": (a // smt) if a is not None else i // smt,
                     "ccd": (a >> 4) if a is not None else None})
    _topo_cache = rows
    return rows


def logical_for_apic(apic: int) -> Optional[int]:
    return next((r["logical"] for r in topology() if r["apic"] == apic), None)


# ---- hardware error history ---------------------------------------------------------------------------------------- #

def _events(query: str, count: int = 200) -> list[ET.Element]:
    if not WIN:
        return []
    try:
        r = subprocess.run(["wevtutil", "qe", "System", f"/q:{query}", "/f:xml", "/rd:true", f"/c:{count}"],
                           capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        root = ET.fromstring("<Events>" + r.stdout + "</Events>")
    except ET.ParseError:
        return []
    return list(root)


def _ns(tag: str) -> str:
    return "{http://schemas.microsoft.com/win/2004/08/events/event}" + tag


def _when(ev: ET.Element) -> float:
    import datetime as dt
    t = ev.find(f"{_ns('System')}/{_ns('TimeCreated')}").get("SystemTime", "")     # 2026-09-30T22:21:42.1234567Z
    try:
        return dt.datetime.fromisoformat(t[:19] + "+00:00").timestamp()
    except ValueError:
        return 0.0


def hw_errors(days: int = 365) -> dict:
    """Machine-check errors (WHEA-Logger) by APIC id with times, and unexpected power losses (Kernel-Power 41)."""
    since = time.time() - days * 86400
    mce = []
    for ev in _events("*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger']]]"):
        t = _when(ev)
        if t < since:
            continue
        data = {d.get("Name"): (d.text or "") for d in ev.iter(_ns("Data"))}
        eid = int(ev.find(f"{_ns('System')}/{_ns('EventID')}").text or 0)
        mce.append({"time": t, "event": eid, "fatal": eid in (18, 20, 46, 47),
                    "apic": int(data["ApicId"]) if data.get("ApicId", "").isdigit() else None,
                    "bank": int(data["MCABank"]) if data.get("MCABank", "").isdigit() else None,
                    "type": data.get("ErrorType")})
    power = [_when(ev) for ev in _events("*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=41)]]")]
    power = [t for t in power if t >= since]
    by_apic: dict = {}
    for m in mce:
        if m["apic"] is not None:
            b = by_apic.setdefault(m["apic"], {"apic": m["apic"], "errors": 0, "fatal": 0, "last": 0.0})
            b["errors"] += 1
            b["fatal"] += int(m["fatal"])
            b["last"] = max(b["last"], m["time"])
    for b in by_apic.values():
        b["logical"] = logical_for_apic(b["apic"])
    return {"machine_checks": sorted(mce, key=lambda m: -m["time"]), "by_apic": sorted(by_apic.values(), key=lambda b: -b["errors"]),
            "power_losses": sorted(power, reverse=True)}


# ---- one reading --------------------------------------------------------------------------------------------------- #

class _GPU:
    """GPU engine load (3D/compute) and dedicated memory from Windows' GPU performance counters."""

    def __init__(self):
        self.q = None
        if not WIN:
            return
        try:
            import win32pdh
            self.pdh = win32pdh
            self.q = win32pdh.OpenQuery()
            self.util = win32pdh.AddEnglishCounter(self.q, r"\GPU Engine(*)\Utilization Percentage")
            self.mem = win32pdh.AddEnglishCounter(self.q, r"\GPU Adapter Memory(*)\Dedicated Usage")
            win32pdh.CollectQueryData(self.q)
        except Exception:                                    # noqa: BLE001 - no counters: GPU columns stay empty
            self.q = None

    def read(self) -> dict:
        if self.q is None:
            return {}
        try:
            self.pdh.CollectQueryData(self.q)
            util = self.pdh.GetFormattedCounterArray(self.util, self.pdh.PDH_FMT_DOUBLE)
            mem = self.pdh.GetFormattedCounterArray(self.mem, self.pdh.PDH_FMT_LARGE)
        except Exception:                                    # noqa: BLE001
            return {}
        by_kind: dict[str, float] = {}
        for name, v in util.items():
            kind = name.rsplit("engtype_", 1)[-1] if "engtype_" in name else "other"
            by_kind[kind] = by_kind.get(kind, 0.0) + float(v)
        return {"gpu_3d": round(min(100.0, by_kind.get("3D", 0.0)), 1),
                "gpu_compute": round(min(100.0, sum(v for k, v in by_kind.items() if k.startswith("Compute"))), 1),
                "gpu_copy": round(min(100.0, by_kind.get("Copy", 0.0)), 1),
                "gpu_mem_gb": round(max(mem.values(), default=0) / 2**30, 2)}


class Sampler:
    def __init__(self):
        self.gpu = _GPU()
        self.prev_disk = psutil.disk_io_counters(perdisk=True) or {}
        self.prev_net = psutil.net_io_counters()
        self.prev_t = time.time()
        psutil.cpu_percent(percpu=True)
        self.me = psutil.Process(os.getpid())
        self.me.cpu_percent()

    def read(self) -> dict:
        now = time.time()
        dt = max(1e-3, now - self.prev_t)
        per = psutil.cpu_percent(percpu=True)
        freq = psutil.cpu_freq()
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        disks = {}
        cur = psutil.disk_io_counters(perdisk=True) or {}
        for name, c in cur.items():
            p = self.prev_disk.get(name)
            if not p:
                continue
            busy_ms = (getattr(c, "busy_time", 0) - getattr(p, "busy_time", 0)) or \
                      ((c.read_time - p.read_time) + (c.write_time - p.write_time))
            disks[name] = {"read_mb_s": round((c.read_bytes - p.read_bytes) / dt / 2**20, 2),
                           "write_mb_s": round((c.write_bytes - p.write_bytes) / dt / 2**20, 2),
                           "iops": round(((c.read_count - p.read_count) + (c.write_count - p.write_count)) / dt, 1),
                           "busy": round(min(100.0, busy_ms / (dt * 10)), 1)}
        net = psutil.net_io_counters()
        out = {"t": now, "cpu": round(sum(per) / max(1, len(per)), 1), "cpu_max": max(per or [0]), "per_cpu": per,
               "freq_mhz": round(freq.current) if freq else None, "ram_used": round(vm.percent, 1),
               "ram_avail_gb": round(vm.available / 2**30, 2), "swap_used": round(sw.percent, 1),
               "net_rx_mb_s": round((net.bytes_recv - self.prev_net.bytes_recv) / dt / 2**20, 3),
               "net_tx_mb_s": round((net.bytes_sent - self.prev_net.bytes_sent) / dt / 2**20, 3),
               "disks": disks, "abp_cpu": round(self.me.cpu_percent() / max(1, len(per)), 2),
               "procs": len(psutil.pids()), **self.gpu.read()}
        self.prev_disk, self.prev_net, self.prev_t = cur, net, now
        return out


_sampler: Optional[Sampler] = None


def sample() -> dict:
    global _sampler
    if _sampler is None:
        _sampler = Sampler()
        time.sleep(0.5)
    return _sampler.read()


# ---- the recorder -------------------------------------------------------------------------------------------------- #

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(db_path(), timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS samples (t REAL PRIMARY KEY, cpu REAL, cpu_max REAL, ram_used REAL, swap_used REAL, "
              "gpu_mem_gb REAL, gpu_compute REAL, gpu_3d REAL, freq_mhz REAL, data TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS events (t REAL, kind TEXT, data TEXT)")
    return c


def record(s: dict) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (s["t"], s["cpu"], s["cpu_max"], s["ram_used"], s["swap_used"], s.get("gpu_mem_gb"), s.get("gpu_compute"),
                   s.get("gpu_3d"), s.get("freq_mhz"), json.dumps(s)))


def log_event(kind: str, data: dict) -> None:
    with _db() as c:
        c.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), kind, json.dumps(data)))


def history(seconds: float = 3600, limit: int = 100000) -> list[dict]:
    with _db() as c:
        rows = c.execute("SELECT data FROM samples WHERE t >= ? ORDER BY t LIMIT ?", (time.time() - seconds, limit)).fetchall()
    return [json.loads(r[0]) for r in rows]


def events(kind: str = "", seconds: float = 30 * 86400) -> list[dict]:
    with _db() as c:
        q = "SELECT t, kind, data FROM events WHERE t >= ?" + (" AND kind = ?" if kind else "") + " ORDER BY t"
        rows = c.execute(q, (time.time() - seconds, kind) if kind else (time.time() - seconds,)).fetchall()
    return [{"t": t, "kind": k, **json.loads(d)} for t, k, d in rows]


def stats() -> dict:
    with _db() as c:
        n, first, last = c.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM samples").fetchone()
        ev = c.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall()
    return {"samples": n, "first": first, "last": last, "events": dict(ev), "db": str(db_path())}


class Recorder:
    def __init__(self, interval: float = 10.0):
        self.interval = interval
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> "Recorder":
        if self.thread and self.thread.is_alive():
            return self
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, name="abp-telemetry", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last_prune = 0.0
        while not self._stop.wait(self.interval):
            try:
                record(sample())
                if time.time() - last_prune > 3600:
                    with _db() as c:
                        c.execute("DELETE FROM samples WHERE t < ?", (time.time() - KEEP_DAYS * 86400,))
                    last_prune = time.time()
            except (OSError, sqlite3.Error, psutil.Error):
                continue
