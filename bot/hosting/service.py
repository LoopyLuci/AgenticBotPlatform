"""Sites, and taking one live: what every interface (page, CLI, TUI, agents) calls.

A site (stored in `<hosting>/sites.json`, which the edge reads directly):

    {"name": "Blog", "kind": "static" | "proxy" | "redirect",
     "root": "E:/sites/blog/dist",            static: the folder served (or the build's output)
     "spa": false,                            static: unknown paths fall back to /index.html
     "upstream": "http://127.0.0.1:3000",     proxy: the app
     "redirect_to": "https://example.com",    redirect
     "domains": ["blog.example.com"],
     "https": "auto" | "self-signed" | "off", "force_https": true,
     "serve": "edge" | "none",                served by this machine, or only deployed elsewhere
     "build": {"command": "npm run build", "cwd": "E:/sites/blog", "output": "dist"},
     "auth": {"user": "me", "password_hash": "pbkdf2$..."},
     "exposure": {"mode": "cloudflare-tunnel" | "tailscale-funnel" | "port-forward" | "direct" | "lan", "account": id},
     "targets": [{"account": id, ...}],         where `publish` sends the files
     "history": [...]}                          the last deploys and go-live runs

Going live = a plan of steps from the site and its exposure (`plan()`), run in order (`go_live()`), each step's
outcome recorded; check() then asks the world: DNS over HTTPS, the page over http and https, the certificate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from bot.hosting import netinfo, procs
from bot.hosting.store import HostingError, load, root, update

logger = logging.getLogger(__name__)
Log = Callable[[str], None]

KINDS = ("static", "proxy", "redirect")
MODES = ("lan", "cloudflare-tunnel", "tailscale-funnel", "port-forward", "direct", "server", "provider")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_DOMAIN = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$|^localhost$|^[a-z0-9-]+\.localhost$")


def settings() -> dict:
    return {"engine": "edge", "http_port": 80, "https_port": 443, "bind": "0.0.0.0", "acme_email": "", "ca": "letsencrypt",
            "agreed_ca_terms": False, "ddns": True, "autostart_edge": True, **load("settings", {})}


def set_settings(changes: dict) -> dict:
    allowed = {"engine", "http_port", "https_port", "bind", "acme_email", "ca", "agreed_ca_terms", "ddns", "autostart_edge"}
    bad = set(changes) - allowed
    if bad:
        raise HostingError(f"unknown setting(s): {', '.join(sorted(bad))}")
    if "engine" in changes and changes["engine"] not in ("edge", "caddy"):
        raise HostingError("the engine is edge (ABP's own) or caddy")
    for p in ("http_port", "https_port"):
        if p in changes and not (0 <= int(changes[p]) <= 65535):
            raise HostingError(f"{p} must be 0-65535 (0 turns that listener off)")
    update("settings", {}, lambda s: s.update(changes))
    return settings()


# ---- sites ---------------------------------------------------------------------------------------------------- #

def sites() -> dict[str, dict]:
    return load("sites", {})


def get(site_id: str) -> dict:
    s = sites().get(site_id)
    if not s:
        hits = [k for k, v in sites().items() if v.get("name", "").lower() == site_id.lower()]
        if len(hits) != 1:
            raise HostingError(f"no site {site_id!r}")
        site_id, s = hits[0], sites()[hits[0]]
    return {**s, "id": site_id}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "site"
    taken = sites()
    out, n = s, 2
    while out in taken:
        out, n = f"{s}-{n}", n + 1
    return out


def _clean_domains(domains: Any) -> list[str]:
    if isinstance(domains, str):
        domains = [d for d in re.split(r"[\s,]+", domains) if d]
    out = []
    for d in domains or []:
        d = str(d).strip().lower().rstrip(".")
        d = re.sub(r"^https?://", "", d).split("/")[0]
        if not _DOMAIN.match(d):
            raise HostingError(f"{d!r} is not a domain name")
        if d not in out:
            out.append(d)
    return out


def _validate(s: dict) -> None:
    if s.get("kind") not in KINDS:
        raise HostingError(f"a site's kind is one of {', '.join(KINDS)}")
    if s["kind"] == "static":
        if not s.get("root") and not s.get("build"):
            raise HostingError("a static site needs a folder (root) or a build")
        if s.get("root") and not s.get("build") and not Path(s["root"]).is_dir():
            raise HostingError(f"{s['root']} is not a folder")
    if s["kind"] == "proxy" and not re.match(r"^https?://[^\s/]+", s.get("upstream") or ""):
        raise HostingError("a proxy site needs the app's address, like http://127.0.0.1:3000")
    if s["kind"] == "redirect" and not re.match(r"^https?://", s.get("redirect_to") or ""):
        raise HostingError("a redirect needs the address to send visitors to")
    if s.get("https", "auto") not in ("auto", "self-signed", "off"):
        raise HostingError("https is auto, self-signed or off")
    taken = {d: sid for sid, o in sites().items() if sid != s.get("id") for d in o.get("domains") or []}
    for d in s.get("domains") or []:
        if d in taken:
            raise HostingError(f"{d} already belongs to site {taken[d]}")


_FIELDS = ("name", "kind", "root", "spa", "upstream", "redirect_to", "domains", "https", "force_https", "serve", "build",
           "headers", "preserve_host", "exposure", "targets", "enabled", "remote_upstream")


def create(fields: dict) -> dict:
    unknown = set(fields) - set(_FIELDS) - {"password", "user", "id"}
    if unknown:
        raise HostingError(f"unknown field(s): {', '.join(sorted(unknown))}")
    name = (fields.get("name") or "").strip() or "Site"
    sid = fields.get("id") or _slug(name)
    if not _ID.match(sid) or sid in sites():
        raise HostingError(f"the id {sid!r} is taken or not lowercase letters, digits and dashes")
    s = {"name": name, "kind": fields.get("kind", "static"), "domains": _clean_domains(fields.get("domains")),
         "https": fields.get("https", "auto"), "force_https": bool(fields.get("force_https", True)),
         "serve": fields.get("serve", "edge"), "spa": bool(fields.get("spa", False)), "enabled": bool(fields.get("enabled", True)),
         "targets": list(fields.get("targets") or []), "created": int(time.time()), "history": []}
    for k in ("root", "upstream", "redirect_to", "build", "headers", "preserve_host", "exposure", "remote_upstream"):
        if fields.get(k) not in (None, ""):
            s[k] = fields[k]
    if s.get("root"):
        s["root"] = str(Path(s["root"]).expanduser())
    if fields.get("password"):
        from bot.hosting.edge import hash_password
        s["auth"] = {"user": fields.get("user") or "admin", "password_hash": hash_password(fields["password"])}
    s["id"] = sid
    _validate(s)
    s.pop("id")
    update("sites", {}, lambda all_: all_.__setitem__(sid, s))
    _engine_apply()
    return get(sid)


def edit(site_id: str, changes: dict) -> dict:
    cur = get(site_id)
    sid = cur.pop("id")
    unknown = set(changes) - set(_FIELDS) - {"password", "user"}
    if unknown:
        raise HostingError(f"unknown field(s): {', '.join(sorted(unknown))}")
    new = {**cur, **{k: v for k, v in changes.items() if k not in ("password", "user")}}
    if "domains" in changes:
        new["domains"] = _clean_domains(changes["domains"])
    if "password" in changes:
        if changes["password"]:
            from bot.hosting.edge import hash_password
            new["auth"] = {"user": changes.get("user") or (cur.get("auth") or {}).get("user") or "admin",
                           "password_hash": hash_password(changes["password"])}
        else:
            new.pop("auth", None)
    for k in [k for k, v in new.items() if v is None]:
        new.pop(k)
    _validate({**new, "id": sid})
    update("sites", {}, lambda all_: all_.__setitem__(sid, new))
    _engine_apply()
    return get(sid)


def remove(site_id: str) -> bool:
    sid = get(site_id)["id"]
    update("sites", {}, lambda all_: all_.pop(sid, None))
    _engine_apply()
    return True


def public(site: dict) -> dict:
    """A site as the API shows it: no password hash, the last 10 history entries."""
    s = {k: v for k, v in site.items() if k != "auth"}
    if site.get("auth"):
        s["auth"] = {"user": site["auth"].get("user")}
    s["history"] = (site.get("history") or [])[-10:]
    return s


def _history(sid: str, entry: dict) -> None:
    def put(all_):
        if sid in all_:
            h = all_[sid].setdefault("history", [])
            h.append({"at": int(time.time()), **entry})
            del h[:-50]
    update("sites", {}, put)


# ---- serving engine ------------------------------------------------------------------------------------------- #

def _engine_apply() -> None:
    """sites.json is the edge's configuration already (it re-reads it); Caddy needs its file rewritten."""
    st = settings()
    if st["engine"] == "caddy" and procs.status("caddy")["running"]:
        from bot.hosting import caddy
        try:
            caddy.apply(sites(), st["acme_email"])
        except HostingError as e:
            logger.warning("hosting: Caddy did not take the new configuration: %s", e)


