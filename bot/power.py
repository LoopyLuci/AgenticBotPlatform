"""Power: keeping this machine awake while it is being used, and waking other machines.

**Keeping awake.** One background thread holds the operating system's "don't sleep" request whenever something needs
the machine: Windows SetThreadExecutionState (the request belongs to the thread that made it, so one long-lived thread
holds it), macOS `caffeinate`, Linux `systemd-inhibit`. What counts as needing it is the mode, in config/backends.yaml:

    power:
      keep_awake: while_busy     # off | always | while_busy
      keep_display_on: false
      idle_minutes: 10           # while_busy: stay awake this long after the last activity
      auto_wake: true            # wake a linked server (Wake-on-LAN) before calling it if it does not answer

``while_busy`` keeps the machine awake while an agent turn or a job runs, while a linked machine is controlling it, and
for ``idle_minutes`` after. Anyone can also add a *hold* with a reason and a duration (the page's "Keep awake for...",
the agent's power_keep_awake, or a linked machine that is about to use this one); holds are shown with who asked.

**Waking.** A Wake-on-LAN magic packet wakes a sleeping (or, if its network card allows, powered-off) machine on the
same network. ABP learns each linked server's network cards automatically (``learn_peer``) and keeps them under
``power.wake`` in config, so waking one is a click. The target's own firmware and network card must allow waking;
``info()`` on that machine says whether Windows has wake enabled on each card.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Optional

log = logging.getLogger("abp.power")

MODES = ("off", "always", "while_busy")
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("power") or {})
    except Exception:  # noqa: BLE001
        return {}


def settings() -> dict:
    c = _cfg()
    mode = c.get("keep_awake", "while_busy")
    return {"keep_awake": mode if mode in MODES else "while_busy", "keep_display_on": bool(c.get("keep_display_on", False)),
            "idle_minutes": float(c.get("idle_minutes", 10)), "auto_wake": bool(c.get("auto_wake", True)),
            "wake": dict(c.get("wake") or {})}


class KeepAwake:
    """The one thread that asks the OS not to sleep, and the reasons it currently has to."""

    def __init__(self) -> None:
        self.holds: dict[str, dict] = {}          # key -> {reason, until, by}
        self.last_activity: dict[str, float] = {}  # kind -> when
        self.busy_probe = self._default_busy
        self._lock = threading.Lock()
        self._held = False
        self._display = False
        self._helper: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake_now = threading.Event()

    # ---- reasons ---------------------------------------------------------------------------------------------------------
    def hold(self, key: str, reason: str, minutes: float, by: str = "") -> dict:
        until = time.time() + max(0.5, minutes) * 60 if minutes > 0 else float("inf")
        with self._lock:
            self.holds[key] = {"reason": reason, "until": until, "by": by, "since": time.time()}
        self.start()
        self._wake_now.set()
        return self.state()

    def release(self, key: str = "") -> dict:
        with self._lock:
            if key:
                self.holds.pop(key, None)
            else:
                self.holds.clear()
        self._wake_now.set()
        return self.state()

    def note_activity(self, kind: str) -> None:
        self.last_activity[kind] = time.time()
        if not self._held:
            self._wake_now.set()

    @staticmethod
    def _default_busy() -> list[str]:
        out = []
        try:
            from bot import db
            running = db.get_overview().get("jobs_running", 0)
            if running:
                out.append(f"{running} job(s) running")
        except Exception:  # noqa: BLE001
            pass
        return out

    def reasons(self) -> list[str]:
        s = settings()
        now = time.time()
        with self._lock:
            for k in [k for k, h in self.holds.items() if h["until"] < now]:
                self.holds.pop(k, None)
            out = [f"{h['reason']}" + (f" (asked by {h['by']})" if h["by"] else "") for h in self.holds.values()]
        if s["keep_awake"] == "always":
            out.append("set to always stay awake")
        elif s["keep_awake"] == "while_busy":
            out += self.busy_probe()
            idle = s["idle_minutes"] * 60
            for kind, when in self.last_activity.items():
                if now - when < idle:
                    out.append(f"{kind} {int((now - when) // 60)} min ago")
        return out

    # ---- holding the OS request -------------------------------------------------------------------------------------------
    def _apply(self, want: bool, display: bool) -> None:
        if want == self._held and display == self._display:
            return
        if sys.platform == "win32":
            import ctypes
            flags = ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0)) if want else 0)
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        else:
            if self._helper and self._helper.poll() is None:
                self._helper.terminate()
            self._helper = None
            if want:
                cmd = (["caffeinate", "-i", *(["-d"] if display else []), "-w", str(os.getpid())] if sys.platform == "darwin"
                       else ["systemd-inhibit", "--what=sleep:idle", "--who=AgenticBotPlatform", "--why=in use",
                             "sleep", "infinity"])
                try:
                    self._helper = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL)
                except FileNotFoundError:
                    log.warning("keep-awake: %s is not available", cmd[0])
        if want != self._held:
            log.info("keep-awake: %s", "holding" if want else "released")
        self._held, self._display = want, display

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rs = self.reasons()
                self._apply(bool(rs), bool(rs) and settings()["keep_display_on"])
            except Exception as e:  # noqa: BLE001 - the thread must outlive a bad config read
                log.debug("keep-awake: %s", e)
            self._wake_now.wait(20)
            self._wake_now.clear()
        self._apply(False, False)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="keep-awake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake_now.set()

    def state(self) -> dict:
        rs = self.reasons()
        with self._lock:
            holds = [{"key": k, **{kk: (None if vv == float("inf") else vv) for kk, vv in h.items()}} for k, h in self.holds.items()]
        return {"awake_requested": self._held, "display_on": self._display, "reasons": rs, "holds": holds, **settings()}


keeper = KeepAwake()


# ---- this machine's network cards (for being woken) ------------------------------------------------------------------
def _run(cmd: list[str], timeout: float = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                              creationflags=0x08000000 if os.name == "nt" else 0).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def info() -> dict:
    """This machine's wired and wireless cards: MAC, IPv4 and subnet broadcast, and (on Windows) whether wake is on."""
    import json

    import psutil
    cards = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        mac = next((a.address for a in addrs if a.family == getattr(psutil, "AF_LINK", -1)), "")
        v4 = [a for a in addrs if a.family == socket.AF_INET and not a.address.startswith("127.")]
        if not mac or not v4 or mac.replace("-", ":").lower() in ("00:00:00:00:00:00", ""):
            continue
        a = v4[0]
        try:
            bcast = str(ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False).broadcast_address)
        except ValueError:
            bcast = "255.255.255.255"
        up = stats.get(name).isup if stats.get(name) else False
        mac_u = mac.replace("-", ":").upper()
        # Only a physical card on a real network can receive a magic packet: not Hyper-V/WSL/VirtualBox/VPN
        # adapters, not Wi-Fi Direct, not a link-local (169.254) address.
        virtual = (name.lower().startswith(("vethernet", "local area connection*", "loopback", "bluetooth"))
                   or mac_u.startswith(("00:15:5D", "0A:00:27", "00:50:56", "00:05:69", "02:42"))
                   or "tailscale" in name.lower() or "virtualbox" in name.lower() or "vmware" in name.lower())
        cards.append({"name": name, "mac": mac_u, "ip": a.address, "broadcast": bcast, "up": up,
                      "physical": not virtual and not a.address.startswith("169.254.") and up,
                      "tailscale": a.address.startswith("100.") and "tailscale" in name.lower()})
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-NetAdapterPowerManagement -ErrorAction SilentlyContinue | "
                    "Select-Object Name,WakeOnMagicPacket,DeviceSleepOnDisconnect | ConvertTo-Json -Compress"])
        try:
            data = json.loads(out) if out.strip() else []
            data = data if isinstance(data, list) else [data]
            # ConvertTo-Json writes the enum as its number: 0 Unsupported, 1 Disabled, 2 Enabled.
            names = {"0": "Unsupported", "1": "Disabled", "2": "Enabled"}
            wol = {d["Name"]: names.get(str(d.get("WakeOnMagicPacket")), str(d.get("WakeOnMagicPacket"))) for d in data}
            for c in cards:
                if c["name"] in wol:
                    c["wake_on_magic_packet"] = wol[c["name"]]
        except (ValueError, KeyError):
            pass
    return {"hostname": socket.gethostname(), "cards": [c for c in cards if not c["tailscale"]], "platform": sys.platform}


