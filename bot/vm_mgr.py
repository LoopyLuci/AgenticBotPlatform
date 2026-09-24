"""Virtual-machine management with three backends behind one API:

- qemu     : full lifecycle of QEMU VMs defined by ABP (data/vms/<name>/vm.json),
             driven through the QMP control socket - start/stop/pause/reset,
             live snapshots, media change, screenshots, key injection, ballooning,
             disk images via qemu-img (create/resize/convert/snapshot/check).
- hyperv   : Windows Hyper-V through the Hyper-V PowerShell module (needs an
             elevated ABP; reports that plainly when it isn't).
- libvirt  : any libvirt host through `virsh` (KVM/QEMU/Xen/LXC).

Safety: argv lists only, no shell; VM names/paths/values validated; QEMU VMs
are built from structured options only (no arbitrary extra command line, which
would let a caller run host commands); QMP sockets bind to 127.0.0.1 only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import psutil

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}$")
_SIZE_RE = re.compile(r"^\d+(\.\d+)?[KMGTkmgt]?$")
BACKENDS = ("qemu", "hyperv", "libvirt")


class VMError(Exception):
    pass


def _name(v: Any) -> str:
    s = str(v).strip()
    if not _NAME_RE.match(s):
        raise VMError(f"invalid VM name {s!r} (letters, digits, . _ -)")
    return s


def _size(v: Any, what: str) -> str:
    s = str(v).strip()
    if not _SIZE_RE.match(s):
        raise VMError(f"bad {what} {s!r} (e.g. 20G, 4096M)")
    return s


def _exists_file(path: str, what: str) -> str:
    if not path or not os.path.isfile(path):
        raise VMError(f"{what} not found: {path}")
    return path


def _exe(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for base in (r"C:\Program Files\qemu", os.path.expanduser(r"~\scoop\apps\qemu\current")):
        cand = os.path.join(base, name + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(cand):
            return cand
    return None


def _run(argv: list[str], timeout: float = 60.0) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, f"{Path(argv[0]).name} timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, str(exc)
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()


def _need(argv: list[str], timeout: float = 60.0) -> str:
    ok, out = _run(argv, timeout)
    if not ok:
        raise VMError(out)
    return out


def backends() -> dict:
    hv = False
    if os.name == "nt" and shutil.which("powershell"):
        hv = _run(["powershell", "-NoProfile", "-Command", "Get-Command Get-VM -ErrorAction SilentlyContinue | Out-Null; $?"],
                  15)[1].strip().endswith("True")
    accels: list[str] = []
    q = _exe("qemu-system-x86_64")
    if q:
        ok, out = _run([q, "-accel", "help"], 10)
        accels = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()] if ok else []
    return {
        "qemu": {"available": bool(q), "binary": q, "accelerators": accels,
                 "img": _exe("qemu-img"), "archs": sorted(_archs())},
        "hyperv": {"available": hv},
        "libvirt": {"available": bool(shutil.which("virsh"))},
    }


def _archs() -> list[str]:
    q = _exe("qemu-system-x86_64")
    if not q:
        return []
    return sorted({p.name.replace(".exe", "").replace("qemu-system-", "").replace("w", "", 0)
                   for p in Path(q).parent.glob("qemu-system-*") if not p.name.endswith("w.exe")})


# ================================================================== QEMU
def _root() -> Path:
    from bot import envfile
    d = Path(envfile.PROJECT_ROOT) / "data" / "vms"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_paths() -> dict:
    disks = _root() / "disks"
    disks.mkdir(parents=True, exist_ok=True)
    return {"disks": str(disks).replace("\\", "/"), "vms": str(_root()).replace("\\", "/")}


def _vm_dir(name: str) -> Path:
    return _root() / _name(name)


def _load(name: str) -> dict:
    f = _vm_dir(name) / "vm.json"
    if not f.is_file():
        raise VMError(f"no such VM: {name}")
    return json.loads(f.read_text(encoding="utf-8"))


def _save(spec: dict) -> None:
    d = _vm_dir(spec["name"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "vm.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_ARCH_MACHINE = {"x86_64": "q35", "i386": "pc", "aarch64": "virt", "arm": "virt", "riscv64": "virt",
                 "ppc64": "pseries", "s390x": "s390-ccw-virtio", "mips64el": "malta"}


def qemu_define(name: str, *, arch: str = "x86_64", cpus: int = 2, memory: str = "2048M",
                disks: Optional[list[dict]] = None, cdrom: Optional[str] = None, boot: str = "disk",
                nics: Optional[list[dict]] = None, display: str = "vnc", accel: str = "auto",
                uefi_firmware: Optional[str] = None, machine: Optional[str] = None, cpu: str = "max",
                usb: bool = True, tpm: bool = False, shared_folders: Optional[list[dict]] = None,
                notes: str = "", update: bool = False) -> dict:
    """Create (or, with update=True, replace) a VM definition. disks: [{path, format, interface}]
    nics: [{model, mode:'user'|'none', hostfwd:['tcp::2222-:22']}]."""
    n = _name(name)
    if (_vm_dir(n) / "vm.json").exists() and not update:
        raise VMError(f"a VM named {n} already exists")
    if arch not in _ARCH_MACHINE:
        raise VMError(f"arch must be one of {sorted(_ARCH_MACHINE)}")
    if not (1 <= int(cpus) <= 256):
        raise VMError("cpus must be 1-256")
    if boot not in ("disk", "cdrom", "network"):
        raise VMError("boot must be disk, cdrom or network")
    if display not in ("vnc", "sdl", "gtk", "none", "spice"):
        raise VMError("display must be vnc, sdl, gtk, spice or none")
    if accel not in ("auto", "kvm", "whpx", "hvf", "hax", "tcg"):
        raise VMError("bad accelerator")
    clean_disks = []
    for d in disks or []:
        p = str(d.get("path", ""))
        if not p or p.startswith("-") or "," in p or "\x00" in p:
            raise VMError(f"bad disk path {p!r}")
        fmt = d.get("format", "qcow2")
        if fmt not in ("qcow2", "raw", "vmdk", "vdi", "vhdx", "qed"):
            raise VMError("bad disk format")
        iface = d.get("interface", "virtio")
        if iface not in ("virtio", "ide", "scsi", "sata", "nvme"):
            raise VMError("bad disk interface")
        clean_disks.append({"path": p, "format": fmt, "interface": iface, "readonly": bool(d.get("readonly", False))})
    if cdrom:
        _exists_file(cdrom, "ISO image")
        if "," in cdrom:
            raise VMError("ISO path may not contain commas")
    clean_nics = []
    for nic in nics if nics is not None else [{"model": "virtio-net-pci", "mode": "user"}]:
        model = nic.get("model", "virtio-net-pci")
        if not re.match(r"^[a-z0-9\-]{2,30}$", model):
            raise VMError("bad NIC model")
        fwd = []
        for h in nic.get("hostfwd", []):
            if not re.match(r"^(tcp|udp):[\d.]*:\d{1,5}-[\d.]*:\d{1,5}$", h):
                raise VMError(f"bad port forward {h!r} (e.g. tcp::2222-:22)")
            fwd.append(h)
        mode = nic.get("mode", "user")
        if mode not in ("user", "none"):
            raise VMError("nic mode must be user or none (bridged/tap networking needs host setup)")
        clean_nics.append({"model": model, "mode": mode, "hostfwd": fwd, "mac": nic.get("mac")})
    shares = []
    for s in shared_folders or []:
        if not os.path.isdir(s.get("path", "")) or "," in s["path"]:
            raise VMError(f"shared folder not found: {s.get('path')}")
        shares.append({"path": s["path"], "tag": _name(s.get("tag", "share")), "readonly": bool(s.get("readonly", False))})
    if uefi_firmware:
        _exists_file(uefi_firmware, "UEFI firmware")
    spec = {"name": n, "backend": "qemu", "arch": arch, "cpus": int(cpus), "memory": _size(memory, "memory"),
            "disks": clean_disks, "cdrom": cdrom, "boot": boot, "nics": clean_nics, "display": display,
            "accel": accel, "uefi_firmware": uefi_firmware, "machine": machine or _ARCH_MACHINE[arch],
            "cpu": cpu if re.match(r"^[A-Za-z0-9_,.\-=+]{1,80}$", cpu) else "max", "usb": bool(usb),
            "tpm": bool(tpm), "shared_folders": shares, "notes": notes[:500],
            "qmp_port": None, "vnc_display": None, "created": time.time()}
    if update:
        old = _load(n)
        spec["created"] = old.get("created", spec["created"])
    _save(spec)
    return spec


def _build_argv(spec: dict, qmp_port: int, vnc_port: Optional[int]) -> list[str]:
    binary = _exe(f"qemu-system-{spec['arch']}")
    if not binary:
        raise VMError(f"qemu-system-{spec['arch']} is not installed")
    argv = [binary, "-name", spec["name"], "-machine", spec["machine"], "-smp", str(spec["cpus"]),
            "-m", spec["memory"], "-cpu", spec["cpu"],
            "-qmp", f"tcp:127.0.0.1:{qmp_port},server=on,wait=off"]
    accel = spec["accel"]
    if accel == "auto":
        accel = {"nt": "whpx", "posix": "kvm"}.get(os.name, "tcg")
        if accel == "kvm" and not os.path.exists("/dev/kvm"):
            accel = "tcg"
    argv += ["-accel", accel] + (["-accel", "tcg"] if accel != "tcg" else [])
    if spec.get("uefi_firmware"):
        argv += ["-bios", spec["uefi_firmware"]]
    for i, d in enumerate(spec["disks"]):
        ro = ",readonly=on" if d.get("readonly") else ""
        argv += ["-drive", f"file={d['path']},format={d['format']},if={d['interface']},id=disk{i}{ro}"]
    if spec.get("cdrom"):
        argv += ["-drive", f"file={spec['cdrom']},media=cdrom,if=ide,id=cd0,readonly=on"]
    argv += ["-boot", {"disk": "c", "cdrom": "d", "network": "n"}[spec["boot"]]]
    for i, nic in enumerate(spec["nics"]):
        mac = f",mac={nic['mac']}" if nic.get("mac") and re.match(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", nic["mac"]) else ""
        if nic["mode"] == "none":
            argv += ["-nic", "none"]
            continue
        fwd = "".join(f",hostfwd={h}" for h in nic["hostfwd"])
        argv += ["-netdev", f"user,id=n{i}{fwd}", "-device", f"{nic['model']},netdev=n{i}{mac}"]
    if spec["display"] == "vnc" and vnc_port:
        argv += ["-vnc", f"127.0.0.1:{vnc_port - 5900}"]
    elif spec["display"] == "none":
        argv += ["-display", "none"]
    elif spec["display"] in ("sdl", "gtk"):
        argv += ["-display", spec["display"]]
    argv += ["-vga", "std"] if spec["display"] != "none" and spec["arch"] in ("x86_64", "i386") else []
    if spec.get("usb"):
        argv += ["-usb", "-device", "usb-tablet"]
    for i, s in enumerate(spec.get("shared_folders", [])):
        argv += ["-virtfs", f"local,path={s['path']},mount_tag={s['tag']},security_model=none,id=fs{i}"
                 + (",readonly=on" if s["readonly"] else "")]
    argv += ["-pidfile", str(_vm_dir(spec["name"]) / "qemu.pid")]
    return argv


def qemu_start(name: str) -> dict:
    spec = _load(name)
    if qemu_status(name).get("running"):
        raise VMError("VM is already running")
    qmp = _free_port()
    vnc = None
    if spec["display"] == "vnc":
        for n in range(1, 100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 5900 + n)) != 0:
                    vnc = 5900 + n
                    break
    argv = _build_argv(spec, qmp, vnc)
    log = (_vm_dir(name) / "qemu.log").open("wb")
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=(_NO_WINDOW | 0x00000200) if os.name == "nt" else 0,
                            start_new_session=(os.name != "nt"))
    time.sleep(1.2)
    if proc.poll() is not None:
        tail = (_vm_dir(name) / "qemu.log").read_text(errors="replace")[-600:]
        raise VMError(f"QEMU exited immediately: {tail}")
    spec.update(qmp_port=qmp, vnc_port=vnc, pid=proc.pid, started=time.time())
    _save(spec)
    return {"ok": True, "pid": proc.pid, "vnc": f"127.0.0.1:{vnc}" if vnc else None}


def _qmp(name: str, command: str, arguments: Optional[dict] = None, timeout: float = 10.0) -> Any:
    spec = _load(name)
    port = spec.get("qmp_port")
    if not port:
        raise VMError("VM is not running")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            f = s.makefile("rw", encoding="utf-8")
            f.readline()  # greeting
            f.write(json.dumps({"execute": "qmp_capabilities"}) + "\n")
            f.flush()
            f.readline()
            f.write(json.dumps({"execute": command, **({"arguments": arguments} if arguments else {})}) + "\n")
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    raise VMError("QEMU closed the control connection")
                msg = json.loads(line)
                if "return" in msg:
                    return msg["return"]
                if "error" in msg:
                    raise VMError(msg["error"].get("desc", "QMP error"))
    except OSError as exc:
        raise VMError(f"VM is not reachable (is it running?): {exc}") from exc


def qemu_status(name: str) -> dict:
    spec = _load(name)
    pid = spec.get("pid")
    alive = bool(pid) and psutil.pid_exists(pid) and "qemu" in (psutil.Process(pid).name().lower())
    if not alive:
        return {"name": name, "running": False, "state": "off"}
    try:
        st = _qmp(name, "query-status", timeout=4)
        return {"name": name, "running": True, "state": st.get("status"), "pid": pid,
                "vnc": f"127.0.0.1:{spec['vnc_port']}" if spec.get("vnc_port") else None}
    except VMError:
        return {"name": name, "running": True, "state": "unknown", "pid": pid}


def qemu_stop(name: str, force: bool = False, wait_s: float = 30.0) -> dict:
    spec = _load(name)
    if not qemu_status(name)["running"]:
        return {"ok": True, "state": "off"}
    if force:
        try:
            _qmp(name, "quit")
        except VMError:
            pass
        try:
            proc = psutil.Process(spec["pid"])
            try:
                proc.wait(5)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        except psutil.Error:
            pass
        return {"ok": True, "state": "off"}
    _qmp(name, "system_powerdown")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not qemu_status(name)["running"]:
            return {"ok": True, "state": "off"}
        time.sleep(1)
    return {"ok": False, "state": "running", "detail": "guest ignored the shutdown request; use force"}


def qemu_control(name: str, action: str) -> dict:
    table = {"pause": "stop", "resume": "cont", "reset": "system_reset"}
    if action not in table:
        raise VMError(f"action must be one of {sorted(table)}")
    _qmp(name, table[action])
    return {"ok": True, "state": qemu_status(name)["state"]}


def qemu_delete(name: str, delete_disks: bool = False) -> dict:
    spec = _load(name)
    if qemu_status(name)["running"]:
        raise VMError("stop the VM before deleting it")
    if delete_disks:
        root = _vm_dir(name).resolve()
        for d in spec["disks"]:
            p = Path(d["path"]).resolve()
            if root in p.parents and p.is_file():   # only disks that live inside the VM's own folder
                p.unlink()
    shutil.rmtree(_vm_dir(name), ignore_errors=True)
    return {"ok": True}


def qemu_list() -> list[dict]:
    out = []
    for f in sorted(_root().glob("*/vm.json")):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
            st = qemu_status(spec["name"])
        except (OSError, ValueError, VMError, KeyError):
            continue
        out.append({**{k: spec.get(k) for k in ("name", "arch", "cpus", "memory", "display", "accel", "notes")},
                    "backend": "qemu", **st})
    return out


def qemu_get(name: str) -> dict:
    return {**_load(name), **qemu_status(name)}


def qemu_screenshot(name: str) -> dict:
    path = _vm_dir(name) / "screen.ppm"
    _qmp(name, "screendump", {"filename": str(path)})
    time.sleep(0.3)
    return {"path": str(path), "bytes": path.stat().st_size if path.exists() else 0}


def qemu_send_keys(name: str, keys: list[str]) -> dict:
    for k in keys:
        if not re.match(r"^[a-z0-9_\-]{1,20}$", k):
            raise VMError(f"bad key name {k!r}")
    _qmp(name, "send-key", {"keys": [{"type": "qcode", "data": k} for k in keys]})
    return {"ok": True}


def qemu_change_media(name: str, iso: Optional[str]) -> dict:
    """Insert (path) or eject (None) the CD-ROM without rebooting."""
    if iso is None:
        _qmp(name, "eject", {"id": "cd0", "force": True})
    else:
        _exists_file(iso, "ISO image")
        _qmp(name, "blockdev-change-medium", {"id": "cd0", "filename": iso, "format": "raw"})
    return {"ok": True}


def qemu_balloon(name: str, memory_mb: int) -> dict:
    _qmp(name, "balloon", {"value": int(memory_mb) * 1024 * 1024})
    return {"ok": True}


def qemu_monitor(name: str, command_line: str) -> dict:
    """A human-monitor command (info network, info block, savevm ...). Runs inside QEMU only."""
    if not re.match(r"^[A-Za-z0-9_ .\-:/]{1,200}$", command_line) or command_line.split()[0] in ("migrate", "drive_add", "netdev_add", "chardev-add"):
        raise VMError("that monitor command isn't allowed")
    return {"output": _qmp(name, "human-monitor-command", {"command-line": command_line}, timeout=60)}


def qemu_snapshot(name: str, action: str, tag: str = "") -> dict:
    """Live (RAM+disk) snapshots on a running VM via savevm/loadvm/delvm; offline
    disk-only snapshots via qemu-img when it's stopped."""
    if action not in ("list", "create", "restore", "delete"):
        raise VMError("action must be list, create, restore or delete")
    if action != "list" and not _NAME_RE.match(tag):
        raise VMError("snapshot tag must be letters, digits, . _ -")
    if qemu_status(name)["running"]:
        cmd = {"list": "info snapshots", "create": f"savevm {tag}", "restore": f"loadvm {tag}", "delete": f"delvm {tag}"}[action]
        return {"output": _qmp(name, "human-monitor-command", {"command-line": cmd}, timeout=300)}
    spec = _load(name)
    if not spec["disks"]:
        raise VMError("VM has no disks")
    flag = {"list": "-l", "create": "-c", "restore": "-a", "delete": "-d"}[action]
    argv = [_img(), "snapshot", flag] + ([tag] if action != "list" else []) + [spec["disks"][0]["path"]]
    return {"output": _need(argv, timeout=300)}


