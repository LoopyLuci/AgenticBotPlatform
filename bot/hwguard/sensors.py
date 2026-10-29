"""One reading of this machine's health. Nothing here raises; a value that cannot be read is None, and `unavailable`
says why in plain words.

    snapshot(disks=None) -> {"ts", "cpu": {...}, "gpu": {...}, "memory": {...}, "disks": [...], "unavailable": [...]}

GPU VRAM comes from the OS's GPU Adapter Memory counters (all processes on the card), not from a Vulkan or DXGI
per-process budget, which reports what one process may use and is wrong for "how full is the card". The card's size
is DXGI's DedicatedVideoMemory (bot/gpu.py). Temperatures: LibreHardwareMonitor / OpenHardwareMonitor's WMI sensors when
one of them is running, else the ACPI thermal zones; many desktop boards expose neither, and then there is no CPU or GPU
temperature (the AMD driver publishes none through Windows).
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from typing import Any, Optional

GIB = float(1 << 30)

# One PowerShell call per sample: counters, the hardware-monitor WMI sensors and the ACPI zones, as JSON.
_PS = r"""
$o=@{gpu_mem=@();gpu_eng=@();lhm=@();acpi=@()}
try{(Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -EA Stop).CounterSamples|%{$o.gpu_mem+=@{p=$_.InstanceName;v=$_.CookedValue}}}catch{}
try{(Get-Counter '\GPU Engine(*)\Utilization Percentage' -EA Stop).CounterSamples|?{$_.CookedValue -gt 0}|%{$o.gpu_eng+=@{p=$_.InstanceName;v=$_.CookedValue}}}catch{}
foreach($ns in 'root/LibreHardwareMonitor','root/OpenHardwareMonitor'){try{Get-CimInstance -Namespace $ns -ClassName Sensor -EA Stop|?{$_.SensorType -eq 'Temperature'}|%{$o.lhm+=@{n=$_.Name;i=$_.Identifier;v=$_.Value}}}catch{}}
try{Get-CimInstance -Namespace root/wmi MSAcpi_ThermalZoneTemperature -EA Stop|%{$o.acpi+=@{n=$_.InstanceName;v=$_.CurrentTemperature}}}catch{}
$o|ConvertTo-Json -Depth 4 -Compress
"""


class _PerfInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("cb", "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
                                               "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + \
               [("HandleCount", ctypes.c_uint32), ("ProcessCount", ctypes.c_uint32), ("ThreadCount", ctypes.c_uint32)]


def _commit() -> Optional[dict]:
    if sys.platform != "win32":
        return None
    try:
        pi = _PerfInfo()
        pi.cb = ctypes.sizeof(pi)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
            return None
        used, limit = pi.CommitTotal * pi.PageSize, pi.CommitLimit * pi.PageSize
        return {"used_gb": round(used / GIB, 2), "limit_gb": round(limit / GIB, 2), "pct": round(100.0 * used / limit, 1) if limit else None}
    except Exception:  # noqa: BLE001
        return None


def _powershell() -> dict:
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS], capture_output=True, text=True,
                             timeout=25, errors="replace", creationflags=0x08000000)
        got = json.loads(res.stdout.strip() or "{}")
        return got if isinstance(got, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else [v] if isinstance(v, dict) else []


def parse_windows(raw: dict, total_vram_gb: Optional[float]) -> tuple[dict, dict, list[str]]:
    """(gpu, temps, unavailable) from the PowerShell sample. Pure, for tests."""
    unavailable: list[str] = []
    mem = [r for r in _as_list(raw.get("gpu_mem")) if isinstance(r, dict)]
    used = max((float(r.get("v") or 0) for r in mem), default=None) if mem else None
    by_type: dict[str, float] = {}
    for r in _as_list(raw.get("gpu_eng")):
        name = str(r.get("p") or "")
        et = name.rsplit("engtype_", 1)[-1] if "engtype_" in name else "other"
        by_type[et] = by_type.get(et, 0.0) + float(r.get("v") or 0)
    load = min(100.0, max(by_type.values())) if by_type else (0.0 if mem else None)
    gpu = {"vram_used_gb": None if used is None else round(used / GIB, 2), "vram_total_gb": total_vram_gb,
           "vram_pct": round(100.0 * used / GIB / total_vram_gb, 1) if used is not None and total_vram_gb else None,
           "load_pct": None if load is None else round(load, 1), "temp_c": None, "source": "os counters" if mem else None}
    if not mem:
        unavailable.append("GPU memory: the OS GPU counters gave nothing")
    cpu_t = gpu_t = None
    for s in _as_list(raw.get("lhm")):
        ident, val = str(s.get("i") or "").lower(), s.get("v")
        if not isinstance(val, (int, float)):
            continue
        if "/gpu" in ident or "gpu" in str(s.get("n") or "").lower():
            gpu_t = max(gpu_t or 0.0, float(val))
        elif "cpu" in ident or "tctl" in str(s.get("n") or "").lower():
            cpu_t = max(cpu_t or 0.0, float(val))
    src = "hardware monitor" if (cpu_t or gpu_t) else None
    if cpu_t is None:
        zones = [float(z["v"]) / 10.0 - 273.15 for z in _as_list(raw.get("acpi")) if isinstance(z.get("v"), (int, float)) and z["v"] > 0]
        if zones:
            cpu_t, src = max(zones), "acpi thermal zone"
    if cpu_t is None:
        unavailable.append("CPU temperature: Windows exposes no ACPI thermal zone here and no LibreHardwareMonitor is running")
    if gpu_t is None:
        unavailable.append("GPU temperature: the AMD driver publishes none through Windows; it needs LibreHardwareMonitor running")
    else:
        gpu["temp_c"] = round(gpu_t, 1)
    return gpu, {"cpu_c": None if cpu_t is None else round(cpu_t, 1), "gpu_c": gpu["temp_c"], "source": src}, unavailable


def snapshot(disks: Optional[list[str]] = None) -> dict:
    import psutil

    unavailable: list[str] = []
    try:
        per = psutil.cpu_percent(interval=0.5, percpu=True)
    except Exception:  # noqa: BLE001
        per = []
    cpu = {"load_pct": round(sum(per) / len(per), 1) if per else None, "max_core_pct": max(per) if per else None,
           "cores": psutil.cpu_count(logical=True) or 1, "temp_c": None}
    total_vram = None
    try:
        from bot import gpu as _gpu

        total_vram = _gpu.gpu_status().get("total_gb")
    except Exception:  # noqa: BLE001
        pass
    gpu: dict = {"vram_used_gb": None, "vram_total_gb": total_vram, "vram_pct": None, "load_pct": None, "temp_c": None, "source": None}
    temps: dict = {"cpu_c": None, "gpu_c": None, "source": None}
    if sys.platform == "win32":
        gpu, temps, un = parse_windows(_powershell(), total_vram)
        unavailable += un
    else:
        try:
            t = psutil.sensors_temperatures() or {}
            vals = [x.current for k in ("k10temp", "coretemp", "cpu_thermal", "zenpower") for x in t.get(k, [])]
            temps["cpu_c"] = max(vals) if vals else None
            gvals = [x.current for x in t.get("amdgpu", [])]
            temps["gpu_c"] = gpu["temp_c"] = max(gvals) if gvals else None
        except Exception:  # noqa: BLE001
            pass
        if temps["cpu_c"] is None:
            unavailable.append("CPU temperature: no sensor found")
    cpu["temp_c"] = temps["cpu_c"]
    vm = psutil.virtual_memory()
    try:
        sw = psutil.swap_memory()
        swap = {"used_gb": round(sw.used / GIB, 2), "total_gb": round(sw.total / GIB, 2), "pct": sw.percent}
    except Exception:  # noqa: BLE001
        swap = None
    memory = {"available_gb": round(vm.available / GIB, 2), "total_gb": round(vm.total / GIB, 2), "pct": vm.percent,
              "pagefile": swap, "commit": _commit()}
    rows = []
    try:
        mounts = disks or [p.mountpoint for p in psutil.disk_partitions(all=False) if "cdrom" not in (p.opts or "") and p.fstype]
    except Exception:  # noqa: BLE001
        mounts = disks or []
    for m in mounts:
        try:
            du = psutil.disk_usage(m)
            rows.append({"mount": m, "free_gb": round(du.free / GIB, 1), "total_gb": round(du.total / GIB, 1), "pct": du.percent})
        except Exception:  # noqa: BLE001
            continue
    return {"ts": time.time(), "cpu": cpu, "gpu": gpu, "temps": temps, "memory": memory, "disks": rows, "unavailable": unavailable}
