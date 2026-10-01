"""Caddy as the serving engine: on this machine instead of ABP's edge (a person's choice on the Hosting page), and on
every server ABP sets up over SSH. Caddy gets and renews certificates by itself.

    caddy.block(site)          the Caddyfile block for one site
    caddy.caddyfile(sites)     the whole file for this machine (<hosting>/Caddyfile)
    caddy.apply(sites)         write it; start Caddy, or reload a running one
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional

from bot.hosting import procs
from bot.hosting.store import HostingError, root

CADDY_RELEASES = "https://github.com/caddyserver/caddy/releases/latest"


def caddy_path() -> Optional[str]:
    local = root() / "bin" / ("caddy.exe" if sys.platform == "win32" else "caddy")
    return str(local) if local.exists() else shutil.which("caddy")


def _q(s: str) -> str:
    return '"' + s.replace("\\", "/").replace('"', '\\"') + '"'


def block(site: dict, remote: bool = False) -> str:
    domains = [d for d in site.get("domains") or [] if d]
    if not domains:
        raise HostingError(f"site {site.get('id')} has no domain names")
    https = site.get("https", "auto")
    addrs = ", ".join((f"http://{d}" if https == "off" else d) for d in domains)
    lines = [f"{addrs} {{", "\tencode zstd gzip"]
    if https == "self-signed" or (https == "auto" and all(d.endswith((".local", ".lan", ".home", ".internal", ".test")) or d == "localhost" for d in domains)):
        lines.append("\ttls internal")
    for k, v in (site.get("headers") or {}).items():
        lines.append(f"\theader {k} {_q(str(v))}")
    kind = site.get("kind")
    if kind == "static":
        lines.append(f"\troot * {_q(site['root'])}")
        if site.get("spa"):
            lines.append("\ttry_files {path} {path}/ {path}.html /index.html")
        else:
            lines.append("\ttry_files {path} {path}/ {path}.html")
        lines.append("\tfile_server")
        lines.append("\t@assets path_regexp \\.[0-9a-f]{8,}\\.(js|css|woff2?|png|jpe?g|svg|webp|avif)$")
        lines.append("\theader @assets Cache-Control \"public, max-age=31536000, immutable\"")
    elif kind in ("proxy", "container", "app"):
        up = site["upstream"]
        if remote and site.get("remote_upstream"):
            up = site["remote_upstream"]
        lines.append(f"\treverse_proxy {up}" + (" {\n\t\theader_up Host {upstream_hostport}\n\t}" if not site.get("preserve_host", True) else ""))
    elif kind == "redirect":
        lines.append(f"\tredir {site['redirect_to'].rstrip('/')}{{uri}} permanent")
    else:
        raise HostingError(f"site {site.get('id')}: unknown kind {kind!r}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def caddyfile(sites: dict, email: str = "") -> str:
    head = ["{", "\tadmin localhost:2019"]
    if email:
        head.append(f"\temail {email}")
    head.append("}")
    body = []
    for sid, s in sorted(sites.items()):
        if s.get("enabled", True) and s.get("serve", "edge") == "edge" and s.get("domains"):
            body.append(f"# {sid}: {s.get('name', '')}\n" + block({**s, "id": sid}))
    return "\n".join(head) + "\n\n" + "\n".join(body)


def validate(text: str) -> None:
    exe = caddy_path()
    if not exe:
        return
    path = root() / "Caddyfile.check"
    path.write_text(text, encoding="utf-8")
    r = subprocess.run([exe, "validate", "--config", str(path), "--adapter", "caddyfile"], capture_output=True, text=True, timeout=60)
    path.unlink(missing_ok=True)
    if r.returncode != 0:
        raise HostingError(f"Caddy rejects the configuration: {(r.stderr or r.stdout)[-600:]}")


def apply(sites: dict, email: str = "") -> dict:
    exe = caddy_path()
    if not exe:
        raise HostingError("Caddy is not installed: install it (Hosting page, or your package manager) or use ABP's edge")
    text = caddyfile(sites, email)
    validate(text)
    path = root() / "Caddyfile"
    path.write_text(text, encoding="utf-8")
    if procs.status("caddy")["running"]:
        r = subprocess.run([exe, "reload", "--config", str(path), "--adapter", "caddyfile"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise HostingError(f"caddy reload failed: {(r.stderr or r.stdout)[-600:]}")
        return {"reloaded": True, "path": str(path)}
    st = procs.start("caddy", [exe, "run", "--config", str(path), "--adapter", "caddyfile"], cwd=str(root()))
    return {"started": True, "path": str(path), "pid": st.get("pid")}
