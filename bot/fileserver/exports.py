"""SMB and NFS exports of shares, through the operating system's own servers.

Exporting a folder as an SMB or NFS share is an administrator's change to the system, so ABP writes the exact
commands / configuration and the person runs them (an elevated PowerShell on Windows, sudo on Linux); ABP never
elevates itself. A user share spread over several array disks has no single folder to export: export its disks'
share folders separately, use the file server's WebDAV (one tree), or set the share to cache "only" / a folder share.
"""
from __future__ import annotations

import shlex
import sys

from bot.fileserver import shares
from bot.fileserver.store import FsError


def folders(name: str) -> list[str]:
    s = shares.get(name)
    return [str(b) for _n, b in shares.branches(s)]


def smb(name: str, platform: str = "") -> dict:
    s = shares.get(name)
    platform = platform or ("windows" if sys.platform == "win32" else "linux")
    paths = folders(name)
    if not paths:
        raise FsError("the share has no folders yet")
    readers = [u for u, m in (s.get("users") or {}).items() if m in ("r", "rw")]
    writers = [u for u, m in (s.get("users") or {}).items() if m == "rw"]
    notes = []
    if len(paths) > 1:
        notes.append("This user share lives on several disks; each folder is exported on its own (or use WebDAV for one tree).")
    if platform == "windows":
        lines = []
        for i, p in enumerate(paths):
            n = s["name"] if i == 0 else f"{s['name']}-{i + 1}"
            if s["access"] == "public":
                acl = "-FullAccess 'Everyone'"
            elif s["access"] == "secure":
                acl = "-ReadAccess 'Everyone'" + (f" -ChangeAccess {','.join(repr(u) for u in writers)}" if writers else "")
            else:
                acl = (f"-ChangeAccess {','.join(repr(u) for u in writers)}" if writers else "") + \
                      (f" -ReadAccess {','.join(repr(u) for u in readers if u not in writers)}" if [u for u in readers if u not in writers] else "")
            lines.append(f"New-SmbShare -Name '{n}' -Path '{p}' {acl} -FolderEnumerationMode AccessBased".strip())
        notes.append("Run in PowerShell as administrator. The users must be Windows accounts on this machine "
                     "(file-server users are separate). Remove later with Remove-SmbShare -Name <name>.")
        return {"platform": "windows", "commands": lines, "notes": notes}
    blocks = []
    for i, p in enumerate(paths):
        n = s["name"] if i == 0 else f"{s['name']}-{i + 1}"
        b = [f"[{n}]", f"   path = {p}", f"   comment = {s.get('comment') or 'ABP share'}"]
        if s["access"] == "public":
            b += ["   guest ok = yes", "   read only = no"]
        elif s["access"] == "secure":
            b += ["   guest ok = yes", "   read only = yes"] + ([f"   write list = {' '.join(writers)}"] if writers else [])
        else:
            b += ["   guest ok = no", f"   valid users = {' '.join(readers) or 'nobody'}", "   read only = yes"] + \
                 ([f"   write list = {' '.join(writers)}"] if writers else [])
        blocks.append("\n".join(b))
    conf = "\n\n".join(blocks) + "\n"
    cmds = [f"printf %s {shlex.quote(conf)} | sudo tee /etc/samba/smb.conf.d/abp-{s['name']}.conf",
            "grep -q 'include = /etc/samba/smb.conf.d' /etc/samba/smb.conf || echo 'include = /etc/samba/smb.conf.d/abp-*.conf' | sudo tee -a /etc/samba/smb.conf",
            "sudo testparm -s >/dev/null && sudo systemctl reload smbd"]
    notes.append("Users need Samba passwords: sudo smbpasswd -a <user>.")
    return {"platform": "linux", "config": conf, "commands": cmds, "notes": notes}


def nfs(name: str, clients: str = "192.168.0.0/16") -> dict:
    s = shares.get(name)
    if sys.platform == "win32":
        return {"platform": "windows", "commands": [], "notes": [
            "NFS serving on Windows needs Windows Server's Server for NFS role; on Windows 10/11 use SMB or WebDAV instead."]}
    ro = "ro" if s["access"] == "secure" else "rw"
    lines = [f"{p} {clients}({ro},sync,no_subtree_check,root_squash)" for p in folders(name)]
    return {"platform": "linux", "exports": lines,
            "commands": [f"printf %s {shlex.quote(chr(10).join(lines) + chr(10))} | sudo tee /etc/exports.d/abp-{s['name']}.exports",
                         "sudo exportfs -ra"], "notes": ["Limit `clients` to your own network."]}