# --------------------------------------------------------------- disk images
def _img() -> str:
    p = _exe("qemu-img")
    if not p:
        raise VMError("qemu-img is not installed")
    return p


def disk_create(path: str, size: str, fmt: str = "qcow2", backing: Optional[str] = None, preallocate: bool = False) -> dict:
    if fmt not in ("qcow2", "raw", "vmdk", "vdi", "vhdx", "qed"):
        raise VMError("bad format")
    if path.startswith("-") or os.path.exists(path):
        raise VMError("path is invalid or already exists")
    argv = [_img(), "create", "-f", fmt]
    if backing:
        argv += ["-b", _exists_file(backing, "backing image"), "-F", "qcow2"]
    if preallocate and fmt == "qcow2":
        argv += ["-o", "preallocation=metadata"]
    return {"ok": True, "output": _need([*argv, path, _size(size, "size")])}


def disk_info(path: str) -> dict:
    return json.loads(_need([_img(), "info", "--output=json", _exists_file(path, "disk image")]))


def disk_resize(path: str, size: str) -> dict:
    return {"ok": True, "output": _need([_img(), "resize", _exists_file(path, "disk image"), _size(size, "size")])}


def disk_convert(src: str, dest: str, fmt: str, compress: bool = False) -> dict:
    if fmt not in ("qcow2", "raw", "vmdk", "vdi", "vhdx", "qed") or dest.startswith("-") or os.path.exists(dest):
        raise VMError("bad destination or format")
    argv = [_img(), "convert", "-O", fmt] + (["-c"] if compress and fmt == "qcow2" else []) + [_exists_file(src, "source image"), dest]
    return {"ok": True, "output": _need(argv, timeout=7200)}


