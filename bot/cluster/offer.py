"""What this machine shares with the cluster (its owner's choice), and the budget that holds reservations against it.

The offer lives in config/backends.yaml under `cluster.offer` and is changed only through the desktop dashboard (a
linked peer can read it, never change it). Sharing is off until the owner turns it on:

    cluster:
      offer:
        enabled: false        # nothing is shared until this is true
        cpu_percent: 50       # the share of this machine's CPU threads jobs may use together
        ram_gb: 8             # memory jobs may use together
        gpus: []              # GPU indices jobs may use ("all" for every GPU)
        disk_gb: 20           # space jobs may fill in the work folder
        work_dir: ""          # where job folders go (default: data/cluster/work)
        kinds: [command, python, module_op, module_build, inference]
        peers: ["*"]          # which linked servers may use it, by name ("*" = every linked server)
        when: always          # or "idle": only while nobody has used this machine for idle_minutes
        idle_minutes: 10
        max_jobs: 4

The budget is capacity (from the offer and the real hardware) minus what running and accepted jobs reserved.
`try_reserve` checks and takes in one step under a lock, so two schedulers can never both get the last slot.
"""
from __future__ import annotations

import math
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bot.cluster import inventory

KINDS = ("command", "python", "module_op", "module_build", "inference")
DEFAULTS: dict[str, Any] = {"enabled": False, "cpu_percent": 50, "ram_gb": 8, "gpus": [], "disk_gb": 20, "work_dir": "",
                            "kinds": list(KINDS), "peers": ["*"], "when": "always", "idle_minutes": 10, "max_jobs": 4}


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict(((config.current or {}).get("cluster") or {}).get("offer") or {})
    except Exception:  # noqa: BLE001
        return {}


def current() -> dict:
    o = {**DEFAULTS, **{k: v for k, v in _cfg().items() if k in DEFAULTS}}
    o["kinds"] = [k for k in (o.get("kinds") or []) if k in KINDS]
    return o


def validate(changes: dict) -> dict:
    """Check an edit to the offer; return the cleaned values. Raises ValueError with a readable reason."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown offer setting {key!r}")
        if key == "enabled":
            out[key] = bool(value)
        elif key in ("cpu_percent",):
            v = float(value)
            if not 0 <= v <= 100:
                raise ValueError("cpu_percent is 0-100")
            out[key] = v
        elif key in ("ram_gb", "disk_gb"):
            v = float(value)
            if v < 0:
                raise ValueError(f"{key} can't be negative")
            out[key] = v
        elif key in ("idle_minutes", "max_jobs"):
            v = int(value)
            if v < 0 or v > 10_000:
                raise ValueError(f"{key} is out of range")
            out[key] = v
        elif key == "gpus":
            if value == "all":
                out[key] = "all"
            elif isinstance(value, list) and all(isinstance(i, int) and i >= 0 for i in value):
                out[key] = sorted(set(value))
            else:
                raise ValueError('gpus is a list of GPU indices, or "all"')
        elif key == "kinds":
            if not isinstance(value, list) or any(k not in KINDS for k in value):
                raise ValueError(f"kinds are from: {', '.join(KINDS)}")
            out[key] = list(value)
        elif key == "peers":
            if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
                raise ValueError('peers is a list of linked server names, or ["*"]')
            out[key] = list(value)
        elif key == "when":
            if value not in ("always", "idle"):
                raise ValueError('when is "always" or "idle"')
            out[key] = value
        elif key == "work_dir":
            out[key] = str(value or "")
    return out


def save(changes: dict) -> dict:
    from bot.config import config
    clean = validate(changes)
    config.set_values({("cluster", "offer", k): v for k, v in clean.items()}, actor="dashboard")
    return current()


def work_root() -> Path:
    o = current()
    if o.get("work_dir"):
        return Path(str(o["work_dir"])).expanduser()
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "data" / "cluster" / "work"


def idle_seconds() -> Optional[float]:
    """How long nobody has used this machine's keyboard or mouse (Windows); None where it can't be told."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
    except Exception:  # noqa: BLE001
        return None


