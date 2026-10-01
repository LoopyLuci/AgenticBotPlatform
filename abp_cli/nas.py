"""`abp nas ...`: the ABP File Server from the command line (the dashboard's /api/fileserver).

  abp nas status [--deep] | disks | stats | events | settings [key=value ...]
  abp nas server start|stop
  abp nas array show | set --disk name=path ... --parity path [--parity path2] [--block-kib 256] [--same-drive-ok]
                 | sync [--dry-run] | scrub [--percent 10] | fix [--disk d] [--file path ...] [--target folder]
  abp nas pool set <name> <path> | remove <name>
  abp nas share list | add <name> [key=value ...] | edit <name> key=value ... | remove <name> | smb <name> | nfs <name>
  abp nas user list | add <name> [--admin] (prompts for the password) | remove <name>
  abp nas mover | index [--share s] | search <words...> [--mode auto|words|meaning] | dups
  abp nas guard | unfreeze [share ...]
  abp nas remote list | set <name> <kind> key=value ... (secrets prompted) | remove <name>
  abp nas transfer list | set <name> --from SPEC --to SPEC [--mode copy|mirror|two-way] [--every MIN] | run <name> [--dry-run] | remove <name>
         SPEC is a folder, or remote:<name>[/sub/path]
  abp nas backup list | set <name> --repo FOLDER --source FOLDER ... [--every HOURS] | run <name> | check <name>
         | snapshots <name> | restore <name> <snapshot> <target> [--include PATH] | remove <name>
  abp nas apps | app install <app> <name> --mount key=share:<share>[/sub] ... [--port key=host:container]
"""
from __future__ import annotations

import asyncio
import getpass
import json
import sys
from typing import Any

A = "/api/fileserver"


def add_parser(sub) -> None:
    n = sub.add_parser("nas", help="ABP File Server: disks, parity, shares, users, search, transfers, backups, apps")
    ns = n.add_subparsers(dest="nas_cmd", required=True)
    p = ns.add_parser("status"); p.add_argument("--deep", action="store_true")
    for c in ("disks", "stats", "events", "mover", "dups", "guard", "apps"):
        ns.add_parser(c)
    p = ns.add_parser("settings"); p.add_argument("pairs", nargs="*")
    p = ns.add_parser("server"); p.add_argument("action", choices=["start", "stop"])
    p = ns.add_parser("array"); p.add_argument("action", choices=["show", "set", "sync", "scrub", "fix"])
    p.add_argument("--disk", action="append", default=[]); p.add_argument("--parity", action="append", default=[])
    p.add_argument("--block-kib", type=int, default=256, dest="block_kib"); p.add_argument("--same-drive-ok", action="store_true", dest="same")
    p.add_argument("--dry-run", action="store_true", dest="dry"); p.add_argument("--percent", type=float, default=10)
    p.add_argument("--file", action="append", default=[]); p.add_argument("--target", default="")
    p = ns.add_parser("pool"); p.add_argument("action", choices=["set", "remove"]); p.add_argument("name"); p.add_argument("path", nargs="?", default="")
    p = ns.add_parser("share"); p.add_argument("action", choices=["list", "add", "edit", "remove", "smb", "nfs"])
    p.add_argument("name", nargs="?", default=""); p.add_argument("pairs", nargs="*")
    p = ns.add_parser("user"); p.add_argument("action", choices=["list", "add", "remove"]); p.add_argument("name", nargs="?", default="")
    p.add_argument("--admin", action="store_true")
    p = ns.add_parser("index"); p.add_argument("--share", default="")
    p = ns.add_parser("search"); p.add_argument("words", nargs="+"); p.add_argument("--mode", default="auto")
    p = ns.add_parser("unfreeze"); p.add_argument("shares", nargs="*")
    p = ns.add_parser("remote"); p.add_argument("action", choices=["list", "set", "remove"]); p.add_argument("name", nargs="?", default="")
    p.add_argument("kind", nargs="?", default=""); p.add_argument("pairs", nargs="*")
    p = ns.add_parser("transfer"); p.add_argument("action", choices=["list", "set", "run", "remove"]); p.add_argument("name", nargs="?", default="")
    p.add_argument("--from", dest="src", default=""); p.add_argument("--to", dest="dst", default="")
    p.add_argument("--mode", default="copy", choices=["copy", "mirror", "two-way"]); p.add_argument("--every", type=int, default=0)
    p.add_argument("--dry-run", action="store_true", dest="dry")
    p = ns.add_parser("backup"); p.add_argument("action", choices=["list", "set", "run", "check", "snapshots", "restore", "remove"])
    p.add_argument("name", nargs="?", default=""); p.add_argument("rest", nargs="*")
    p.add_argument("--repo", default=""); p.add_argument("--source", action="append", default=[]); p.add_argument("--every", type=float, default=24)
    p.add_argument("--include", default="")
    p = ns.add_parser("app"); p.add_argument("action", choices=["install"]); p.add_argument("app"); p.add_argument("name")
    p.add_argument("--mount", action="append", default=[]); p.add_argument("--port", action="append", default=[])


