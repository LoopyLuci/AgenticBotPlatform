"""Every drive and volume on this machine, their health, and how likely each drive is to fail.

Sources, best first: smartctl (smartmontools, if installed: full SMART for SATA/SAS/NVMe/USB bridges); on Windows
Get-PhysicalDisk and Get-StorageReliabilityCounter (the latter needs an elevated ABP for some fields); on Linux lsblk
and /sys. Volumes (letters / mount points, sizes, free space) always come from psutil.

risk()  a logistic failure-risk estimate per drive from the SMART attributes that Backblaze's published drive-stats
        analyses tie to failure (5 reallocated, 187 uncorrectable, 188 command timeouts, 197 pending, 198 offline
        uncorrectable), NVMe critical warnings / media errors / wear, the OS's own health verdict, age, and whether the
        counts are rising between checks (the history is kept). The weights are set from those findings, not trained
        on this machine: the score orders drives by concern and explains itself; it is not a guarantee either way.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

import psutil

from bot.fileserver.store import db

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(argv: list[str], timeout: float = 30) -> Optional[str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode in (0, 4) or r.stdout.strip().startswith(("{", "[")) else None


def _ps_json(script: str) -> Any:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return None
    out = _run([exe, "-NoProfile", "-NonInteractive", "-Command", script + " | ConvertTo-Json -Depth 4 -Compress"], timeout=60)
    if not out or not out.strip():
        return None
    try:
        v = json.loads(out)
    except ValueError:
        return None
    return v if isinstance(v, list) else [v]


def volumes() -> list[dict]:
    out = []
    for p in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (OSError, PermissionError):
            continue
        out.append({"mount": p.mountpoint, "device": p.device, "fs": p.fstype, "size": u.total, "used": u.used,
                    "free": u.free, "percent": u.percent, "options": p.opts})
    return out


def _smartctl() -> list[dict]:
    exe = shutil.which("smartctl")
    if not exe:
        return []
    scan = _run([exe, "--scan-open", "-j"])
    if not scan:
        return []
    out = []
    for dev in json.loads(scan).get("devices", []):
        raw = _run([exe, "-a", "-j", dev["name"]] + (["-d", dev["type"]] if dev.get("type") else []), timeout=60)
        if not raw:
            continue
        j = json.loads(raw)
        attrs = {a["id"]: a.get("raw", {}).get("value", 0) for a in (j.get("ata_smart_attributes") or {}).get("table", [])}
        nv = j.get("nvme_smart_health_information_log") or {}
        out.append({"source": "smartctl", "device": dev["name"], "model": j.get("model_name") or j.get("model_family", ""),
                    "serial": j.get("serial_number", ""), "size": (j.get("user_capacity") or {}).get("bytes", 0),
                    "rotational": (j.get("rotation_rate") or 0) > 0, "bus": (j.get("device") or {}).get("protocol", ""),
                    "smart_passed": (j.get("smart_status") or {}).get("passed"),
                    "temperature_c": (j.get("temperature") or {}).get("current"),
                    "power_on_hours": (j.get("power_on_time") or {}).get("hours") or attrs.get(9),
                    "attributes": {k: attrs.get(k) for k in (5, 187, 188, 197, 198, 199, 194) if k in attrs},
                    "nvme": {k: nv.get(k) for k in ("critical_warning", "media_errors", "percentage_used", "available_spare",
                                                    "unsafe_shutdowns") if k in nv} or None})
    return out


def _windows() -> list[dict]:
    disks = _ps_json("Get-PhysicalDisk | Select-Object DeviceId,FriendlyName,SerialNumber,MediaType,BusType,Size,HealthStatus,"
                     "OperationalStatus,SpindleSpeed") or []
    rel = {}
    for r in _ps_json("Get-PhysicalDisk | ForEach-Object { $c = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; "
                      "if ($c) { [pscustomobject]@{DeviceId=$_.DeviceId; Temperature=$c.Temperature; Wear=$c.Wear; "
                      "ReadErrorsUncorrected=$c.ReadErrorsUncorrected; WriteErrorsUncorrected=$c.WriteErrorsUncorrected; "
                      "PowerOnHours=$c.PowerOnHours} } }") or []:
        rel[str(r.get("DeviceId"))] = r
    parts = _ps_json("Get-Partition | Where-Object DriveLetter | Select-Object DiskNumber,DriveLetter") or []
    letters: dict[str, list[str]] = {}
    for p in parts:
        letters.setdefault(str(p.get("DiskNumber")), []).append(f"{p.get('DriveLetter')}:")
    out = []
    for d in disks:
        did = str(d.get("DeviceId"))
        r = rel.get(did, {})
        health = d.get("HealthStatus")
        health = {0: "Healthy", 1: "Warning", 2: "Unhealthy"}.get(health, health) if isinstance(health, int) else health
        media = d.get("MediaType")
        media = {3: "HDD", 4: "SSD", 5: "SCM"}.get(media, media) if isinstance(media, int) else media
        out.append({"source": "windows", "device": f"PhysicalDrive{did}", "model": (d.get("FriendlyName") or "").strip(),
                    "serial": (d.get("SerialNumber") or "").strip(), "size": d.get("Size") or 0,
                    "rotational": media == "HDD" or (d.get("SpindleSpeed") or 0) > 0, "media": media,
                    "bus": d.get("BusType"), "os_health": health, "temperature_c": r.get("Temperature") or None,
                    "power_on_hours": r.get("PowerOnHours"), "wear_percent": r.get("Wear"),
                    "read_errors_uncorrected": r.get("ReadErrorsUncorrected"), "volumes": letters.get(did, []),
                    "reliability_counters": bool(r)})
    return out


def _linux() -> list[dict]:
    raw = _run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,ROTA,TRAN,MOUNTPOINTS"])
    if not raw:
        return []
    out = []
    for d in json.loads(raw).get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        mounts = []
        for c in d.get("children") or []:
            mounts += [m for m in (c.get("mountpoints") or []) if m]
        temp = None
        try:
            hw = list(__import__("pathlib").Path(f"/sys/block/{d['name']}/device").glob("hwmon/hwmon*/temp1_input"))
            if hw:
                temp = int(hw[0].read_text()) / 1000
        except OSError:
            pass
        out.append({"source": "lsblk", "device": f"/dev/{d['name']}", "model": (d.get("model") or "").strip(),
                    "serial": d.get("serial") or "", "size": int(d.get("size") or 0), "rotational": bool(d.get("rota")),
                    "bus": d.get("tran"), "volumes": mounts, "temperature_c": temp})
    return out


def _con():
    con = db("disks")
    con.execute("CREATE TABLE IF NOT EXISTS history(serial TEXT, at INT, data TEXT)")
    return con


def risk(d: dict, history: Optional[list[dict]] = None) -> dict:
    a = {int(k): (v or 0) for k, v in (d.get("attributes") or {}).items()}
    nv = d.get("nvme") or {}
    reasons = []
    z = -5.0

    def add(w, why):
        nonlocal z
        if w > 0:
            z += w
            reasons.append(why)
    add(2.2 * math.log1p(a.get(5, 0)), f"{a.get(5)} reallocated sectors")
    add(1.6 * math.log1p(a.get(187, 0)), f"{a.get(187)} uncorrectable errors reported")
    add(0.8 * math.log1p(a.get(188, 0)), f"{a.get(188)} command timeouts")
    add(2.0 * math.log1p(a.get(197, 0)), f"{a.get(197)} sectors pending reallocation")
    add(2.0 * math.log1p(a.get(198, 0)), f"{a.get(198)} offline uncorrectable sectors")
    add(1.5 * math.log1p(d.get("read_errors_uncorrected") or 0), f"{d.get('read_errors_uncorrected')} uncorrected read errors")
    if nv:
        add(3.0 if (nv.get("critical_warning") or 0) else 0, "the NVMe drive reports a critical warning")
        add(1.5 * math.log1p(nv.get("media_errors") or 0), f"{nv.get('media_errors')} NVMe media errors")
        add(0.05 * max(0, (nv.get("percentage_used") or 0) - 80), f"{nv.get('percentage_used')}% of its rated wear used")
        if nv.get("available_spare") is not None and nv["available_spare"] < 20:
            add(2.0, f"only {nv['available_spare']}% spare blocks left")
    wear = d.get("wear_percent")
    if wear:
        add(0.05 * max(0, wear - 80), f"{wear}% wear")
    if d.get("smart_passed") is False:
        add(4.0, "the drive's own SMART self-assessment FAILED")
    if d.get("os_health") not in (None, "Healthy"):
        add(3.5, f"the operating system rates it {d.get('os_health')}")
    hours = d.get("power_on_hours") or 0
    add(0.25 * max(0, (hours - 40000) / 10000), f"{hours} hours powered on")
    t = d.get("temperature_c")
    if t and t > 55:
        add(0.04 * (t - 55), f"running hot ({t} °C)")
    if history and len(history) >= 2:
        first = history[0].get("attributes") or {}
        grown = [k for k in ("5", "187", "197", "198") if (a.get(int(k), 0) or 0) > (first.get(k) or first.get(int(k)) or 0)]
        if grown:
            add(1.0, f"error counts rising since {time.strftime('%Y-%m-%d', time.localtime(history[0]['at']))} (attributes {', '.join(grown)})")
    p = 1 / (1 + math.exp(-z))
    band = "ok" if p < 0.05 else "watch" if p < 0.25 else "replace soon"
    known = bool(a or nv or d.get("os_health") or d.get("smart_passed") is not None or d.get("reliability_counters"))
    return {"probability": round(p, 3), "band": band if known else "unknown", "reasons": reasons or (["no warning signs"] if known else
            ["no health data: install smartmontools (smartctl), or run ABP elevated for Windows' reliability counters"])}


def inventory(record: bool = True) -> dict:
    drives = _smartctl()
    if not drives:
        drives = _windows() if sys.platform == "win32" else _linux() if sys.platform.startswith("linux") else []
    con = _con()
    now = int(time.time())
    for d in drives:
        key = d.get("serial") or d.get("device")
        hist = [{"at": r[0], **json.loads(r[1])} for r in con.execute(
            "SELECT at, data FROM history WHERE serial=? ORDER BY at", (key,))]
        d["risk"] = risk(d, hist)
        if record and (not hist or now - hist[-1]["at"] > 3600):
            con.execute("INSERT INTO history(serial, at, data) VALUES(?,?,?)",
                        (key, now, json.dumps({"attributes": d.get("attributes"), "nvme": d.get("nvme"), "temperature_c": d.get("temperature_c"),
                                               "os_health": d.get("os_health")})))
    con.commit()
    con.close()
    return {"drives": drives, "volumes": volumes(), "smartctl": bool(shutil.which("smartctl")), "at": now}


def system_stats() -> dict:
    """The dashboard numbers: CPU, memory, network and disk throughput, temperatures where the OS gives them."""
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    dio = psutil.disk_io_counters()
    temps = {}
    if hasattr(psutil, "sensors_temperatures"):
        try:
            for k, v in (psutil.sensors_temperatures() or {}).items():
                temps[k] = [round(x.current, 1) for x in v][:8]
        except Exception:  # noqa: BLE001
            pass
    return {"cpu_percent": psutil.cpu_percent(interval=0.2), "cpus": psutil.cpu_count(), "memory": {"total": vm.total, "used": vm.used,
            "percent": vm.percent}, "net": {"sent": net.bytes_sent, "recv": net.bytes_recv},
            "disk_io": {"read": dio.read_bytes, "write": dio.write_bytes} if dio else None, "temperatures": temps,
            "uptime_s": int(time.time() - psutil.boot_time())}
