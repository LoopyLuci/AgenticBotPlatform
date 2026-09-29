"""What this machine has, for the cluster: hardware, software, and live load.

The static part (CPU model, RAM, GPUs and their memory, disks, OS, hypervisors, toolchains) is measured once and
kept for ten minutes; the live part (CPU and RAM in use, GPU memory in use where Windows or the vendor tool reports
it, disk free) is measured at most every few seconds. Everything is best-effort: a probe that fails leaves its
field empty rather than failing the whole inventory.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import psutil

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_VIRTUAL_GPU_WORDS = ("basic display", "basic render", "virtual", "parsec", "remote display", "idd", "spacedesk",
                      "citrix", "vmware svga", "hyper-v video")
_static: dict[str, Any] = {"at": 0.0, "data": None}
_live: dict[str, Any] = {"at": 0.0, "data": None}
_lock = threading.Lock()
psutil.cpu_percent(interval=None)          # primes the counter: later calls measure since the previous one


def _run(args: list[str], timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def this_os() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _cpu_model() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _nvidia_gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                gpus.append({"vendor": "nvidia", "name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 2),
                             "vram_used_gb": round(float(parts[2]) / 1024, 2), "util_pct": float(parts[3])})
            except ValueError:
                continue
    return gpus


def _windows_gpus() -> list[dict]:
    """Display adapters from the registry: the name, and the real dedicated memory (the qwMemorySize value;
    WMI's AdapterRAM stops at 4 GB)."""
    try:
        import winreg
    except ImportError:
        return []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    gpus = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as k:
                    name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
                    mem = 0
                    for value in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                        try:
                            v = winreg.QueryValueEx(k, value)[0]
                            mem = int.from_bytes(v, "little") if isinstance(v, (bytes, bytearray)) else int(v)
                            if mem:
                                break
                        except OSError:
                            continue
            except OSError:
                continue
            if any(w in name.lower() for w in _VIRTUAL_GPU_WORDS):
                continue
            vendor = "amd" if ("amd" in name.lower() or "radeon" in name.lower()) else \
                "nvidia" if "nvidia" in name.lower() else "intel" if "intel" in name.lower() else "other"
            gpus.append({"vendor": vendor, "name": name, "vram_gb": round(mem / 1024 ** 3, 2) if mem else None})
    return gpus


def _rocm_gpus() -> list[dict]:
    exe = shutil.which("rocm-smi")
    if not exe:
        return []
    try:
        data = json.loads(_run([exe, "--showproductname", "--showmeminfo", "vram", "--json"]) or "{}")
    except ValueError:
        return []
    gpus = []
    for card, d in sorted(data.items()):
        if not card.startswith("card"):
            continue
        total = float(d.get("VRAM Total Memory (B)") or 0)
        used = float(d.get("VRAM Total Used Memory (B)") or 0)
        gpus.append({"vendor": "amd", "name": d.get("Card series") or d.get("Card SKU") or card,
                     "vram_gb": round(total / 1024 ** 3, 2), "vram_used_gb": round(used / 1024 ** 3, 2)})
    return gpus


def gpus() -> list[dict]:
    found = _nvidia_gpus()
    if sys.platform == "win32":
        seen = {g["name"] for g in found}
        found += [g for g in _windows_gpus() if g["name"] not in seen]
    else:
        found += _rocm_gpus()
    for i, g in enumerate(found):
        g["index"] = i
    return found


def _windows_gpu_memory_used_gb() -> Optional[float]:
    """Dedicated GPU memory in use, summed over adapters (Windows' own GPU performance counters)."""
    out = _run(["typeperf", r"\GPU Adapter Memory(*)\Dedicated Usage", "-sc", "1"], timeout=10)
    lines = [ln for ln in out.splitlines() if ln.startswith('"') and "," in ln]
    if len(lines) < 2:
        return None
    try:
        values = [float(v.strip('"')) for v in lines[-1].split(",")[1:] if v.strip('"')]
    except ValueError:
        return None
    return round(sum(values) / 1024 ** 3, 2)


_gpu_mem: dict[str, Any] = {"value": None, "at": 0.0, "busy": False}


def _gpu_mem_background() -> Optional[float]:
    """The last GPU-memory reading, refreshed in the background (Windows' counters take ~2 s to read, and a node's
    report must not wait for them)."""
    if not _gpu_mem["busy"] and time.monotonic() - _gpu_mem["at"] > 15:
        _gpu_mem["busy"] = True

        def go() -> None:
            try:
                _gpu_mem["value"] = _windows_gpu_memory_used_gb()
            finally:
                _gpu_mem.update(at=time.monotonic(), busy=False)
        threading.Thread(target=go, name="gpu-mem", daemon=True).start()
    return _gpu_mem["value"]


def _hypervisors() -> list[str]:
    found = []
    if sys.platform == "win32":
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        if (system32 / "WinHvPlatform.dll").is_file():
            found.append("whp")
        if (system32 / "vmms.exe").is_file():
            found.append("hyperv")
    if Path("/dev/kvm").exists():
        found.append("kvm")
    if shutil.which("qemu-system-x86_64") or Path(r"C:\Program Files\qemu\qemu-system-x86_64.exe").is_file():
        found.append("qemu")
    if shutil.which("VBoxManage") or Path(r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe").is_file():
        found.append("virtualbox")
    return found


TOOLS = ("python", "git", "cargo", "node", "npm", "pnpm", "java", "docker", "go", "dotnet", "gcc", "cmake", "ollama")


def _toolchains() -> list[str]:
    have = [t for t in TOOLS if shutil.which(t)]
    if "cargo" not in have and (Path.home() / ".cargo" / "bin" / ("cargo.exe" if os.name == "nt" else "cargo")).is_file():
        have.append("cargo")
    return sorted(have)


def _modules() -> list[dict]:
    try:
        from bot.modules import harness
        return [{"id": r["id"], "ready": bool(r.get("ready")), "hub": bool((r.get("hub") or {}).get("running"))}
                for r in harness.overview() if r.get("installed")]
    except Exception:  # noqa: BLE001
        return []


def _local_models() -> list[str]:
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
        return sorted(m["name"] for m in (r.json().get("models") or []))
    except Exception:  # noqa: BLE001
        return []


def _addresses() -> dict:
    tailscale, lan = [], []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family != socket.AF_INET or a.address.startswith(("127.", "169.254.")):
                continue                  # loopback, and link-local addresses nothing else can reach
            (tailscale if a.address.startswith("100.") else lan).append(a.address)
    return {"tailscale": sorted(set(tailscale)), "lan": sorted(set(lan))}


def _disks() -> list[dict]:
    out = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        out.append({"mount": p.mountpoint, "fs": p.fstype, "total_gb": round(u.total / 1024 ** 3, 1),
                    "free_gb": round(u.free / 1024 ** 3, 1)})
    return out


def static(refresh: bool = False) -> dict:
    with _lock:
        if refresh or _static["data"] is None or time.monotonic() - _static["at"] > 600:
            vm = psutil.virtual_memory()
            _static["data"] = {
                "hostname": socket.gethostname(), "os": this_os(), "os_version": platform.platform(),
                "arch": platform.machine().lower(), "cpu": {"model": _cpu_model(),
                                                           "cores": psutil.cpu_count(logical=False) or 0,
                                                           "threads": psutil.cpu_count(logical=True) or 0},
                "ram_gb": round(vm.total / 1024 ** 3, 1), "gpus": gpus(), "hypervisors": _hypervisors(),
                "toolchains": _toolchains(), "addresses": _addresses(),
            }
            _static["at"] = time.monotonic()
        return dict(_static["data"])


def live(refresh: bool = False) -> dict:
    with _lock:
        fresh = _live["data"] is not None and time.monotonic() - _live["at"] < 5
    if fresh and not refresh:
        return dict(_live["data"])
    vm = psutil.virtual_memory()
    # cpu_pct: the average since the previous report (no sampling pause)
    data: dict[str, Any] = {"cpu_pct": psutil.cpu_percent(interval=None), "ram_used_gb": round(vm.used / 1024 ** 3, 1),
                            "ram_free_gb": round(vm.available / 1024 ** 3, 1), "disks": _disks(),
                            "uptime_s": int(time.time() - psutil.boot_time()), "at": time.time()}
    if sys.platform == "win32" and any(g.get("vram_used_gb") is None for g in static()["gpus"]):
        data["gpu_mem_used_gb"] = _gpu_mem_background()
    nv = _nvidia_gpus()
    if nv:
        data["gpus"] = [{"name": g["name"], "vram_used_gb": g["vram_used_gb"], "util_pct": g["util_pct"]} for g in nv]
    with _lock:
        _live.update(at=time.monotonic(), data=data)
    return dict(data)


def software() -> dict:
    """Installed modules and local models (a little slower: kept with the static part's cache)."""
    with _lock:
        cached = _static.get("software")
        if cached and time.monotonic() - cached["at"] < 120:
            return dict(cached["data"])
    data = {"modules": _modules(), "models": _local_models()}
    with _lock:
        _static["software"] = {"at": time.monotonic(), "data": data}
    return data