def _kv(pairs: list[str]) -> dict:
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        k, _, v = pair.partition("=")
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v.replace(".", "", 1).isdigit():
            out[k] = float(v) if "." in v else int(v)
        elif v.startswith(("[", "{")):
            out[k] = json.loads(v)
        else:
            out[k] = v
    return out


def _spec(s: str) -> dict:
    if s.startswith("remote:"):
        name, _, sub = s[7:].partition("/")
        return {"remote": name, "path": sub}
    return {"path": s}


def _show(data: Any) -> None:
    print(json.dumps(data, indent=1, default=str))


async def _follow(client, started: dict) -> int:
    seen = 0
    while True:
        run = await client._request("GET", f"{A}/runs/{started['run']}", params={"since": seen})
        for line in run["log"]:
            print(line, flush=True)
        seen = run["log_total"]
        if run["done"]:
            if run.get("error"):
                print(f"error: {run['error']}", file=sys.stderr)
                return 1
            _show(run.get("result"))
            return 0
        await asyncio.sleep(1.5)


async def run(args, client) -> int:
    r = client._request
    c = args.nas_cmd
    if c == "status":
        o = await r("GET", A, params={"deep": args.deep}, timeout=120.0)
        if args.json:
            _show(o)
            return 0
        s = o["server"]
        print(f"file server: {'running at ' + s['urls']['web'] if s.get('running') else 'stopped'}")
        a = o["array"]
        if a["configured"]:
            print(f"array: {len(a['disks'])} data disk(s), {'dual' if a['dual_parity'] else 'single'} parity; last sync "
                  f"{a['last_sync'] or 'never'}; {a['pending_parity']} stripe(s) pending")
            for d in a["disks"]:
                print(f"  {d['name']:<8} {d['state']:<12} {d['files']} files  {d['path']}" + (f"  unsynced {d['unsynced']}" if "unsynced" in d else ""))
            for w in a["warnings"]:
                print(f"  ! {w}")
        print("shares: " + (", ".join(f"{x['name']} ({x['access']})" for x in o["shares"]) or "none"))
        for al in o["alerts"]:
            print(f"ALERT {al['share']}: score {al['score']}{' (frozen)' if al['frozen'] else ''}")
        for e in o["events"][:8]:
            print(f"  {e['level']:<7} {e['kind']:<9} {e['text']}")
        return 0
    if c in ("disks", "stats", "events", "dups", "guard", "apps"):
        path = {"disks": "/disks", "stats": "/stats", "events": "/events", "dups": "/duplicates", "guard": "/guard", "apps": "/apps"}[c]
        res = await r("GET", A + path, timeout=180.0)
        if c == "disks" and not args.json:
            for d in res["drives"]:
                print(f"{d['device']:<16} {d['model'][:30]:<30} {d.get('bus') or '':<6} {d['size'] >> 30:>6} GB  "
                      f"{d.get('os_health') or ('SMART ok' if d.get('smart_passed') else '')}  risk {d['risk']['band']}: {'; '.join(d['risk']['reasons'])}")
            for v in res["volumes"]:
                print(f"  {v['mount']:<12} {v['fs']:<6} {v['free'] >> 30} GB free of {v['size'] >> 30} GB")
        else:
            _show(res)
        return 0
    if c == "settings":
        _show(await (r("PUT", f"{A}/settings", json=_kv(args.pairs)) if args.pairs else r("GET", f"{A}/settings")))
        return 0
    if c == "server":
        _show(await r("POST", f"{A}/server/{args.action}"))
        return 0
    if c == "array":
        if args.action == "show":
            _show(await r("GET", f"{A}/array", params={"changes": True}, timeout=300.0))
            return 0
        if args.action == "set":
            disks = []
            for d in args.disk:
                name, _, path = d.partition("=")
                disks.append({"name": name, "path": path})
            _show(await r("PUT", f"{A}/array", json={"disks": disks, "parity": [{"path": p} for p in args.parity],
                                                    "block_kib": args.block_kib, "allow_same_drive": args.same}))
            return 0
        body = {"dry_run": args.dry} if args.action == "sync" else {"percent": args.percent} if args.action == "scrub" else \
            {"disk": (args.disk or [""])[0], "files": args.file, "target": args.target}
        return await _follow(client, await r("POST", f"{A}/array/{args.action}", json=body))
    if c == "pool":
        _show(await (r("PUT", f"{A}/pools/{args.name}", json={"path": args.path}) if args.action == "set" else r("DELETE", f"{A}/pools/{args.name}")))
        return 0
    if c == "share":
        a = args.action
        if a == "list":
            res = await r("GET", f"{A}/shares")
            if args.json:
                _show(res)
            for s in res if not args.json else []:
                print(f"{s['name']:<16} {s['access']:<8} cache {s['cache']:<6} {s.get('path') or s['allocation']}  {s.get('comment', '')}")
        elif a == "add":
            _show(await r("POST", f"{A}/shares", json={"name": args.name, "settings": _kv(args.pairs)}))
        elif a == "edit":
            _show(await r("PATCH", f"{A}/shares/{args.name}", json=_kv(args.pairs)))
        elif a == "remove":
            _show(await r("DELETE", f"{A}/shares/{args.name}"))
        else:
            res = await r("GET", f"{A}/shares/{args.name}/{a}")
            for line in res.get("commands", []):
                print(line)
            for note in res.get("notes", []):
                print(f"# {note}")
        return 0
    if c == "user":
        if args.action == "list":
            _show(await r("GET", f"{A}/users"))
        elif args.action == "add":
            pw = getpass.getpass(f"Password for {args.name}: ")
            if pw != getpass.getpass("Again: "):
                print("the passwords differ", file=sys.stderr)
                return 2
            _show(await r("POST", f"{A}/users", json={"name": args.name, "password": pw, "admin": args.admin}))
        else:
            _show(await r("DELETE", f"{A}/users/{args.name}"))
        return 0
    if c == "mover":
        return await _follow(client, await r("POST", f"{A}/mover", json={}))
    if c == "index":
        return await _follow(client, await r("POST", f"{A}/index", json={"share": args.share} if args.share else {}))
    if c == "search":
        res = await r("GET", f"{A}/search", params={"q": " ".join(args.words), "mode": args.mode}, timeout=120.0)
        if args.json:
            _show(res)
        for h in res if not args.json else []:
            print(f"{h['share']}/{h['path']}  ({h['kind']}, {h['size']} B){'  ' + h['snippet'] if h['snippet'] else ''}"
                  + (f"  [{', '.join(h['tags'])}]" if h["tags"] else ""))
        return 0
    if c == "unfreeze":
        _show(await r("POST", f"{A}/guard/unfreeze", json={"shares": args.shares or None}))
        return 0
    if c == "remote":
        if args.action == "list":
            _show(await r("GET", f"{A}/remotes"))
        elif args.action == "remove":
            _show(await r("DELETE", f"{A}/remotes/{args.name}"))
        else:
            fields = _kv(args.pairs)
            if args.kind in ("abp", "webdav") and "password" not in fields:
                fields["password"] = getpass.getpass("Password: ")
            if args.kind == "s3" and "secret_key" not in fields:
                fields["secret_key"] = getpass.getpass("Secret key: ")
            _show(await r("PUT", f"{A}/remotes/{args.name}", json={"kind": args.kind, "fields": fields}))
        return 0
    if c == "transfer":
        a = args.action
        if a == "list":
            _show(await r("GET", f"{A}/transfers"))
        elif a == "set":
            _show(await r("PUT", f"{A}/transfers/{args.name}", json={"source": _spec(args.src), "dest": _spec(args.dst),
                                                                    "mode": args.mode, "every_minutes": args.every}))
        elif a == "remove":
            _show(await r("DELETE", f"{A}/transfers/{args.name}"))
        else:
            return await _follow(client, await r("POST", f"{A}/transfers/{args.name}/run", params={"dry": args.dry}))
        return 0
    if c == "backup":
        a = args.action
        if a == "list":
            _show(await r("GET", f"{A}/backups"))
        elif a == "set":
            pw = getpass.getpass("Repository password (a new repository is created with it; it cannot be recovered): ")
            _show(await r("PUT", f"{A}/backups/{args.name}", json={"repo": args.repo, "sources": args.source, "password": pw,
                                                                  "every_hours": args.every}, timeout=120.0))
        elif a == "snapshots":
            _show(await r("GET", f"{A}/backups/{args.name}/snapshots", timeout=120.0))
        elif a == "remove":
            _show(await r("DELETE", f"{A}/backups/{args.name}"))
        elif a == "restore":
            if len(args.rest) < 2:
                print("restore <name> <snapshot> <target>", file=sys.stderr)
                return 2
            return await _follow(client, await r("POST", f"{A}/backups/{args.name}/restore",
                                                 json={"snapshot": args.rest[0], "target": args.rest[1], "include": args.include}))
        else:
            return await _follow(client, await r("POST", f"{A}/backups/{args.name}/{a}", json={}))
        return 0
    if c == "app":
        mounts = dict(m.split("=", 1) for m in args.mount)
        ports = dict(p.split("=", 1) for p in args.port) or None
        cat = {x["id"]: x for x in await r("GET", f"{A}/apps")}
        if args.app not in cat:
            print(f"no app {args.app}; see `abp nas apps`", file=sys.stderr)
            return 2
        if (await asyncio.to_thread(input, f"Install {cat[args.app]['title']}: Docker downloads {cat[args.app]['image']} from its registry. "
                                          "Go ahead? [y/N] ")).lower() != "y":
            return 2
        return await _follow(client, await r("POST", f"{A}/apps", json={"app": args.app, "name": args.name, "mounts": mounts, "ports": ports}))
    print(f"unknown nas subcommand {c!r}", file=sys.stderr)
    return 2