def edge_status() -> dict:
    st = settings()
    name = "caddy" if st["engine"] == "caddy" else "edge"
    p = procs.status(name)
    out = {"engine": st["engine"], **p, "http_port": st["http_port"], "https_port": st["https_port"]}
    if p["running"] and name == "edge" and st["http_port"]:
        try:
            import httpx
            out["health"] = httpx.get(f"http://127.0.0.1:{st['http_port']}/.well-known/abp-edge", timeout=3).json()
        except Exception as e:  # noqa: BLE001
            out["health"] = {"error": str(e)}
    return out


def edge_start() -> dict:
    st = settings()
    if st["engine"] == "caddy":
        from bot.hosting import caddy
        return caddy.apply(sites(), st["acme_email"])
    for port in (st["http_port"], st["https_port"]):
        if port and netinfo.port_open("127.0.0.1", port, 1.0) and not procs.status("edge")["running"]:
            raise HostingError(f"port {port} is already used by another program on this machine; stop it or change the "
                               "edge's ports in Hosting settings")
    from bot.envfile import CODE_ROOT
    env = {"PYTHONPATH": str(CODE_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""), "ABP_HOSTING_DIR": str(root())}
    return procs.start("edge", [sys.executable, "-m", "bot.hosting.edge", "--http", str(st["http_port"]),
                                "--https", str(st["https_port"]), "--bind", st["bind"]], env=env, cwd=str(CODE_ROOT))