def disk_check(path: str) -> dict:
    ok, out = _run([_img(), "check", _exists_file(path, "disk image")], 600)
    return {"ok": ok, "output": out}


# ================================================================ Hyper-V
def _ps(script: str, timeout: float = 60.0) -> Any:
    ok, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    f"$ErrorActionPreference='Stop'; {script} | ConvertTo-Json -Depth 4 -Compress"], timeout)
    if not ok:
        hint = " (Hyper-V management needs ABP running as Administrator)" if "elevat" in out.lower() or "denied" in out.lower() else ""
        raise VMError(out[:400] + hint)
    return json.loads(out) if out else []


def hv_list() -> list[dict]:
    data = _ps("Get-VM | Select-Object Name,State,CPUUsage,MemoryAssigned,ProcessorCount,Uptime,Generation,Version")
    return [{**d, "backend": "hyperv"} for d in (data if isinstance(data, list) else [data])]


def hv_action(name: str, action: str) -> dict:
    cmds = {"start": "Start-VM", "stop": "Stop-VM", "force-stop": "Stop-VM -TurnOff", "restart": "Restart-VM -Force",
            "pause": "Suspend-VM", "resume": "Resume-VM", "save": "Save-VM", "delete": "Remove-VM -Force",
            "checkpoint": "Checkpoint-VM"}
    if action not in cmds:
        raise VMError(f"action must be one of {sorted(cmds)}")
    _ps(f"{cmds[action]} -Name '{_name(name)}' -Confirm:$false; 'ok'")
    return {"ok": True}


