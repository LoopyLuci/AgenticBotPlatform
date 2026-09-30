"""abp_modkit: make any project an ABP module.

    abp_modkit adopt PATH [--id ID] [--name NAME] [--dry-run] [--force] [--register] [--publish] [--push]
        Read the project, write abp-module.toml and abp-ops.toml (a refresh keeps hand edits), optionally register
        it with this ABP (modules.projects) and put it in its own private GitHub repo.
    abp_modkit detect PATH             what adopt would find, as JSON (writes nothing)
    abp_modkit check PATH              validate the files, start its hub, list its operations, bridge MCP, stop
    abp_modkit new PATH --lang python|node|powershell|shell [--name NAME]
        a new, empty project that is already a module (one example operation)
    abp_modkit serve --spec abp-ops.toml --project PATH --home DIR [--port 0] [--var k=v ...]
    abp_modkit mcp --home DIR          MCP over stdio for the hub running in DIR
    abp_modkit call --home DIR OP [JSON]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from abp_modkit import __version__


def _client(home: Path):
    try:
        c = json.loads((home / "control.json").read_text(encoding="utf-8"))
    except OSError:
        sys.exit(f"no hub is running in {home} (control.json is missing)")

    def request(method: str, path: str, body=None, timeout: float = 900):
        req = urllib.request.Request(c["url"] + path, method=method, headers={"Authorization": f"Bearer {c['token']}",
                                                                              "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - our own loopback hub
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read() or b"{}")
            except ValueError:
                d = {}
            raise RuntimeError(d.get("error", {}).get("message") or f"HTTP {e.code}") from None
    return request


def mcp(home: Path) -> None:
    """A stdio MCP server whose tools are the hub's operations (dots become underscores)."""
    request = _client(home)
    ops = request("GET", "/v1/operations")["operations"]
    by_name = {o["id"].replace(".", "_"): o for o in ops}
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}), flush=True)
            continue
        if "id" not in msg:
            continue
        mid, method = msg["id"], msg.get("method", "")
        try:
            if method == "initialize":
                res = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2024-11-05"),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "abp-modkit", "version": __version__}}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": [{"name": n, "description": o["summary"], "inputSchema": o["input_schema"],
                                  "annotations": {"readOnlyHint": not o["mutating"], "destructiveHint": o["destructive"]}}
                                 for n, o in by_name.items()]}
            elif method == "tools/call":
                p = msg.get("params") or {}
                op = by_name.get(p.get("name", ""))
                if op is None:
                    raise LookupError(f"no tool {p.get('name')}")
                try:
                    out = request("POST", f"/v1/call/{op['id']}", p.get("arguments") or {})["result"]
                    res = {"content": [{"type": "text", "text": json.dumps(out, indent=1, default=str)}], "isError": False}
                except RuntimeError as e:
                    res = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            else:
                raise NotImplementedError(f"no method {method}")
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
        except (LookupError, NotImplementedError) as e:
            code = -32602 if isinstance(e, LookupError) else -32601
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": str(e)}}), flush=True)