def edge_stop() -> bool:
    return procs.stop("caddy" if settings()["engine"] == "caddy" else "edge")


def ensure_edge(log: Log) -> None:
    if not edge_status()["running"]:
        edge_start()
        log("started the web server (ABP edge)" if settings()["engine"] == "edge" else "started Caddy")


# ---- build ---------------------------------------------------------------------------------------------------- #

def build(site_id: str, log: Log = lambda m: None) -> Path:
    """Run the site's build (if it has one) and return the folder to serve or publish."""
    s = get(site_id)
    b = s.get("build")
    if not b:
        if s["kind"] != "static" or not s.get("root"):
            raise HostingError("only static sites (a folder, or a build) can be published as files")
        return Path(s["root"])
    cwd = Path(b.get("cwd") or s.get("root") or ".")
    if not cwd.is_dir():
        raise HostingError(f"the build folder {cwd} does not exist")
    cmd = b.get("command") or ""
    if cmd:
        log(f"building: {cmd} (in {cwd})")
        t0 = time.time()
        r = subprocess.run(cmd if os.name == "nt" else shlex.split(cmd), cwd=cwd, shell=os.name == "nt",
                           capture_output=True, text=True, timeout=int(b.get("timeout", 1800)), env={**os.environ, "CI": "1"})
        out = (r.stdout or "") + (r.stderr or "")
        (root() / "logs").mkdir(exist_ok=True)
        (root() / "logs" / f"build-{s['id']}.log").write_text(out, encoding="utf-8")
        if r.returncode != 0:
            raise HostingError(f"the build failed ({r.returncode}):\n{out[-1500:]}")
        log(f"built in {time.time() - t0:.0f}s")
    out_dir = (cwd / b.get("output", "dist")).resolve()
    if not out_dir.is_dir():
        raise HostingError(f"the build did not produce {out_dir}")
    if s.get("serve", "edge") == "edge" and s.get("root") != str(out_dir):
        edit(s["id"], {"root": str(out_dir)})
    return out_dir


# ---- going live ----------------------------------------------------------------------------------------------- #