def hv_create(name: str, memory: str = "2GB", cpus: int = 2, disk_gb: int = 40, switch: Optional[str] = None,
              generation: int = 2, iso: Optional[str] = None) -> dict:
    n = _name(name)
    if generation not in (1, 2):
        raise VMError("generation must be 1 or 2")
    if not re.match(r"^\d+(MB|GB)$", memory):
        raise VMError("memory like 2GB or 2048MB")
    sw = f" -SwitchName '{_name(switch)}'" if switch else ""
    vhd = rf"$((Get-VMHost).VirtualHardDiskPath)\{n}.vhdx"
    script = (f"New-VM -Name '{n}' -MemoryStartupBytes {memory} -Generation {generation} "
              f"-NewVHDPath \"{vhd}\" -NewVHDSizeBytes {int(disk_gb)}GB{sw} | Out-Null; "
              f"Set-VMProcessor -VMName '{n}' -Count {int(cpus)}; ")
    if iso:
        script += f"Add-VMDvdDrive -VMName '{n}' -Path '{_exists_file(iso, 'ISO image')}'; "
    _ps(script + "'ok'", 180)
    return {"ok": True}


def hv_checkpoints(name: str) -> Any:
    return _ps(f"Get-VMSnapshot -VMName '{_name(name)}' | Select-Object Name,CreationTime,ParentSnapshotName")