def check(path: Path, verbose: bool = True) -> dict:
    """Load both files, start a real hub over the project, list its operations, call service.status, bridge MCP."""
    import tomllib

    from abp_modkit import spec as sp
    res: dict = {"path": str(path), "checks": []}

    def ok(name: str, cond: bool, detail: str = "") -> bool:
        res["checks"].append({"check": name, "ok": bool(cond), **({"detail": detail} if detail and not cond else {})})
        if verbose:
            print(("  ok    " if cond else "  FAIL  ") + name + (f"  ({detail})" if detail and not cond else ""), flush=True)
        return bool(cond)
    try:
        m = tomllib.loads((path / "abp-module.toml").read_text(encoding="utf-8"))
        ok("abp-module.toml parses and has [module] and [hub]", "module" in m and "hub" in m)
        try:
            from bot.modules import manifest as abp_manifest    # ABP's own validation, when ABP is importable
            abp_manifest.parse(m, str(path / "abp-module.toml"))
            ok("ABP accepts the manifest", True)
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001
            ok("ABP accepts the manifest", False, str(e))
    except (OSError, tomllib.TOMLDecodeError) as e:
        ok("abp-module.toml parses", False, str(e))
    try:
        spec = sp.load(path / "abp-ops.toml")
        ok(f"abp-ops.toml loads ({len(spec.ops)} operations)", True)
    except (OSError, sp.SpecError) as e:
        ok("abp-ops.toml loads", False, str(e))
        res["ok"] = False
        return res
    home = Path(tempfile.mkdtemp(prefix="abp-modkit-check-"))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parent.parent),
                                                        os.environ.get("PYTHONPATH", "")])}
    p = subprocess.Popen([sys.executable, "-m", "abp_modkit", "serve", "--spec", str(path / "abp-ops.toml"), "--project",
                          str(path), "--home", str(home)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    try:
        for _ in range(100):
            if (home / "control.json").is_file() or p.poll() is not None:
                break
            time.sleep(0.1)
        if not ok("the hub starts", (home / "control.json").is_file(),
                  (p.stderr.read().decode(errors="replace")[-400:] if p.poll() is not None else "")):
            res["ok"] = False
            return res
        request = _client(home)
        ops = request("GET", "/v1/operations")["operations"]
        ok(f"it lists {len(ops)} operations", len(ops) >= len(spec.ops) + 4)
        st = request("POST", "/v1/call/service.status", {})["result"]
        ok("service.status answers", st.get("module") == spec.id)
        mp = subprocess.run([sys.executable, "-m", "abp_modkit", "mcp", "--home", str(home)], capture_output=True,
                            text=True, timeout=60, env=env, input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
        tools = json.loads(mp.stdout.splitlines()[0])["result"]["tools"] if mp.stdout.strip() else []
        ok("MCP lists them as tools", len(tools) == len(ops))
        res["operations"] = len(ops)
        request("POST", "/v1/service/stop", {})
        p.wait(15)
        ok("it stops and removes its control file", not (home / "control.json").exists())
    finally:
        if p.poll() is None:
            p.kill()
    res["ok"] = all(c["ok"] for c in res["checks"])
    return res


TEMPLATES = {
    "python": ("tool.py", '"""{name}: an ABP module. `python tool.py hello --who you`"""\nimport argparse\nimport json\n'
               'import os\n\n\ndef main():\n    ap = argparse.ArgumentParser()\n    sub = ap.add_subparsers(dest="cmd", '
               'required=True)\n    h = sub.add_parser("hello", help="Say hello")\n    h.add_argument("--who", default="world")'
               '\n    a = ap.parse_args()\n    if a.cmd == "hello":\n        print(json.dumps({{"hello": a.who, "args": '
               'json.loads(os.environ.get("ABP_OP_ARGS") or "{{}}")}}))\n\n\nif __name__ == "__main__":\n    main()\n'),
    "node": ("index.js", '#!/usr/bin/env node\n// {name}: an ABP module. `node index.js hello you`\nconst [cmd, who = '
             '"world"] = process.argv.slice(2);\nif (cmd === "hello") console.log(JSON.stringify({{ hello: who }}));\n'
             'else {{ console.error("usage: index.js hello [who]"); process.exit(2); }}\n'),
    "powershell": ("{name}.psm1", '# {name}: an ABP module. Every exported function is an ABP operation.\nfunction '
                   'Get-Greeting {{\n    param([Parameter(Mandatory)][string]$Who)\n    [pscustomobject]@{{ hello = $Who '
                   '}}\n}}\nExport-ModuleMember -Function Get-Greeting\n'),
    "shell": ("hello.sh", '#!/usr/bin/env bash\n# {name}: an ABP module.\necho "{{\\"hello\\": \\"${{1:-world}}\\"}}"\n'),
}


def new(path: Path, lang: str, name: str) -> dict:
    from abp_modkit import adopt as ad
    from abp_modkit import spec as sp
    from abp_modkit.detect import slug
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise SystemExit(f"{path} is not empty; use `adopt` for an existing project")
    name = name or path.name
    fname, body = TEMPLATES[lang]
    fname = fname.format(name=name)
    (path / fname).write_text(body.format(name=name), encoding="utf-8", newline="\n")
    (path / "README.md").write_text(f"# {name}\n\nAn ABP module (made with `abp_modkit new`). What it can do is listed in "
                                    "abp-ops.toml; ABP drives it through abp_modkit's hub.\n", encoding="utf-8",
                                    newline="\n")
    if lang == "node":
        (path / "package.json").write_text(json.dumps({"name": slug(name), "version": "0.1.0", "private": True,
                                                       "bin": {slug(name): "index.js"},
                                                       "scripts": {"hello": "node index.js hello"}}, indent=2) + "\n",
                                           encoding="utf-8", newline="\n")
    res = ad.adopt(path, name=name)
    if lang == "shell":
        s = sp.load(path / "abp-ops.toml")
        s.ops.append(sp.Op(id="hello.run", kind="cmd", argv=["bash", "hello.sh", "{who}"], mutating_flag=False,
                           inputs={"who": {"type": "string", "description": "who to greet"}}, summary="Say hello"))
        (path / "abp-ops.toml").write_text(sp.dump(s), encoding="utf-8", newline="\n")
    return res


def _register(path: Path) -> str:
    try:
        from bot.config import config
    except ImportError:
        return "not registered: run this with ABP's python to add it to ABP (modules.projects)"
    config.load()
    projects = list(((config.current or {}).get("modules") or {}).get("projects") or [])
    p = str(path.resolve()).replace("\\", "/")
    if p not in projects:
        projects.append(p)
        config.set_value(("modules", "projects"), projects, actor="abp_modkit")
        return f"registered with ABP (modules.projects has {len(projects)})"
    return "already registered with ABP"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="abp_modkit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("adopt")
    a.add_argument("path")
    a.add_argument("--id", default="")
    a.add_argument("--name", default="")
    a.add_argument("--description", default="", help="one sentence (default: the README's first one)")
    a.add_argument("--repo", default="", help="the repo URL to record (default: its origin, or LoopyLuci/<folder>)")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true", help="rewrite both files from scratch (hand edits are lost)")
    a.add_argument("--register", action="store_true", help="add it to this ABP's modules.projects")
    a.add_argument("--publish", action="store_true", help="commit the module files and create its private GitHub repo")
    a.add_argument("--push", action="store_true", help="with --publish: also push to a repo it already has")
    a.add_argument("--owner", default="LoopyLuci")
    d = sub.add_parser("detect")
    d.add_argument("path")
    c = sub.add_parser("check")
    c.add_argument("path")
    n = sub.add_parser("new")
    n.add_argument("path")
    n.add_argument("--lang", choices=sorted(TEMPLATES), default="python")
    n.add_argument("--name", default="")
    s = sub.add_parser("serve")
    s.add_argument("--spec", required=True)
    s.add_argument("--project", required=True)
    s.add_argument("--home", required=True)
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--base-url")
    s.add_argument("--var", action="append", default=[], help="k=v placeholder values (ABP passes target=...)")
    m = sub.add_parser("mcp")
    m.add_argument("--home", required=True)
    k = sub.add_parser("call")
    k.add_argument("--home", required=True)
    k.add_argument("op")
    k.add_argument("args", nargs="?", default="{}")
    o = ap.parse_args(argv)
    caller = os.environ.get("ABP_CALLER_CWD")    # the launchers cd to ABP_HOME; paths given are the caller's
    if caller and os.path.isdir(caller) and o.cmd in ("adopt", "detect", "check", "new"):
        o.path = os.path.join(caller, o.path)

    if o.cmd == "serve":
        from abp_modkit import hub, spec
        variables = dict(v.split("=", 1) for v in o.var if "=" in v)
        hub.serve(hub.Hub(spec.load(o.spec), Path(o.project), Path(o.home), o.base_url, variables), o.host, o.port)
    elif o.cmd == "adopt":
        from abp_modkit import adopt as ad
        res = ad.adopt(o.path, mid=o.id, name=o.name, repo=o.repo, dry_run=o.dry_run, force=o.force,
                       description=o.description)
        if o.dry_run:
            for f, text in (res.pop("files") or {}).items():
                print(f"----- {f}\n{text}")
        else:
            res.pop("files", None)
        if o.register and not o.dry_run:
            res["registered"] = _register(Path(o.path))
        if o.publish and not o.dry_run:
            from abp_modkit import repo as rp
            res["publish"] = rp.publish(o.path, stacks=res["stacks"], owner=o.owner, only=["abp-module.toml", "abp-ops.toml"],
                                        push=o.push or not (Path(o.path) / ".git").exists(), description=res["description"])
        print(json.dumps(res, indent=1, default=str))
    elif o.cmd == "detect":
        from abp_modkit import detect as dt
        print(json.dumps(dt.summary(dt.detect(o.path)), indent=1))
    elif o.cmd == "check":
        return 0 if check(Path(o.path).resolve())["ok"] else 1
    elif o.cmd == "new":
        print(json.dumps({k: v for k, v in new(Path(o.path), o.lang, o.name).items() if k != "files"}, indent=1))
    elif o.cmd == "mcp":
        mcp(Path(o.home))
    elif o.cmd == "call":
        print(json.dumps(_client(Path(o.home))("POST", f"/v1/call/{o.op}", json.loads(o.args))["result"], indent=1,
                         default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