def available_now(o: Optional[dict] = None) -> tuple[bool, str]:
    """Whether the offer is open right now (enabled, and idle enough when it only shares while idle)."""
    o = o or current()
    if not o["enabled"]:
        return False, "this machine does not share resources (its owner has not turned sharing on)"
    if o["when"] == "idle":
        idle = idle_seconds()
        if idle is None:
            return False, "this machine shares only while idle, and its idle time can't be read"
        if idle < float(o["idle_minutes"]) * 60:
            return False, f"this machine shares only after {o['idle_minutes']} idle minutes (someone is using it)"
    return True, ""


def peer_allowed(peer: Optional[str], o: Optional[dict] = None) -> bool:
    if peer is None:          # this machine itself
        return True
    allowed = (o or current())["peers"]
    return "*" in allowed or peer.lower() in {p.lower() for p in allowed}


@dataclass
class Request:
    """What one job needs. cpu is in threads (fractions allowed), ram_gb and vram_gb in GB, disk_gb in GB."""
    cpu: float = 1.0
    ram_gb: float = 1.0
    gpus: int = 0
    vram_gb: float = 0.0
    disk_gb: float = 0.0
    os: Optional[str] = None
    needs: list[str] = field(default_factory=list)       # hypervisors or toolchains, e.g. "kvm", "cargo"
    module: Optional[str] = None                          # a module that must be installed (and built)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Request":
        d = dict(d or {})
        return cls(cpu=float(d.get("cpu", 1)), ram_gb=float(d.get("ram_gb", 1)), gpus=int(d.get("gpus", 0)),
                   vram_gb=float(d.get("vram_gb", 0)), disk_gb=float(d.get("disk_gb", 0)), os=d.get("os") or None,
                   needs=[str(x) for x in d.get("needs") or []], module=d.get("module") or None)

    def public(self) -> dict:
        return {"cpu": self.cpu, "ram_gb": self.ram_gb, "gpus": self.gpus, "vram_gb": self.vram_gb,
                "disk_gb": self.disk_gb, "os": self.os, "needs": self.needs, "module": self.module}