def hv_switches() -> Any:
    return _ps("Get-VMSwitch | Select-Object Name,SwitchType,NetAdapterInterfaceDescription")


# ================================================================ libvirt
def _virsh(args: list[str], timeout: float = 60.0) -> str:
    if not shutil.which("virsh"):
        raise VMError("libvirt (virsh) is not installed on this machine")
    return _need(["virsh", *args], timeout)


def lv_list() -> list[dict]:
    out = _virsh(["list", "--all", "--name"])
    vms = []
    for n in [x for x in out.splitlines() if x.strip()]:
        state = _virsh(["domstate", n]).strip()
        vms.append({"name": n, "state": state, "running": state == "running", "backend": "libvirt"})
    return vms


def lv_action(name: str, action: str) -> dict:
    table = {"start": ["start"], "stop": ["shutdown"], "force-stop": ["destroy"], "restart": ["reboot"],
             "pause": ["suspend"], "resume": ["resume"], "delete": ["undefine", "--remove-all-storage", "--nvram"],
             "autostart": ["autostart"], "no-autostart": ["autostart", "--disable"]}
    if action not in table:
        raise VMError(f"action must be one of {sorted(table)}")
    return {"ok": True, "output": _virsh([*table[action], _name(name)])}


def lv_snapshot(name: str, action: str, tag: str = "") -> dict:
    n = _name(name)
    if action == "list":
        return {"output": _virsh(["snapshot-list", n])}
    t = _name(tag)
    cmd = {"create": ["snapshot-create-as", n, t], "restore": ["snapshot-revert", n, t],
           "delete": ["snapshot-delete", n, t]}.get(action)
    if not cmd:
        raise VMError("action must be list, create, restore or delete")
    return {"output": _virsh(cmd, 300)}


def lv_info(name: str) -> dict:
    return {"info": _virsh(["dominfo", _name(name)]), "xml": _virsh(["dumpxml", _name(name)])}


# ================================================================ unified
def list_all() -> dict:
    """Every VM across every available backend; a backend that errors reports why, not a crash."""
    result: dict[str, Any] = {}
    for key, fn in (("qemu", qemu_list), ("hyperv", hv_list), ("libvirt", lv_list)):
        try:
            result[key] = fn()
        except (VMError, ValueError) as exc:
            result[key] = {"error": str(exc)}
    return result
