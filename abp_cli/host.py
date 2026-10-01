"""`abp host ...`: ABP Web Hosting from the command line (the same /api/hosting the Hosting page uses).

  abp host status | network | providers | settings [key=value ...]
  abp host account list | add <provider> [--name N] | verify <id> | remove <id>      (secrets are prompted, never echoed)
  abp host site list | show <site> | add --name N --kind static|proxy|redirect ... | edit <site> key=value ... | remove <site>
  abp host plan <site> [--mode M --account A]      the steps to go live
  abp host live <site> [--mode M --account A] [--agree-ca-terms]                     run them, the log as it happens
  abp host publish <site> [--account A]            build and deploy to its targets
  abp host check <site>                            DNS, http, https, certificate from outside
  abp host server status|start|stop|log [--name edge|caddy|cloudflared]
  abp host dns zones <acc> | records <acc> <zone> | set <acc> <zone> <name> <type> <value>... [--ttl N] [--proxied]
              | delete <acc> <zone> <name> <type>
  abp host tunnel status|install|run|stop
  abp host router list | add <port> [--to PORT] [--udp] | remove <port> [--udp]
  abp host cert list | issue <name>... [--method http-01|dns-01] [--agree-ca-terms]
  abp host vps options <acc> | list <acc> | create <acc> --name N --region R --size S | destroy <acc> <id> | setup <ssh-acc>
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

API = "/api/hosting"


def add_parser(sub) -> None:
    h = sub.add_parser("host", help="ABP Web Hosting: sites, domains, DNS, tunnels, certificates, deploys, servers")
    hs = h.add_subparsers(dest="host_cmd", required=True)
    hs.add_parser("status")
    hs.add_parser("network")
    hs.add_parser("providers")
    p = hs.add_parser("settings", help="show, or change with key=value (engine, http_port, https_port, acme_email, ca, ddns)")
    p.add_argument("pairs", nargs="*")

    p = hs.add_parser("account")
    p.add_argument("action", choices=["list", "add", "verify", "remove"])
    p.add_argument("target", nargs="?", default="", help="provider (add) or account id/name")
    p.add_argument("--name", default="")

    p = hs.add_parser("site")
    p.add_argument("action", choices=["list", "show", "add", "edit", "remove"])
    p.add_argument("site", nargs="?", default="")
    p.add_argument("pairs", nargs="*", help="edit: key=value")
    p.add_argument("--name", default=""); p.add_argument("--kind", default="static", choices=["static", "proxy", "redirect"])
    p.add_argument("--root", default=""); p.add_argument("--upstream", default=""); p.add_argument("--redirect", default="")
    p.add_argument("--domains", default="", help="comma or space separated")
    p.add_argument("--spa", action="store_true"); p.add_argument("--https", default="auto", choices=["auto", "self-signed", "off"])
    p.add_argument("--build-cmd", default=""); p.add_argument("--build-cwd", default=""); p.add_argument("--build-out", default="dist")
    p.add_argument("--user", default=""); p.add_argument("--password", action="store_true", help="prompt for a basic-auth password")
    p.add_argument("--target", action="append", default=[], help="a deploy target account (repeatable)")

    for name in ("plan", "live", "publish", "check"):
        p = hs.add_parser(name)
        p.add_argument("site")
        if name in ("plan", "live"):
            p.add_argument("--mode", default="", help="lan | cloudflare-tunnel | tailscale-funnel | port-forward | direct | server | provider")
        if name in ("plan", "live", "publish"):
            p.add_argument("--account", default="")
        if name == "live":
            p.add_argument("--agree-ca-terms", action="store_true", dest="agree", help="agree to the certificate authority's terms")

    p = hs.add_parser("server")
    p.add_argument("action", choices=["status", "start", "stop", "log"])
    p.add_argument("--name", default="edge")

    p = hs.add_parser("dns")
    p.add_argument("action", choices=["zones", "records", "set", "delete"])
    p.add_argument("account"); p.add_argument("zone", nargs="?", default=""); p.add_argument("name", nargs="?", default="")
    p.add_argument("type", nargs="?", default="A"); p.add_argument("values", nargs="*")
    p.add_argument("--ttl", type=int, default=300); p.add_argument("--proxied", action="store_true")

    p = hs.add_parser("tunnel")
    p.add_argument("action", choices=["status", "install", "run", "stop"])

    p = hs.add_parser("router", help="port forwards on the router (UPnP)")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("port", nargs="?", type=int, default=0); p.add_argument("--to", type=int, default=0)
    p.add_argument("--udp", action="store_true")

    p = hs.add_parser("cert")
    p.add_argument("action", choices=["list", "issue"])
    p.add_argument("names", nargs="*"); p.add_argument("--method", default="http-01", choices=["http-01", "dns-01"])
    p.add_argument("--agree-ca-terms", action="store_true", dest="agree")

    p = hs.add_parser("vps", help="servers at Hetzner, DigitalOcean, Vultr, Linode")
    p.add_argument("action", choices=["options", "list", "create", "destroy", "setup"])
    p.add_argument("account"); p.add_argument("server", nargs="?", default="")
    p.add_argument("--name", default=""); p.add_argument("--region", default=""); p.add_argument("--size", default="")


def _out(args, data: Any) -> None:
    print(json.dumps(data, indent=1, default=str))


def _kv(pairs: list[str]) -> dict:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        k, _, v = pair.partition("=")
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v.isdigit():
            out[k] = int(v)
        elif v.startswith(("[", "{")):
            out[k] = json.loads(v)
        else:
            out[k] = v
    return out


async def _follow(args, client, started: dict) -> int:
    """Print a run's log as it grows, then its result."""
    if "run" not in started:
        _out(args, started)
        return 0
    seen = 0
    while True:
        run = await client._request("GET", f"{API}/runs/{started['run']}", params={"since": seen})
        for line in run["log"]:
            print(line, flush=True)
        seen = run["log_total"]
        if run["done"]:
            if run.get("error"):
                print(f"error: {run['error']}", file=sys.stderr)
                return 1
            if args.json or not isinstance(run.get("result"), dict) or "steps" not in run["result"]:
                _out(args, run.get("result"))
            else:
                for s in run["result"]["steps"]:
                    print(f"{'✓' if s['ok'] else '✗'} {s['text']}\n    {s['note']}")
            return 0 if not isinstance(run.get("result"), dict) or run["result"].get("ok", True) else 1
        await asyncio.sleep(1.5)