# ---- waking others ---------------------------------------------------------------------------------------------------
def _mac_bytes(mac: str) -> bytes:
    clean = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(clean) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return bytes.fromhex(clean)


def wake(mac: str, broadcast: str = "255.255.255.255", port: int = 9, repeat: int = 3) -> dict:
    """Send a Wake-on-LAN magic packet (to the subnet's broadcast address and the global one, ports 9 and 7)."""
    packet = b"\xff" * 6 + _mac_bytes(mac) * 16
    targets = sorted({broadcast, "255.255.255.255"})
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(max(1, repeat)):
            for addr in targets:
                for p in {port, 7}:
                    try:
                        s.sendto(packet, (addr, p))
                        sent += 1
                    except OSError as e:
                        log.debug("wake %s via %s:%s: %s", mac, addr, p, e)
            time.sleep(0.1)
    return {"mac": mac.upper(), "sent": sent, "to": targets}


def save_settings(**values: Any) -> dict:
    """Change power settings (keep_awake, keep_display_on, idle_minutes, auto_wake, wake) in config/backends.yaml,
    keeping the file's comments."""
    from bot.config import config
    allowed = {"keep_awake", "keep_display_on", "idle_minutes", "auto_wake", "wake"}
    bad = set(values) - allowed
    if bad:
        raise ValueError(f"unknown power setting(s): {', '.join(sorted(bad))}")
    if "keep_awake" in values and values["keep_awake"] not in MODES:
        raise ValueError(f"keep_awake is one of {', '.join(MODES)}")
    config.set_values({("power", k): v for k, v in values.items()}, actor="power")
    keeper._wake_now.set()
    return settings()