def plan(site_id: str, mode: Optional[str] = None, account: Optional[str] = None) -> list[dict]:
    """The steps to make the site reachable in `mode` (default: the site's exposure, else a recommendation)."""
    s = get(site_id)
    exp = dict(s.get("exposure") or {})
    mode = mode or exp.get("mode") or recommend()["mode"]
    account = account or exp.get("account")
    if mode not in MODES:
        raise HostingError(f"the mode is one of {', '.join(MODES)}")
    st = settings()
    steps: list[dict] = []
    add = lambda kind, text, **kw: steps.append({"step": kind, "text": text, **kw})   # noqa: E731
    if s.get("build"):
        add("build", f"build the site ({s['build'].get('command') or 'no command'})")
    doms = [d for d in s.get("domains") or [] if not d.startswith("*.")]
    if mode in ("lan", "cloudflare-tunnel", "tailscale-funnel", "port-forward", "direct"):
        add("edge", "make sure the web server is running on this machine")
    if mode == "lan":
        add("info", f"reach it on the local network at http://{netinfo.lan_ip()}:{st['http_port']} with a Host of "
                    f"{doms[0] if doms else '(add a domain)'}, or add the name to devices' hosts files / the router's DNS")
    elif mode == "cloudflare-tunnel":
        if not account:
            raise HostingError("choose the Cloudflare account that holds the domain")
        add("tunnel", f"create or reuse the Cloudflare tunnel and route {', '.join(doms)} to the web server", account=account)
        add("tunnel-run", "run cloudflared (installed into ABP's hosting folder if missing)")
    elif mode == "tailscale-funnel":
        add("funnel", "publish the web server with Tailscale Funnel at https://<this machine>.<tailnet>.ts.net")
    elif mode in ("port-forward", "direct"):
        add("dns", f"point {', '.join(doms)} at this machine's public address (A record), kept current as it changes")
        if mode == "port-forward":
            add("upnp", f"ask the router to forward ports 80 and 443 to {netinfo.lan_ip()}")
        if s.get("https", "auto") == "auto":
            add("certificate", f"get a certificate for {', '.join(doms)} ({st['ca']}, http-01 through the edge)")
    elif mode == "server":
        if not account:
            raise HostingError("choose the server (an SSH account)")
        add("publish", "upload the site's files to the server and serve them with Caddy (it gets the certificate)", account=account)
        add("dns", f"point {', '.join(doms)} at the server's address", account=account)
    elif mode == "provider":
        if not account:
            raise HostingError("choose the provider account (Netlify, Vercel, Cloudflare Pages, GitHub Pages)")
        add("publish", "deploy the files to the provider", account=account)
        if doms:
            add("provider-domain", f"connect {', '.join(doms)} at the provider and point DNS at it", account=account)
    if doms or mode == "tailscale-funnel":
        add("check", "check every name from outside: DNS, http, https, the certificate")
    return steps


def recommend() -> dict:
    """The simplest way to be reachable from here, from what the network looks like and what is connected."""
    from bot.hosting import accounts
    net = netinfo.overview(router=True)
    has_cf = bool(accounts.listing("tunnel"))
    if net["situation"] in ("cgnat", "double-nat", "offline") or has_cf:
        if has_cf:
            return {"mode": "cloudflare-tunnel", "why": "no ports to open, works behind any NAT, Cloudflare handles HTTPS", "network": net}
        from bot.hosting.tunnels import ts_name
        if ts_name():
            return {"mode": "tailscale-funnel", "why": "this network cannot accept connections; Tailscale is already set up here", "network": net}
        return {"mode": "cloudflare-tunnel", "why": "this network cannot accept incoming connections: connect a Cloudflare account "
                "(free) and use a tunnel", "network": net}
    if net["situation"] == "direct":
        return {"mode": "direct", "why": "this machine has a public address", "network": net}
    return {"mode": "port-forward", "why": "a home router in front: forward 80/443 (or connect Cloudflare and use a tunnel instead)",
            "network": net}


