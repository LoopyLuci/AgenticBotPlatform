"""The agents' hosting tools (bot/hosting): sites, going live, DNS, deploys, the web server, checks from outside.

    hosting_status     the sites, the web server, tunnels, connected accounts (names only), certificates
    hosting_network    this machine's public/LAN addresses, NAT situation, and the recommended way to go live
    hosting_site       create / edit / remove a site (asks first)
    hosting_plan       the steps that would make a site reachable in a given mode
    hosting_go_live    run those steps (asks first)
    hosting_publish    build and deploy a site's files to its targets (asks first)
    hosting_check      DNS, http, https and the certificate of a site's names, from outside
    hosting_dns        zones and records of a connected account;  hosting_dns_change  set or delete a record set (asks first)
    hosting_server     status and log of the web server;          hosting_server_control  start / stop it (asks first)

What agents cannot do here, by design: read or add provider credentials, create or destroy paid servers, or agree to
a certificate authority's terms. Those stay with the person (Hosting page, `abp host`).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

MAX_OUT = 14_000


def _out(value: Any) -> str:
    text = json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


async def _call(fn, *args, **kwargs) -> str:
    from bot.hosting.store import HostingError
    try:
        return _out(await asyncio.to_thread(fn, *args, **kwargs))
    except HostingError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - reaches the agent as text
        return f"Error: {type(e).__name__}: {e}"


def _status() -> dict:
    from bot.hosting import accounts, acme, service, tunnels
    return {"sites": [service.public(service.get(s)) for s in service.sites()], "web_server": service.edge_status(),
            "tunnels": tunnels.overview(), "accounts": [{k: a[k] for k in ("id", "provider", "name", "caps", "verified")}
                                                         for a in accounts.listing()],
            "certificates": acme.certificates(), "settings": {k: v for k, v in service.settings().items() if k != "acme_email"}}


def _site(inp: dict) -> Any:
    from bot.hosting import service
    action = inp.get("action", "create")
    fields = dict(inp.get("fields") or {})
    if action == "create":
        return service.public(service.create(fields))
    if action == "edit":
        return service.public(service.edit(inp["site"], fields))
    if action == "remove":
        return {"removed": service.remove(inp["site"])}
    raise ValueError("action is create, edit or remove")


def _dns(inp: dict) -> Any:
    from bot.hosting import dns
    action = inp.get("action", "zones")
    p = dns.provider(inp["account"])
    if action == "zones":
        return p.zones()
    if action == "records":
        return p.records(inp["zone"])
    if action == "set":
        vals = inp.get("values") or []
        return p.set(inp["zone"], inp.get("name", "@"), inp.get("type", "A"), vals if isinstance(vals, list) else [vals],
                     int(inp.get("ttl") or 300), inp.get("proxied"))
    if action == "delete":
        return {"removed": p.delete(inp["zone"], inp["name"], inp["type"])}
    raise ValueError("action is zones, records, set or delete")


def _server(action: str) -> Any:
    from bot.hosting import procs, service
    if action == "start":
        return service.edge_start()
    if action == "stop":
        return {"stopped": service.edge_stop()}
    if action == "log":
        return {"edge": procs.tail("edge", 60), "cloudflared": procs.tail("cloudflared", 30)}
    return service.edge_status()


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, read_only=True, needs_approval=None):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, "read" if read_only else "external", read_only=read_only,
                              concurrency_safe=read_only, origin="registered", needs_approval=needs_approval),
            handler)

    async def status(inp, **_):
        return await _call(_status)

    async def network(inp, **_):
        from bot.hosting import service
        return await _call(service.recommend)

    async def site(inp, **_):
        return await _call(_site, inp)

    async def plan(inp, **_):
        from bot.hosting import service
        return await _call(service.plan, inp["site"], inp.get("mode") or None, inp.get("account") or None)

    async def go_live(inp, **_):
        from bot.hosting import service
        lines: list[str] = []
        res = await _call(service.go_live, inp["site"], inp.get("mode") or None, inp.get("account") or None, lines.append)
        return res + ("\n\nlog:\n" + "\n".join(lines[-40:]) if lines else "")

    async def publish(inp, **_):
        from bot.hosting import service
        lines: list[str] = []
        res = await _call(service.publish, inp["site"], inp.get("account") or None, lines.append)
        return res + ("\n\nlog:\n" + "\n".join(lines[-40:]) if lines else "")

    async def check(inp, **_):
        from bot.hosting import service
        return await _call(service.check, inp["site"])

    async def dns_tool(inp, **_):
        if inp.get("action") not in ("set", "delete"):
            return "Error: hosting_dns_change sets or deletes; reads go through hosting_dns"
        return await _call(_dns, inp)

    async def dns_read(inp, **_):
        if inp.get("action") not in ("zones", "records"):
            return "Error: hosting_dns reads (zones, records); changes go through hosting_dns_change"
        return await _call(_dns, inp)

    async def server(inp, **_):
        return await _call(_server, "log" if inp.get("action") == "log" else "status")

    async def server_control(inp, **_):
        if inp.get("action") not in ("start", "stop"):
            return "Error: action is start or stop"
        return await _call(_server, inp["action"])

    site_fields = {"type": "object", "description":
                   "name; kind: static | proxy | redirect; root (a folder, static); spa (bool); upstream (http://127.0.0.1:PORT, "
                   "proxy); redirect_to; domains (list); https: auto | self-signed | off; force_https; serve: edge | none; "
                   "build: {command, cwd, output}; headers; targets: [{account, ...}]; user + password (basic auth)"}
    modes = ["lan", "cloudflare-tunnel", "tailscale-funnel", "port-forward", "direct", "server", "provider"]
    reg("hosting_status", "ABP Web Hosting on this machine: every site (kind, folder or app, domains, how it is exposed, "
        "deploy targets, recent history), whether the web server runs, tunnels, connected provider accounts (names and "
        "what they can do — never their secrets) and certificates.", {}, [], status)
    reg("hosting_network", "This machine on the internet: public IPv4/IPv6, LAN address, the router (UPnP), whether it is "
        "behind NAT or carrier-grade NAT, and the recommended way to make a site reachable from here.", {}, [], network)
    reg("hosting_site", "Create, edit or remove a hosted site. A site is a folder of files (static), an app on a port "
        "(proxy) or a redirect, with the domain names it answers to. The person is asked first.",
        {"action": {"type": "string", "enum": ["create", "edit", "remove"]}, "site": {"type": "string", "description": "id or name (edit/remove)"},
         "fields": site_fields}, ["action"], site, read_only=False, needs_approval=True)
    reg("hosting_plan", "The steps that would make a site reachable in a mode (default: its current one or the "
        "recommended one): " + ", ".join(modes) + ". server/provider/cloudflare-tunnel need an account id.",
        {"site": {"type": "string"}, "mode": {"type": "string", "enum": modes}, "account": {"type": "string"}}, ["site"], plan)
    reg("hosting_go_live", "Make a site reachable: build it, start the web server, create DNS records, tunnels, router "
        "forwards, certificates or deploys as its plan says, then check it from outside. Returns each step's outcome "
        "and a log. The person is asked first.",
        {"site": {"type": "string"}, "mode": {"type": "string", "enum": modes}, "account": {"type": "string"}}, ["site"],
        go_live, read_only=False, needs_approval=True)
    reg("hosting_publish", "Build a site and deploy its files to its targets (or one target account): an SSH server, "
        "FTP/FTPS hosting, Netlify, Vercel, Cloudflare Pages, GitHub Pages. The person is asked first.",
        {"site": {"type": "string"}, "account": {"type": "string"}}, ["site"], publish, read_only=False, needs_approval=True)
    reg("hosting_check", "Check a site's names from outside: what public DNS answers, whether http and https work, the "
        "certificate, and the problem if one does not.", {"site": {"type": "string"}}, ["site"], check)
    dns_props = {"account": {"type": "string"}, "zone": {"type": "string"}, "name": {"type": "string"}, "type": {"type": "string"},
                 "values": {"type": "array", "items": {"type": "string"}}, "ttl": {"type": "integer"}, "proxied": {"type": "boolean"}}
    reg("hosting_dns", "Read DNS through a connected account: action zones, or records (of a zone).",
        {"action": {"type": "string", "enum": ["zones", "records"]}, **dns_props}, ["action", "account"], dns_read)
    reg("hosting_dns_change", "Change DNS through a connected account: action set (zone, name, type, values, ttl, "
        "proxied — the record set becomes exactly these values) or delete (zone, name, type). The person is asked first.",
        {"action": {"type": "string", "enum": ["set", "delete"]}, **dns_props}, ["action", "account", "zone", "name", "type"],
        dns_tool, read_only=False, needs_approval=True)
    reg("hosting_server", "The web server that serves sites from this machine (ABP's edge or Caddy): action status or log.",
        {"action": {"type": "string", "enum": ["status", "log"]}}, [], server)
    reg("hosting_server_control", "Start or stop the web server that serves sites from this machine. The person is asked first.",
        {"action": {"type": "string", "enum": ["start", "stop"]}}, ["action"], server_control, read_only=False, needs_approval=True)

register_tools()
