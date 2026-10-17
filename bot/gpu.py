"""Spare-VRAM measurement that works on any GPU vendor (docs/gpu.md).

Primary source on Windows is DXGI (IDXGIAdapter3::QueryVideoMemoryInfo, local segment), driven through ctypes: it
reports the OS video-memory budget and the current usage across all processes, on AMD, NVIDIA and Intel alike.
Fallbacks: nvidia-smi, then the Linux amdgpu sysfs counters, then the registry total with an unknown free figure.
Nothing here raises: an unmeasurable box gives `{"known": False, ...}`.

    gpu_status()            -> {"known", "source", "adapters": [...], "total_gb", "used_gb", "free_gb", "budget_gb"}
    free_vram_gb()          -> float | None
    can_fit(need_gb, headroom_gb=1.0) -> bool | None   (None = could not measure)
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

GIB = float(1 << 30)


def _gb(n: int) -> float:
    return round(n / GIB, 2)


# ---- DXGI ---------------------------------------------------------------------------------------------------------- #

class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "_GUID":
        u = uuid.UUID(s)
        g = cls()
        g.a, g.b, g.c = u.time_low, u.time_mid, u.time_hi_version
        for i, byte in enumerate(u.bytes[8:]):
            g.d[i] = byte
        return g


class _Desc1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("LuidLow", ctypes.c_uint), ("LuidHigh", ctypes.c_int), ("Flags", ctypes.c_uint)]


class _MemInfo(ctypes.Structure):
    _fields_ = [("Budget", ctypes.c_uint64), ("CurrentUsage", ctypes.c_uint64),
                ("AvailableForReservation", ctypes.c_uint64), ("CurrentReservation", ctypes.c_uint64)]


_VENDORS = {0x1002: "amd", 0x10DE: "nvidia", 0x8086: "intel", 0x1414: "microsoft"}
_IID_FACTORY1 = "770AAE78-F26F-4DBA-A829-253C83D1B387"
_IID_ADAPTER3 = "645967A4-1392-4310-A798-8053CE3E93FD"


def _call(obj, index: int, restype, *argtypes):
    """Slot `index` of a COM object's vtable, as a callable taking the object first."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(p) -> None:
    if p:
        _call(p, 2, ctypes.c_ulong)(p)