async def run(args, client) -> int:
    c = args.host_cmd
    r = client._request
    if c == "status":
        o = await r("GET", API)
        if args.json:
            _out(args, o)
            return 0
        e = o["edge"]
        print(f"web server: {e['engine']} {'running' if e.get('running') else 'stopped'} (http {e['http_port']}, https {e['https_port']})")
        for s in o["sites"]:
            exp = (s.get("exposure") or {}).get("mode", "-")
            print(f"  {s['id']:<20} {s['kind']:<8} {exp:<18} {', '.join(s.get('domains') or []) or '(no domains)'}")
        print(f"accounts: {', '.join(a['name'] + ' (' + a['provider'] + ')' for a in o['accounts']) or 'none'}")
        cf = o["tunnels"]["cloudflare"]
        if cf["configured"]:
            print(f"cloudflare tunnel: {'running' if cf['process']['running'] else 'stopped'}, hosts {', '.join(cf['hosts'])}")
        for cert in o["certs"]:
            print(f"certificate {cert.get('folder')}: {cert.get('days_left')} days left")
        return 0
    if c == "network":
        res = await r("GET", f"{API}/network", timeout=60.0)
        if args.json:
            _out(args, res)
            return 0
        n = res["network"]
        print(f"public IPv4 {n['public_ipv4'] or '-'}, IPv6 {n['public_ipv6'] or '-'}, LAN {n['lan_ip'] or '-'}, "
              f"router UPnP {'yes' if n['router_upnp'] else 'no'}")
        print(f"{n['situation']}: {n['advice']}")
        print(f"recommended: {res['mode']} — {res['why']}")
        return 0
    if c == "providers":
        res = await r("GET", f"{API}/providers")
        for k, v in res.items():
            print(f"{k:<13} {v['label']:<40} {', '.join(v['caps'])}")
        return 0
    if c == "settings":
        _out(args, await (r("PUT", f"{API}/settings", json=_kv(args.pairs)) if args.pairs else r("GET", f"{API}/settings")))
        return 0
    if c == "account":
        return await _account(args, client)
    if c == "site":
        return await _site(args, client)
    if c == "plan":
        res = await r("GET", f"{API}/sites/{args.site}/plan", params={"mode": args.mode, "account": args.account})
        if args.json:
            _out(args, res)
        else:
            for i, s in enumerate(res, 1):
                print(f"{i}. {s['text']}")
        return 0
    if c == "live":
        return await _follow(args, client, await r("POST", f"{API}/sites/{args.site}/go-live", json={
            "mode": args.mode, "account": args.account, **({"agree_ca_terms": True} if args.agree else {})}))
    if c == "publish":
        return await _follow(args, client, await r("POST", f"{API}/sites/{args.site}/publish", json={"account": args.account}))
    if c == "check":
        res = await r("POST", f"{API}/sites/{args.site}/check", timeout=120.0)
        if args.json:
            _out(args, res)
        else:
            for n in res["names"]:
                print(f"{'✓' if n.get('ok') else '✗'} {n['name']}: A {', '.join(n.get('a') or []) or '-'}"
                      + (f" — {n['problem']}" if n.get("problem") else f" — https {n['https'].get('status')} in {n['https'].get('ms')} ms"))
        return 0 if res["ok"] else 1
    if c == "server":
        if args.action == "log":
            print((await r("GET", f"{API}/edge/log", params={"name": args.name}))["text"])
        else:
            _out(args, await r("GET" if args.action == "status" else "POST", f"{API}/edge" + ("" if args.action == "status" else f"/{args.action}")))
        return 0
    if c == "dns":
        a = args.account
        if args.action == "zones":
            _out(args, await r("GET", f"{API}/dns/{a}/zones", timeout=60.0))
        elif args.action == "records":
            recs = await r("GET", f"{API}/dns/{a}/records", params={"zone": args.zone}, timeout=60.0)
            if args.json:
                _out(args, recs)
            else:
                for s in recs:
                    print(f"{s['name']:<40} {s['type']:<6} {s.get('ttl') or '':<6} {', '.join(s['values'])}"
                          + (" (proxied)" if s.get("proxied") else ""))
        elif args.action == "set":
            _out(args, await r("PUT", f"{API}/dns/{a}/records", json={"zone": args.zone, "name": args.name, "type": args.type,
                                                                     "values": args.values, "ttl": args.ttl,
                                                                     **({"proxied": True} if args.proxied else {})}, timeout=60.0))
        else:
            _out(args, await r("DELETE", f"{API}/dns/{a}/records", params={"zone": args.zone, "name": args.name, "type": args.type}))
        return 0
    if c == "tunnel":
        if args.action == "status":
            _out(args, await r("GET", f"{API}/tunnels"))
            return 0
        if args.action == "install":
            if input("Download Cloudflare's cloudflared from github.com/cloudflare/cloudflared into ABP's hosting folder? [y/N] ").lower() != "y":
                return 2
            return await _follow(args, client, await r("POST", f"{API}/tunnels/cloudflared/install"))
        _out(args, await r("POST", f"{API}/tunnels/cloudflare/{args.action}"))
        return 0
    if c == "router":
        proto = "UDP" if args.udp else "TCP"
        if args.action == "list":
            _out(args, await r("GET", f"{API}/upnp", timeout=30.0))
        elif args.action == "add":
            _out(args, await r("POST", f"{API}/upnp", json={"external_port": args.port, "internal_port": args.to or args.port, "protocol": proto}))
        else:
            _out(args, await r("DELETE", f"{API}/upnp", params={"port": args.port, "protocol": proto}))
        return 0
    if c == "cert":
        if args.action == "list":
            _out(args, await r("GET", f"{API}/certs"))
            return 0
        body = {"names": args.names, "method": args.method}
        if args.agree:
            body["agree_ca_terms"] = True
        return await _follow(args, client, await r("POST", f"{API}/certs", json=body))
    if c == "vps":
        return await _vps(args, client)
    print(f"unknown host subcommand {c!r}", file=sys.stderr)
    return 2


