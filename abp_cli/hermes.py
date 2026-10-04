"""`abp hermes ...`: Hermes Agent's own messaging gateway and one-shot `hermes -z`, from the command line.

  abp hermes status [--home DIR]        `hermes gateway status`, parsed (pids, service, raw text)
  abp hermes list                       every Hermes profile and whether its gateway is running
  abp hermes start|stop|restart [--home DIR]
                                        start reuses Hermes's own login-item launcher, so the gateway
                                        outlives this command, AgenticBotPlatform, and any Job Object
  abp hermes logs [--home DIR] [--lines N]   tail of <HERMES_HOME>/logs/gateway*.log
  abp hermes instances                  the AgenticBotPlatform bot instances whose Telegram token is
                                        currently served by a Hermes gateway (ABP is not polling those)
  abp hermes ask <text...> [--instance ID] [--home DIR] [--model M]
                                        one-shot `hermes -z` - works while the gateway serves Telegram

Every command takes --json like the rest of abp_cli, and no command ever prints a bot token: the
API returns only a sha256 fingerprint of the token each home owns.
"""
from __future__ import annotations

import json
import sys
from typing import Any

A = "/api/hermes"


def add_parser(sub) -> None:
    h = sub.add_parser("hermes", help="Hermes Agent's own gateway (status/list/start/stop/restart/logs) and `hermes ask`")
    hs = h.add_subparsers(dest="hermes_cmd", required=True)

    for name in ("status", "start", "stop", "restart", "logs"):
        p = hs.add_parser(name)
        p.add_argument("--home", default="", help="a specific Hermes home (default: this machine's HERMES_HOME)")
        if name == "logs":
            p.add_argument("--lines", type=int, default=80)
    hs.add_parser("list", help="every Hermes profile and whether its gateway is running")
    hs.add_parser("instances", help="bot instances currently served by a Hermes gateway instead of ABP")

    p = hs.add_parser("ask", help="one-shot `hermes -z <text>` (works while the gateway serves Telegram)")
    p.add_argument("text", nargs="+")
    p.add_argument("--instance", type=int, default=None, help="a bot instance whose hermes_home/session to use")
    p.add_argument("--home", default="")
    p.add_argument("--model", default="")


def _out(args, data: Any) -> None:
    print(json.dumps(data, indent=1))


async def run(args, client) -> int:
    c = args.hermes_cmd
    r = client._request
    home = getattr(args, "home", "") or None
    if c == "status":
        o = await r("GET", f"{A}/gateway/status", params={"home": home} if home else None)
        if args.json:
            _out(args, o)
            return 0
        print(f"Hermes gateway: {'running' if o['running'] else 'not running'}  ({o['home']})")
        if o["pids"]:
            print(f"  pids: {', '.join(str(p) for p in o['pids'])}")
        if o.get("service"):
            print(f"  service: {o['service']}")
        if o.get("reported_running") and not o["running"]:
            # Hermes's own process-table scan sees a gateway ABP cannot confirm
            # from this home's state files. Say so rather than pick a side.
            print(f"  hermes reports pids {', '.join(str(p) for p in o.get('reported_pids') or [])}"
                  " that ABP could not confirm alive from this home's gateway.pid")
        if o.get("raw"):
            for line in o["raw"].splitlines():
                print(f"  | {line}")
        return 0
    if c == "list":
        o = await r("GET", f"{A}/gateway/list")
        if args.json:
            _out(args, o)
            return 0
        rows = o["profiles"]
        if not rows:
            print("no Hermes profiles found")
            return 0
        for p in rows:
            mark = "*" if p["current"] else " "
            print(f" {mark} {p['profile']:<20} {'running' if p['running'] else 'not running'}"
                  + (f"  {p['state']}" if p["state"] else ""))
        return 0
    if c in ("start", "stop", "restart"):
        o = await r("POST", f"{A}/gateway/{c}", json={"home": home} if home else {}, timeout=180.0)
        if args.json:
            _out(args, o)
            return 0
        print(f"{c}: {'ok' if o.get('ok') else 'the gateway did not come up'}"
              + (f" (via {o['via']})" if o.get("via") else "")
              + f"  home={o.get('home')}  pids={', '.join(str(p) for p in o.get('pids') or []) or '-'}")
        if o.get("output"):
            for line in str(o["output"]).splitlines():
                print(f"  | {line}")
        return 0 if o.get("ok", True) else 1
    if c == "logs":
        o = await r("GET", f"{A}/gateway/logs", params={"home": home, "lines": args.lines} if home
                    else {"lines": args.lines})
        if args.json:
            _out(args, o)
            return 0
        print(o["tail"] or f"(no gateway*.log under {o['home']}/logs)")
        return 0
    if c == "instances":
        bots = await r("GET", "/api/bots")
        served = [b for b in bots if b.get("served_by")]
        if args.json:
            _out(args, {"served": [{"id": b["id"], "name": b["name"], "served_by": b["served_by"]} for b in served]})
            return 0
        if not served:
            print("every Telegram bot instance's token is ABP's to poll")
            return 0
        print(f"{len(served)} Telegram instance(s) served by a Hermes gateway (AgenticBotPlatform is not polling them):")
        for b in served:
            print(f"  {b['id']:>4}  {b['name']:<24} {b['served_by']}")
        return 0
    if c == "ask":
        body: dict[str, Any] = {"text": " ".join(args.text)}
        if args.instance is not None:
            body["instance_id"] = args.instance
        if home:
            body["hermes_home"] = home
        if args.model:
            body["model"] = args.model
        o = await r("POST", f"{A}/ask", json=body, timeout=900.0)
        if args.json:
            _out(args, o)
            return 0
        print(o["text"])
        return 0
    print(f"unknown hermes subcommand {c!r}", file=sys.stderr)
    return 2