def _dxgi_adapters() -> list[dict]:
    if sys.platform != "win32":
        return []
    dxgi = ctypes.WinDLL("dxgi")
    create = dxgi.CreateDXGIFactory1
    create.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long
    factory = ctypes.c_void_p()
    if create(ctypes.byref(_GUID.of(_IID_FACTORY1)), ctypes.byref(factory)) < 0:
        return []
    out = []
    try:
        for i in range(32):
            ad = ctypes.c_void_p()
            if _call(factory, 12, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(factory, i, ctypes.byref(ad)) < 0:
                break
            try:
                d = _Desc1()
                if _call(ad, 10, ctypes.c_long, ctypes.POINTER(_Desc1))(ad, ctypes.byref(d)) < 0:
                    continue
                if d.Flags & 2 or d.VendorId == 0x1414:        # software adapter / Microsoft Basic Render
                    continue
                row = {"name": d.Description, "vendor": _VENDORS.get(d.VendorId, "other"), "total": int(d.DedicatedVideoMemory),
                       "budget": None, "used": None}
                ad3 = ctypes.c_void_p()
                qi = _call(ad, 0, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
                if qi(ad, ctypes.byref(_GUID.of(_IID_ADAPTER3)), ctypes.byref(ad3)) >= 0:
                    try:
                        m = _MemInfo()
                        # node 0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL (0) = the video memory on the card
                        if _call(ad3, 14, ctypes.c_long, ctypes.c_uint, ctypes.c_int, ctypes.POINTER(_MemInfo))(ad3, 0, 0, ctypes.byref(m)) >= 0:
                            row["budget"], row["used"] = int(m.Budget), int(m.CurrentUsage)
                    finally:
                        _release(ad3)
                out.append(row)
            finally:
                _release(ad)
    finally:
        _release(factory)
    return out


def _smi_adapters() -> list[dict]:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in r.stdout.splitlines():
        try:
            n, t, u = [x.strip() for x in line.split(",")]
            out.append({"name": n, "vendor": "nvidia", "total": int(float(t)) << 20, "budget": int(float(t)) << 20, "used": int(float(u)) << 20})
        except ValueError:
            continue
    return out


def _sysfs_adapters() -> list[dict]:
    out = []
    try:
        for d in sorted(Path("/sys/class/drm").glob("card*/device")):
            try:
                t = int((d / "mem_info_vram_total").read_text())
                u = int((d / "mem_info_vram_used").read_text())
            except (OSError, ValueError):
                continue
            out.append({"name": "AMD GPU", "vendor": "amd", "total": t, "budget": t, "used": u})
    except OSError:
        pass
    return out


def _registry_adapters() -> list[dict]:
    try:
        from bot.localai import engine
        return [{"name": g["name"], "vendor": g["vendor"], "total": int(g["memory"]), "budget": None, "used": None} for g in engine.gpus()]
    except Exception:  # noqa: BLE001 - a fallback must never raise
        return []


def _probe() -> tuple[str, list[dict]]:
    """(source, adapters) from the first source that answers. The seam the tests replace."""
    for source, fn in (("dxgi", _dxgi_adapters), ("nvidia-smi", _smi_adapters), ("sysfs", _sysfs_adapters), ("registry", _registry_adapters)):
        try:
            rows = [r for r in fn() if r["total"] > 0]
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            return source, rows
    return "none", []


def _row(r: dict) -> dict:
    total, used, budget = r["total"], r.get("used"), r.get("budget")
    free = None if used is None else max(0, (budget or total) - used)
    return {"name": r["name"], "vendor": r["vendor"], "total_gb": _gb(total), "used_gb": None if used is None else _gb(used),
            "free_gb": None if free is None else _gb(free), "budget_gb": None if budget is None else _gb(budget)}


def gpu_status() -> dict:
    """Measured VRAM now. The headline numbers are for the adapter with the most memory (the one models load on)."""
    try:
        source, rows = _probe()
    except Exception:  # noqa: BLE001
        source, rows = "none", []
    adapters = [_row(r) for r in rows]
    if not adapters:
        return {"known": False, "source": source, "adapters": [], "name": None, "total_gb": None, "used_gb": None,
                "free_gb": None, "budget_gb": None}
    best = max(adapters, key=lambda a: a["total_gb"])
    return {"known": best["free_gb"] is not None, "source": source, "adapters": adapters, "name": best["name"],
            "total_gb": best["total_gb"], "used_gb": best["used_gb"], "free_gb": best["free_gb"], "budget_gb": best["budget_gb"]}


def total_vram_bytes() -> int:
    """Measured total VRAM of the largest adapter in bytes, 0 when unknown."""
    t = gpu_status()["total_gb"]
    return int(t * GIB) if t else 0


def free_vram_gb() -> Optional[float]:
    """GB of video memory free right now, or None when it cannot be measured."""
    return gpu_status()["free_gb"]


def can_fit(need_gb: float, headroom_gb: float = 1.0) -> Optional[bool]:
    """True when `need_gb` plus `headroom_gb` fits in the free VRAM; None when free VRAM is unknown."""
    free = free_vram_gb()
    return None if free is None else free >= float(need_gb) + float(headroom_gb)


def fit_report(need_gb: Optional[float] = None, headroom_gb: float = 1.0) -> dict:
    """gpu_status() plus, when `need_gb` is given, whether that size fits (`fits`: True/False/None)."""
    st = gpu_status()
    if need_gb is not None:
        free = st["free_gb"]
        st["need_gb"], st["headroom_gb"] = float(need_gb), float(headroom_gb)
        st["fits"] = None if free is None else free >= float(need_gb) + float(headroom_gb)
    return st