async def _account(args, client) -> int:
    r = client._request
    if args.action == "list":
        accs = await r("GET", f"{API}/accounts")
        if args.json:
            _out(args, accs)
        for a in accs if not args.json else []:
            print(f"{a['id']:<22} {a['name']:<28} {a['label']:<26} {'verified' if a.get('verified') else 'not verified'}"
                  + (f" — {a['verify_note']}" if a.get("verify_note") else ""))
        return 0
    if args.action == "add":
        provs = await r("GET", f"{API}/providers")
        spec = provs.get(args.target)
        if not spec:
            print(f"unknown provider {args.target!r}; one of: {', '.join(provs)}", file=sys.stderr)
            return 2
        if spec.get("help"):
            print(f"{spec['label']}: {spec['help']}")
        values = {}
        for f in spec["fields"]:
            prompt = f"{f['label']}{'' if f['required'] else ' (optional)'}: "
            v = getpass.getpass(prompt) if f["secret"] else input(prompt)
            if v:
                values[f["key"]] = v
        acc = await r("POST", f"{API}/accounts", json={"provider": args.target, "name": args.name, "values": values}, timeout=60.0)
        v = acc.get("verify") or {}
        print(f"added {acc['id']} ({acc['name']}): {'works — ' if v.get('ok') else 'NOT working — '}{v.get('note', '')}")
        return 0 if v.get("ok", True) else 1
    if args.action == "verify":
        v = await r("POST", f"{API}/accounts/{args.target}/verify", timeout=60.0)
        print(f"{'works' if v['ok'] else 'NOT working'}: {v['note']}")
        return 0 if v["ok"] else 1
    _out(args, await r("DELETE", f"{API}/accounts/{args.target}"))
    return 0


