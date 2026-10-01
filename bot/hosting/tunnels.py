"""Reach sites with no open port and no public IP: the connection goes out from this machine to the tunnel provider,
which answers visitors (and handles their HTTPS). Works behind carrier-grade NAT, on hotel Wi-Fi, anywhere.

Cloudflare Tunnel   the domain must be on Cloudflare. ABP creates a named tunnel through the API, routes each
                    host name to the edge (ingress), points the name at the tunnel (a proxied CNAME to
                    <id>.cfargotunnel.com) and runs `cloudflared` with the tunnel's token.
Tailscale Funnel    no domain needed: https://<machine>.<tailnet>.ts.net reaches the edge; Tailscale makes the
                    certificate. Funnel must be allowed in the tailnet's policy (Tailscale says so if it is not).
"""
from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

from bot.hosting import accounts, procs
from bot.hosting.store import HostingError, load, root, update

CLOUDFLARED_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def cloudflared_path() -> Optional[str]:
    local = root() / "bin" / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    if local.exists():
        return str(local)
    return shutil.which("cloudflared")


def cloudflared_asset() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if sys.platform == "win32":
        return f"cloudflared-windows-{arch}.exe"
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def install_cloudflared() -> dict:
    """Download Cloudflare's own cloudflared release into <hosting>/bin (only when a person presses Install)."""
    import hashlib
    import tarfile

    import httpx
    asset = cloudflared_asset()
    dest_dir = root() / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    try:
        with httpx.stream("GET", CLOUDFLARED_RELEASES + asset, follow_redirects=True, timeout=120.0) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(".part")
            h = hashlib.sha256()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
                    h.update(chunk)
    except Exception as e:  # noqa: BLE001
        raise HostingError(f"downloading {asset} failed: {e}") from e
    if asset.endswith(".tgz"):
        with tarfile.open(tmp) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("cloudflared"))
            dest.write_bytes(t.extractfile(member).read())
        tmp.unlink()
    else:
        tmp.replace(dest)
    dest.chmod(0o755)
    return {"path": str(dest), "sha256": h.hexdigest(), "source": CLOUDFLARED_RELEASES + asset}


# ---- Cloudflare Tunnel ---------------------------------------------------------------------------------------- #

def _cf(account_id: str):
    from bot.hosting import dns
    acc = accounts.get(account_id)
    if acc["provider"] != "cloudflare":
        raise HostingError("tunnels need a Cloudflare account")
    cf_account = accounts.setting(acc, "account_id")
    if not cf_account:
        raise HostingError("set the Cloudflare account's Account ID (it is on the right of the Cloudflare dashboard's overview)")
    return dns.provider(acc), cf_account


def cf_tunnels(account_id: str) -> list[dict]:
    p, a = _cf(account_id)
    j = p.req("GET", f"/accounts/{a}/cfd_tunnel?is_deleted=false&per_page=100")
    return [{"id": t["id"], "name": t["name"], "status": t.get("status"), "connections": len(t.get("connections") or []),
             "created": t.get("created_at")} for t in j.get("result") or []]


def cf_ensure(account_id: str, name: str = "abp") -> dict:
    """The named tunnel (created if missing, remotely managed) and its run token, kept sealed."""
    p, a = _cf(account_id)
    t = next((t for t in cf_tunnels(account_id) if t["name"] == name), None)
    if not t:
        import secrets as _s
        r = p.req("POST", f"/accounts/{a}/cfd_tunnel", json={"name": name, "config_src": "cloudflare",
                                                              "tunnel_secret": __import__("base64").b64encode(_s.token_bytes(32)).decode()})
        t = {"id": r["result"]["id"], "name": name}
    token = p.req("GET", f"/accounts/{a}/cfd_tunnel/{t['id']}/token")["result"]
    from bot.vault import seal
    update("tunnels", {}, lambda s: s.__setitem__("cloudflare", {"account": account_id, "tunnel_id": t["id"], "name": name,
                                                                 "token": seal(token)}))
    return {"id": t["id"], "name": name}