def go_live(site_id: str, mode: Optional[str] = None, account: Optional[str] = None, log: Log = lambda m: None,
            agree_ca_terms: Optional[bool] = None) -> dict:
    s = get(site_id)
    sid = s["id"]
    steps = plan(sid, mode, account)
    mode = mode or (s.get("exposure") or {}).get("mode") or recommend()["mode"]
    account = account or (s.get("exposure") or {}).get("account")
    edit(sid, {"exposure": {"mode": mode, **({"account": account} if account else {})}})
    st = settings()
    results = []
    folder: Optional[Path] = None
    publish_result: dict = {}
    for step in steps:
        kind = step["step"]
        t0 = time.time()
        try:
            s = get(sid)
            doms = [d for d in s.get("domains") or [] if not d.startswith("*.")]
            if kind == "build":
                folder = build(sid, log)
                note = str(folder)
            elif kind == "edge":
                ensure_edge(log)
                note = "running"
            elif kind == "info":
                note = step["text"]
            elif kind == "tunnel":
                from bot.hosting import tunnels
                cf_hosts = sorted({d for o in sites().values() if (o.get("exposure") or {}).get("mode") == "cloudflare-tunnel"
                                   for d in o.get("domains") or [] if not d.startswith("*.")})
                r = tunnels.cf_route(account, cf_hosts, service=f"http://localhost:{st['http_port']}")
                note = f"tunnel {r['tunnel'][:8]}… routes {', '.join(r['hosts'])}"
            elif kind == "tunnel-run":
                from bot.hosting import tunnels
                if not tunnels.cloudflared_path():
                    log("installing cloudflared")
                    tunnels.install_cloudflared()
                tunnels.cf_run()
                note = "cloudflared running"
            elif kind == "funnel":
                from bot.hosting import tunnels
                name = tunnels.ts_name()
                if not name:
                    raise HostingError("Tailscale is not signed in on this machine")
                if name not in doms:
                    edit(sid, {"domains": doms + [name]})
                r = tunnels.ts_funnel(f"http://127.0.0.1:{st['http_port']}")
                note = r.get("url") or "on"
            elif kind == "dns":
                from bot.hosting import accounts, dns
                if mode == "server":
                    ip = accounts.setting(accounts.get(account), "host")
                    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip):
                        ip = (netinfo.resolve(ip, "A") or [""])[0]
                else:
                    ip = netinfo.public_ip(4)
                if not ip:
                    raise HostingError("could not tell the public IPv4 address")
                done = []
                for d in doms:
                    p, zone = dns.find_zone(d)
                    p.set(zone, d, "A", [ip], ttl=300, proxied=False if p.label == "Cloudflare" else None)
                    done.append(f"{d} → {ip} ({p.label})")
                note = "; ".join(done)
            elif kind == "upnp":
                from bot.hosting import upnp
                try:
                    for port in (80, 443):
                        upnp.add(port, st["http_port"] if port == 80 else st["https_port"], "TCP", f"web {port}")
                    note = "router forwards 80 and 443 here"
                except HostingError as e:
                    note = (f"the router did not accept it ({e}). Forward TCP 80 → {netinfo.lan_ip()}:{st['http_port']} and "
                            f"TCP 443 → {netinfo.lan_ip()}:{st['https_port']} in the router's own page")
                    results.append({**step, "ok": False, "note": note, "seconds": round(time.time() - t0, 1)})
                    continue
            elif kind == "certificate":
                from bot.hosting import acme
                agreed = st["agreed_ca_terms"] if agree_ca_terms is None else agree_ca_terms
                if agree_ca_terms:
                    set_settings({"agreed_ca_terms": True})
                meta = acme.issue(doms, method="http-01", ca=st["ca"], email=st["acme_email"], agree_tos=bool(agreed), log=log)
                note = f"valid until {meta['not_after'][:10]}"
            elif kind == "publish":
                from bot.hosting import deploy
                folder = folder or build(sid, log)
                target = next((t for t in s.get("targets") or [] if t.get("account") == account), {"account": account})
                publish_result = deploy.publish(s, folder, target, log)
                _save_target(sid, target)
                note = publish_result.get("url") or json.dumps(publish_result)[:200]
            elif kind == "provider-domain":
                note = connect_provider_domain(sid, account, publish_result, log)
            elif kind == "check":
                c = check(sid)
                bad = [r for r in c["names"] if not r.get("ok")]
                note = "all names answer" if not bad else "; ".join(f"{r['name']}: {r.get('problem')}" for r in bad)
                if bad:
                    results.append({**step, "ok": False, "note": note + " (new DNS records can take a few minutes; check again)",
                                    "seconds": round(time.time() - t0, 1)})
                    continue
            else:
                note = "skipped"
            results.append({**step, "ok": True, "note": note, "seconds": round(time.time() - t0, 1)})
            log(f"✓ {step['text']}: {note}")
        except HostingError as e:
            results.append({**step, "ok": False, "note": str(e), "seconds": round(time.time() - t0, 1)})
            log(f"✗ {step['text']}: {e}")
            if kind not in ("check", "upnp", "info"):
                break
    ok = all(r["ok"] for r in results) and len(results) == len(steps)
    _history(sid, {"action": "go-live", "mode": mode, "ok": ok, "steps": [{k: r[k] for k in ("step", "ok", "note")} for r in results]})
    return {"site": sid, "mode": mode, "ok": ok, "steps": results}