class Budget:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, dict] = {}          # job id -> {"cpu", "ram_gb", "gpus": [indices], "disk_gb"}

    def capacity(self, o: Optional[dict] = None) -> dict:
        o = o or current()
        inv = inventory.static()
        threads = inv["cpu"]["threads"] or 1
        all_gpus = [g["index"] for g in inv["gpus"]]
        gpus = all_gpus if o["gpus"] == "all" else [i for i in (o["gpus"] or []) if i in all_gpus]
        return {"cpu": math.floor(threads * float(o["cpu_percent"]) / 100 * 10) / 10,
                "ram_gb": min(float(o["ram_gb"]), float(inv["ram_gb"])), "gpus": gpus,
                "vram_gb": {g["index"]: g.get("vram_gb") or 0 for g in inv["gpus"] if g["index"] in gpus},
                "disk_gb": float(o["disk_gb"]), "max_jobs": int(o["max_jobs"])}

    def free(self, o: Optional[dict] = None) -> dict:
        cap = self.capacity(o)
        with self._lock:
            held = list(self._held.values())
        used_gpus = {i for h in held for i in h["gpus"]}
        return {"cpu": round(cap["cpu"] - sum(h["cpu"] for h in held), 2),
                "ram_gb": round(cap["ram_gb"] - sum(h["ram_gb"] for h in held), 2),
                "gpus": [i for i in cap["gpus"] if i not in used_gpus],
                "vram_gb": {i: v for i, v in cap["vram_gb"].items() if i not in used_gpus},
                "disk_gb": round(cap["disk_gb"] - sum(h["disk_gb"] for h in held), 2),
                "slots": cap["max_jobs"] - len(held)}

    def reserved(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._held.items()}

    def fits(self, req: Request, peer: Optional[str] = None, kind: Optional[str] = None) -> tuple[bool, str]:
        """Whether this job could run here now; the reason if not. Doesn't reserve anything."""
        o = current()
        ok, why = available_now(o)
        if not ok:
            return False, why
        if not peer_allowed(peer, o):
            return False, f"this machine does not share with {peer}"
        if kind and kind not in o["kinds"]:
            return False, f"this machine does not run {kind} jobs (it allows: {', '.join(o['kinds']) or 'none'})"
        inv = inventory.static()
        if req.os and req.os != inv["os"]:
            return False, f"it needs {req.os}; this machine runs {inv['os']}"
        have = set(inv["hypervisors"]) | set(inv["toolchains"])
        missing = [n for n in req.needs if n not in have and not (n == "whp-or-kvm" and have & {"whp", "kvm"})]
        if missing:
            return False, "this machine lacks " + ", ".join(missing)
        if req.module:
            mods = {m["id"]: m for m in inventory.software()["modules"]}
            if req.module not in mods:
                return False, f"the {req.module} module is not installed here"
        f = self.free(o)
        if f["slots"] <= 0:
            return False, f"this machine already runs its maximum of {o['max_jobs']} jobs"
        if req.cpu > f["cpu"] + 1e-9:
            return False, f"it needs {req.cpu} CPU threads; {max(f['cpu'], 0)} are free of the offer"
        if req.ram_gb > f["ram_gb"] + 1e-9:
            return False, f"it needs {req.ram_gb} GB RAM; {max(f['ram_gb'], 0)} GB are free of the offer"
        if req.disk_gb > f["disk_gb"] + 1e-9:
            return False, f"it needs {req.disk_gb} GB disk; {max(f['disk_gb'], 0)} GB are free of the offer"
        if req.gpus:
            usable = [i for i in f["gpus"] if (f["vram_gb"].get(i) or 0) >= req.vram_gb]
            if len(usable) < req.gpus:
                return False, (f"it needs {req.gpus} GPU(s) with {req.vram_gb} GB each; "
                               f"{len(usable)} are free of the offer")
        return True, ""

    def try_reserve(self, job_id: str, req: Request, peer: Optional[str] = None,
                    kind: Optional[str] = None) -> tuple[bool, str, dict]:
        """Check and take in one step. Returns (ok, reason, what was reserved)."""
        with self._lock:
            if job_id in self._held:
                return True, "", dict(self._held[job_id])
        ok, why = self.fits(req, peer, kind)
        if not ok:
            return False, why, {}
        with self._lock:
            # re-check under the lock: another reservation may have landed between fits() and here
            o = current()
            cap = self.capacity(o)
            held = list(self._held.values())
            used_gpus = {i for h in held for i in h["gpus"]}
            free_cpu = cap["cpu"] - sum(h["cpu"] for h in held)
            free_ram = cap["ram_gb"] - sum(h["ram_gb"] for h in held)
            free_disk = cap["disk_gb"] - sum(h["disk_gb"] for h in held)
            if len(held) >= cap["max_jobs"] or req.cpu > free_cpu + 1e-9 or req.ram_gb > free_ram + 1e-9 \
                    or req.disk_gb > free_disk + 1e-9:
                return False, "another job just took the free share; try again", {}
            gpus = [i for i in cap["gpus"] if i not in used_gpus and (cap["vram_gb"].get(i) or 0) >= req.vram_gb][:req.gpus]
            if len(gpus) < req.gpus:
                return False, "another job just took the free GPU; try again", {}
            taken = {"cpu": req.cpu, "ram_gb": req.ram_gb, "gpus": gpus, "disk_gb": req.disk_gb}
            self._held[job_id] = taken
            return True, "", dict(taken)

    def release(self, job_id: str) -> None:
        with self._lock:
            self._held.pop(job_id, None)


budget = Budget()