async def learn_peer(peer_ref) -> dict:
    """Ask a linked server for its network cards and remember them, so it can be woken later."""
    from bot import peers
    row = peers.find_peer(peer_ref)
    remote = await peers.proxy(row, "GET", "/api/power/info")
    cards = [c for c in remote.get("cards") or [] if c.get("mac")]
    if not cards:
        raise peers.PeerError(f"{row['name']} reported no network card with an address")
    physical = [c for c in cards if c.get("physical")] or cards
    wired = [c for c in physical if "wi-fi" not in c["name"].lower() and "wireless" not in c["name"].lower()] or physical
    entry = {"mac": wired[0]["mac"], "broadcast": wired[0]["broadcast"], "ip": wired[0]["ip"],
             "hostname": remote.get("hostname", ""), "cards": cards, "learned_at": time.time()}
    save_settings(wake={**settings()["wake"], row["name"]: entry})
    return {"peer": row["name"], **entry}


def wake_target(name: str) -> dict:
    t = settings()["wake"].get(name)
    if not t:
        raise ValueError(f"no wake address for {name!r}: learn it from the linked server first, or give a MAC")
    return {"target": name, **wake(t["mac"], t.get("broadcast") or "255.255.255.255")}


async def wake_and_wait(peer_row, wait_s: float = 120.0) -> bool:
    """Wake a linked server that does not answer, and wait until it does. True if it came up."""
    import asyncio

    import httpx
    t = settings()["wake"].get(peer_row["name"])
    if not t or not peer_row["base_url"]:
        return False
    wake(t["mac"], t.get("broadcast") or "255.255.255.255")
    deadline = time.time() + wait_s
    async with httpx.AsyncClient(timeout=5) as c:
        while time.time() < deadline:
            try:
                r = await c.get(peer_row["base_url"].rstrip("/") + "/healthz")
                if r.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(4)
            wake(t["mac"], t.get("broadcast") or "255.255.255.255", repeat=1)
    return False