def _save_target(sid: str, target: dict) -> None:
    def put(all_):
        ts = all_[sid].setdefault("targets", [])
        for i, t in enumerate(ts):
            if t.get("account") == target["account"]:
                ts[i] = target
                return
        ts.append(target)
    update("sites", {}, put)


def connect_provider_domain(sid: str, account: str, published: dict, log: Log) -> str:
    """Tell the provider the site's domains, and point them at it with DNS where ABP manages the zone."""
    from bot.hosting import accounts, dns
    from bot.hosting.deploy import _http
    s = get(sid)
    acc = accounts.get(account)
    doms = [d for d in s.get("domains") or [] if not d.startswith("*.")]
    p = acc["provider"]
    tok = accounts.secret(acc, "token") or accounts.secret(acc, "api_token")
    if p == "netlify":
        _http("PATCH", f"https://api.netlify.com/api/v1/sites/{published['site_id']}", tok, "Netlify",
              json={"custom_domain": doms[0], "domain_aliases": doms[1:]})
        host = (published.get("url") or "").replace("https://", "").split("/")[0]
        target = host if host.endswith("netlify.app") else f"{published.get('site_id')}.netlify.app"
    elif p == "vercel":
        team = accounts.setting(acc, "team_id")
        q = f"?teamId={team}" if team else ""
        for d in doms:
            try:
                _http("POST", f"https://api.vercel.com/v10/projects/{published['project']}/domains{q}", tok, "Vercel", json={"name": d})
            except HostingError as e:
                if "already" not in str(e).lower():
                    raise
        target = "cname.vercel-dns.com"
    elif p == "cloudflare":
        prov = dns.provider(acc)
        a = accounts.setting(acc, "account_id")
        for d in doms:
            try:
                prov.req("POST", f"/accounts/{a}/pages/projects/{published['project']}/domains", json={"name": d})
            except HostingError as e:
                if "already" not in str(e).lower():
                    raise
        target = f"{published['project']}.pages.dev"
    elif p == "github":
        owner = (published.get("repo") or "/").split("/")[0]
        target = f"{owner.lower()}.github.io"
    else:
        raise HostingError(f"{p} has no custom domains here")
    notes = []
    for d in doms:
        try:
            prov, zone = dns.find_zone(d)
        except HostingError:
            notes.append(f"{d}: add a CNAME to {target} at your DNS provider")
            continue
        if d == zone and prov.label != "Cloudflare":
            notes.append(f"{d} is a zone apex: CNAME is not allowed there; use the provider's apex IPs (see its docs) or a www name")
            continue
        prov.set(zone, d, "CNAME", [target], ttl=300, proxied=False if prov.label == "Cloudflare" else None)
        notes.append(f"{d} → {target}")
    return "; ".join(notes)


def publish(site_id: str, account: Optional[str] = None, log: Log = lambda m: None) -> list[dict]:
    """Deploy to one target or every target of the site."""
    from bot.hosting import deploy
    s = get(site_id)
    targets = [t for t in s.get("targets") or [] if not account or t.get("account") == account] or ([{"account": account}] if account else [])
    if not targets:
        raise HostingError("the site has no deploy targets: add one (an SSH server, FTP, Netlify, Vercel, Cloudflare Pages, GitHub Pages)")
    folder = build(s["id"], log)
    out = []
    for t in targets:
        try:
            r = deploy.publish(s, folder, t, log)
            _save_target(s["id"], t)
            out.append({"account": t["account"], "ok": True, **r})
        except HostingError as e:
            out.append({"account": t["account"], "ok": False, "error": str(e)})
        _history(s["id"], {"action": "publish", **out[-1]})
    return out