def cf_route(account_id: str, hosts: list[str], service: str = "http://localhost:80") -> dict:
    """Make the tunnel's ingress send exactly `hosts` to `service` (the edge), and point each host's DNS at it."""
    p, a = _cf(account_id)
    st = load("tunnels", {}).get("cloudflare") or {}
    if st.get("account") != account_id:
        cf_ensure(account_id)
        st = load("tunnels", {})["cloudflare"]
    tid = st["tunnel_id"]
    ingress = [{"hostname": h, "service": service, "originRequest": {"httpHostHeader": h}} for h in sorted(set(hosts))]
    ingress.append({"service": "http_status:404"})
    p.req("PUT", f"/accounts/{a}/cfd_tunnel/{tid}/configurations", json={"config": {"ingress": ingress}})
    routed = []
    for h in hosts:
        zone = p.zone_for(h)
        p.set(zone, h, "CNAME", [f"{tid}.cfargotunnel.com"], ttl=1, proxied=True)
        routed.append(h)
    update("tunnels", {}, lambda s: s["cloudflare"].__setitem__("hosts", sorted(set(hosts))))
    return {"tunnel": tid, "hosts": routed, "service": service}


def cf_run() -> dict:
    st = load("tunnels", {}).get("cloudflare")
    if not st:
        raise HostingError("no Cloudflare tunnel yet: route a site through one first")
    exe = cloudflared_path()
    if not exe:
        raise HostingError("cloudflared is not installed: press Install on the Hosting page (or `abp host tunnel install`)")
    from bot.vault import unseal
    return procs.start("cloudflared", [exe, "tunnel", "--no-autoupdate", "run", "--token", unseal(st["token"])], secret_args=1)


def cf_stop() -> bool:
    return procs.stop("cloudflared")


def cf_delete(account_id: str) -> bool:
    p, a = _cf(account_id)
    st = load("tunnels", {}).get("cloudflare") or {}
    if not st:
        return False
    cf_stop()
    for h in st.get("hosts", []):
        try:
            p.delete(p.zone_for(h), h, "CNAME")
        except HostingError:
            pass
    p.req("DELETE", f"/accounts/{a}/cfd_tunnel/{st['tunnel_id']}/connections")
    p.req("DELETE", f"/accounts/{a}/cfd_tunnel/{st['tunnel_id']}")
    update("tunnels", {}, lambda s: s.pop("cloudflare", None))
    return True


# ---- Tailscale Funnel ----------------------------------------------------------------------------------------- #

def ts_name() -> Optional[str]:
    from bot import tailscale_mgr as ts
    if not ts.is_installed():
        return None
    try:
        me = ts.status().get("Self") or {}
    except ts.TailscaleError:
        return None
    name = me.get("DNSName", "").rstrip(".")
    return name or None


def ts_funnel(target: str = "http://127.0.0.1:80", on: bool = True) -> dict:
    from bot import tailscale_mgr as ts
    if not ts.is_installed():
        raise HostingError("Tailscale is not installed on this machine")
    if on:
        res = ts.serve_set(target, funnel=True, mode="https", port=443)
    else:
        res = ts.serve_off(funnel=True, mode="https", port=443)
    if not res.get("ok"):
        raise HostingError(f"Tailscale Funnel: {res.get('error') or res}")
    return {"on": on, "url": f"https://{ts_name()}" if ts_name() else None, "target": target, "result": res}


def overview() -> dict:
    st = load("tunnels", {})
    cf = st.get("cloudflare") or {}
    return {"cloudflare": {"configured": bool(cf), "tunnel_id": cf.get("tunnel_id"), "hosts": cf.get("hosts", []),
                           "account": cf.get("account"), "cloudflared": cloudflared_path(), "process": procs.status("cloudflared")},
            "tailscale": {"name": ts_name()}}