async def _site(args, client) -> int:
    r = client._request
    if args.action == "list":
        sites = await r("GET", f"{API}/sites")
        if args.json:
            _out(args, sites)
        for s in sites if not args.json else []:
            print(f"{s['id']:<20} {s['kind']:<8} {', '.join(s.get('domains') or []) or '(no domains)'}")
        return 0
    if args.action == "show":
        _out(args, await r("GET", f"{API}/sites/{args.site}"))
        return 0
    if args.action == "remove":
        _out(args, await r("DELETE", f"{API}/sites/{args.site}"))
        return 0
    if args.action == "edit":
        _out(args, await r("PATCH", f"{API}/sites/{args.site}", json=_kv(args.pairs)))
        return 0
    body: dict[str, Any] = {"name": args.name or args.site or "Site", "kind": args.kind, "domains": args.domains,
                            "https": args.https, "spa": args.spa}
    if args.root:
        body["root"] = args.root
    if args.upstream:
        body["upstream"] = args.upstream
    if args.redirect:
        body["redirect_to"] = args.redirect
    if args.build_cmd or args.build_cwd:
        body["build"] = {"command": args.build_cmd, "cwd": args.build_cwd or args.root, "output": args.build_out}
    if args.password:
        body["user"] = args.user or "admin"
        body["password"] = getpass.getpass("Password visitors must enter: ")
    if args.target:
        body["targets"] = [{"account": t} for t in args.target]
    _out(args, await r("POST", f"{API}/sites", json=body))
    return 0


async def _vps(args, client) -> int:
    r = client._request
    a = args.account
    if args.action == "options":
        o = await r("GET", f"{API}/servers/{a}/options", timeout=60.0)
        if args.json:
            _out(args, o)
            return 0
        print("regions: " + ", ".join(x["id"] for x in o["regions"]))
        for s in sorted(o["sizes"], key=lambda s: s["monthly"])[:40]:
            print(f"  {s['id']:<20} {s['cpus']} CPU  {s['ram_gb']:g} GB RAM  {s['disk_gb']:g} GB disk  {s['monthly']:.2f}/month"
                  + (f"  ({s['region']})" if s.get("region") else ""))
        return 0
    if args.action == "list":
        _out(args, await r("GET", f"{API}/servers/{a}", timeout=60.0))
        return 0
    if args.action == "setup":
        return await _follow(args, client, await r("POST", f"{API}/servers/{a}/setup"))
    if args.action == "create":
        o = await r("GET", f"{API}/servers/{a}/options", timeout=60.0)
        size = next((s for s in o["sizes"] if s["id"] == args.size and (not s.get("region") or s["region"] == args.region)), None)
        if not size:
            print(f"no size {args.size} in {args.region}; see `abp host vps options {a}`", file=sys.stderr)
            return 2
        answer = input(f"Create {args.name} ({args.size} in {args.region}) for about {size['monthly']:.2f} a month, billed by "
                       f"the provider? Type the price to confirm: ")
        try:
            if abs(float(answer) - size["monthly"]) > 0.005:
                raise ValueError
        except ValueError:
            print("not confirmed; nothing created")
            return 2
        return await _follow(args, client, await r("POST", f"{API}/servers/{a}", json={
            "name": args.name, "region": args.region, "size": args.size, "confirm_monthly": size["monthly"]}))
    name = input(f"Destroying server {args.server} deletes everything on it. Type its name to confirm: ")
    _out(args, await r("DELETE", f"{API}/servers/{a}/{args.server}", params={"confirm": name}))
    return 0