def check(site_id: str) -> dict:
    s = get(site_id)
    names = []
    from concurrent.futures import ThreadPoolExecutor

    def one(d: str) -> dict:
        r: dict = {"name": d}
        try:
            r["a"] = netinfo.resolve(d, "A")
            r["aaaa"] = netinfo.resolve(d, "AAAA")
            r["cname"] = netinfo.resolve(d, "CNAME")
        except HostingError as e:
            r["problem"] = str(e)
            r["ok"] = False
            return r
        if not (r["a"] or r["aaaa"]):
            r.update(ok=False, problem="the name does not resolve yet")
            return r
        r["http"] = netinfo.http_probe(f"http://{d}/")
        r["https"] = netinfo.http_probe(f"https://{d}/")
        r["ok"] = bool(r["https"].get("ok") or (s.get("https") == "off" and r["http"].get("ok")))
        if not r["ok"]:
            r["problem"] = r["https"].get("error") or f"https answers {r['https'].get('status')}"
        return r
    doms = [d for d in s.get("domains") or [] if not d.startswith("*.") and not d.endswith(".localhost") and d != "localhost"]
    with ThreadPoolExecutor(4) as ex:
        names = list(ex.map(one, doms))
    return {"site": s["id"], "names": names, "ok": all(n.get("ok") for n in names)}


# ---- background: dynamic DNS, renewals, the edge kept up ------------------------------------------------------ #

def ddns_tick(log: Log = logger.info) -> list[str]:
    """For sites served from here by address (direct / port-forward), keep their A records on the current public IP."""
    changed = []
    live = [s for s in sites().values() if (s.get("exposure") or {}).get("mode") in ("direct", "port-forward") and s.get("enabled", True)]
    if not live:
        return changed
    ip = netinfo.public_ip(4, max_age_s=60)
    if not ip:
        return changed
    from bot.hosting import dns
    for s in live:
        for d in s.get("domains") or []:
            if d.startswith("*."):
                continue
            try:
                if netinfo.resolve(d, "A") == [ip]:
                    continue
                p, zone = dns.find_zone(d)
                cur = next((r for r in p.records(zone) if r["name"] == d and r["type"] == "A"), None)
                if cur and cur["values"] == [ip]:
                    continue
                p.set(zone, d, "A", [ip], ttl=300)
                changed.append(f"{d} → {ip}")
                log(f"hosting: dynamic DNS: {d} now points at {ip}")
            except HostingError as e:
                logger.warning("hosting: dynamic DNS for %s: %s", d, e)
    return changed


_last = {"ddns": 0.0, "renew": 0.0, "edge": 0.0}


def tick() -> None:
    now = time.time()
    st = settings()
    if st["autostart_edge"] and now - _last["edge"] > 60:
        _last["edge"] = now
        serving = any(s.get("serve", "edge") == "edge" and s.get("enabled", True) and s.get("exposure") for s in sites().values())
        if serving and not edge_status()["running"]:
            try:
                edge_start()
                logger.info("hosting: the web server was not running; started it")
            except HostingError as e:
                logger.warning("hosting: could not start the web server: %s", e)
        if load("tunnels", {}).get("cloudflare") and not procs.status("cloudflared")["running"]:
            try:
                from bot.hosting import tunnels
                if tunnels.cloudflared_path():
                    tunnels.cf_run()
            except HostingError as e:
                logger.warning("hosting: could not restart cloudflared: %s", e)
    if st["ddns"] and now - _last["ddns"] > 300:
        _last["ddns"] = now
        ddns_tick()
    if now - _last["renew"] > 12 * 3600:
        _last["renew"] = now
        from bot.hosting import acme
        for r in acme.renew_due(30):
            logger.info("hosting: certificate renewal %s", r)


async def run_forever(stop_event: asyncio.Event) -> None:
    try:                             # let the server come up first
        await asyncio.wait_for(stop_event.wait(), timeout=20)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001 - a bad tick must never kill the loop
            logger.exception("hosting tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
