"""`abp ai ...` and `abp lab ...`: ABP's local AI and the Neural Lab from the command line (/api/localai, /api/lab).

  abp ai status | serve-status | settings [key=value ...] | server start|stop
               | engine install [--backend hip|vulkan|cuda|cpu]
  abp ai models | list | ps | pull <name> | rm <name> | cp <src> <dst> | show <name>
  abp ai run <model> <prompt...> [--generate] [--system TEXT]   a real inference run on a local model
  abp ai import <name> <file.gguf> [--reference] | create <name> -f Modelfile
  abp ai discover | adopt-all [--import]
  abp ai train setup | list | start <base> <data.jsonl> ... [--name n] [--method sft|dpo] [--steps N] [--epochs E]
               [--lr X] [--rank R] [--export q4_k_m] | status <run> | stop <run> | export <run> [--quant q] | amethyst <run> <name>
  abp lab status | runs | run <id> | stop <id> | designs | validate <spec.json>
  abp lab train <spec.json | design> --data <file> [--target col] [--tokenizer t] [--steps N] [--wait]
  abp lab import brainbuilder <file.bbir.edn> | kotmoe <registry id> | kmoe <model.kmoe>   [--name n]
  abp lab projects | systune | advice transfer <src> <dst> [--size-gb G] [--files N] | advice llm <model>
               | advice memory | advice stability | bench drives|llm | retrain <kind> | telemetry | hw
(Inference itself: any Ollama client with OLLAMA_HOST=http://127.0.0.1:11436, or the OpenAI API at /v1.)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

A, L = "/api/localai", "/api/lab"


def add_parser(sub) -> None:
    n = sub.add_parser("ai", help="ABP's local AI (Ollama and Unsloth in ABP): models, the server, fine-tuning")
    ns = n.add_subparsers(dest="ai_cmd", required=True)
    for c in ("status", "list", "models", "ps", "discover"):
        ns.add_parser(c)
    ns.add_parser("serve-status", help="just the inference server: is it up, where, which models are loaded")
    p = ns.add_parser("run", help="a real inference run on a local model (chat by default, --generate to complete)")
    p.add_argument("model")
    p.add_argument("prompt", nargs="+", help="the prompt; joined with spaces")
    p.add_argument("--generate", action="store_true", help="a completion instead of a chat turn")
    p.add_argument("--system", default="")
    p.add_argument("--timeout", type=float, default=900.0)
    p = ns.add_parser("settings"); p.add_argument("pairs", nargs="*")
    p = ns.add_parser("server"); p.add_argument("action", choices=["start", "stop"])
    p = ns.add_parser("engine"); p.add_argument("action", choices=["install"]); p.add_argument("--backend", default="")
    for c in ("pull", "rm", "show"):
        p = ns.add_parser(c); p.add_argument("name")
    p = ns.add_parser("cp"); p.add_argument("src"); p.add_argument("dst")
    p = ns.add_parser("import"); p.add_argument("name"); p.add_argument("path"); p.add_argument("--reference", action="store_true")
    p = ns.add_parser("create"); p.add_argument("name"); p.add_argument("-f", "--file", required=True)
    p = ns.add_parser("adopt-all"); p.add_argument("--import", action="store_true", dest="imp")
    p = ns.add_parser("train"); p.add_argument("action", choices=["setup", "list", "start", "status", "stop", "export", "amethyst"])
    p.add_argument("rest", nargs="*"); p.add_argument("--name", default=""); p.add_argument("--method", default="sft")
    p.add_argument("--steps", type=int, default=0); p.add_argument("--epochs", type=float, default=0); p.add_argument("--lr", type=float, default=0)
    p.add_argument("--rank", type=int, default=0); p.add_argument("--export", default="q4_k_m"); p.add_argument("--quant", default="")

    lab = sub.add_parser("lab", help="the Neural Lab: designs, GPU training, BrainBuilder/KotMoE/Amethyst/Kestrion, system models")
    ls = lab.add_subparsers(dest="lab_cmd", required=True)
    for c in ("status", "runs", "designs", "projects", "systune", "telemetry", "hw"):
        ls.add_parser(c)
    for c in ("run", "stop"):
        p = ls.add_parser(c); p.add_argument("id")
    p = ls.add_parser("validate"); p.add_argument("spec")
    p = ls.add_parser("train"); p.add_argument("spec"); p.add_argument("--data", required=True); p.add_argument("--target", default="")
    p.add_argument("--tokenizer", default=""); p.add_argument("--steps", type=int, default=0); p.add_argument("--wait", action="store_true")
    p = ls.add_parser("import"); p.add_argument("kind", choices=["brainbuilder", "kotmoe", "kmoe"]); p.add_argument("what")
    p.add_argument("--name", default="")
    p = ls.add_parser("advice"); p.add_argument("kind", choices=["transfer", "llm", "memory", "stability"]); p.add_argument("rest", nargs="*")
    p.add_argument("--size-gb", type=float, default=4, dest="size_gb"); p.add_argument("--files", type=int, default=100)
    p = ls.add_parser("bench"); p.add_argument("kind", choices=["drives", "llm"])
    p = ls.add_parser("retrain"); p.add_argument("kind", choices=["transfer", "llm", "memory", "stability"])


def _show(data: Any) -> None:
    print(json.dumps(data, indent=1, default=str))


def _kv(pairs: list[str]) -> dict:
    out: dict[str, Any] = {}
    for pair in pairs:
        k, eq, v = pair.partition("=")
        if not eq:
            raise SystemExit(f"expected key=value, got {pair!r}")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


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
            if run.get("result") is not None:
                _show(run["result"])
            return 0
        await asyncio.sleep(1.5)


def _size(n: int) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


async def run(args, client) -> int:
    if getattr(args, "lab_cmd", None):
        return await _lab(args, client)
    r = client._request
    c = args.ai_cmd
    if c == "status":
        o = await r("GET", A, timeout=120.0)
        if args.json:
            _show(o)
            return 0
        s = o["server"]
        print(f"server: {'running at ' + s['url'] + ' (Ollama API ' + str(s.get('version')) + ')' if s.get('running') else 'stopped'}")
        e = o.get("engine")
        print(f"engine: {'llama.cpp ' + e['build'] + ' (' + e['backend'] + ')' if e else 'not installed: abp ai engine install'}")
        for g in o["gpus"]:
            print(f"gpu: {g['name']} {g.get('vram_gb', '')} GB")
        print(f"models: {len(o['models'])} in {o['home']}")
        for m in o["running"]:
            print(f"  loaded: {m['name']} ({_size(m.get('size_vram') or m.get('size') or 0)})")
        print(f"training environment: {'ready' if o['train_env']['installed'] else 'not set up: abp ai train setup'}")
        for run_ in o["runs"][:5]:
            print(f"  run {run_['id']} {run_.get('state')} {run_.get('name')} step {run_.get('step', '-')} loss {run_.get('loss', '-')}")
        return 0
    if c == "list" or c == "models":
        ms = await r("GET", f"{A}/models", timeout=60.0)
        if args.json:
            _show(ms)
            return 0
        print(f"{'NAME':<50} {'SIZE':>9}  {'QUANT':<8} SOURCE")
        for m in ms:
            print(f"{m['name']:<50} {_size(m['size']):>9}  {m['details'].get('quantization_level', ''):<8} {m.get('source', '')}")
        return 0
    if c == "ps":
        _show((await r("GET", A, timeout=60.0))["running"])
        return 0
    if c == "serve-status":
        o = await r("GET", A, timeout=120.0)
        if args.json:
            _show({"server": o["server"], "running": o["running"]})
            return 0
        s = o["server"]
        if s.get("running"):
            version = f" (Ollama {s['version']})" if s.get("version") else ""
            print(f"server: running at {s['url']}{version}")
        else:
            print("server: stopped (abp ai server start)")
        for m in o["running"]:
            print(f"  loaded: {m['name']} ({_size(m.get('size_vram') or m.get('size') or 0)})")
        if not o["running"]:
            print("  no models loaded")
        return 0
    if c == "run":
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            print("the prompt is empty", file=sys.stderr)
            return 2
        if args.generate:
            body: dict[str, Any] = {"model": args.model, "prompt": prompt, "stream": False}
            operation = "generate"
            if args.system:
                body["system"] = args.system
        else:
            message = {"role": "user", "content": prompt}
            body = {"model": args.model, "messages": [message], "stream": False}
            operation = "chat"
            if args.system:
                body["messages"].insert(0, {"role": "system", "content": args.system})
        out = await r("POST", "/api/ollama/call", json={"operation": operation, "args": body,
                                                        "timeout_s": args.timeout})
        text = (out.get("result") or {}).get("message", {}).get("content") if operation == "chat" \
            else (out.get("result") or {}).get("response")
        if args.json:
            _show(out)
            return 0
        print(text if text is not None else json.dumps(out.get("result"), indent=1, default=str))
        return 0
    if c == "settings":
        _show(await r("PUT", f"{A}/settings", json=_kv(args.pairs)) if args.pairs else await r("GET", f"{A}/settings"))
        return 0
    if c == "server":
        _show(await r("POST", f"{A}/server/{args.action}", timeout=60.0))
        return 0
    if c == "engine":
        return await _follow(client, await r("POST", f"{A}/engine/install", json={"backend": args.backend}))
    if c == "pull":
        return await _follow(client, await r("POST", f"{A}/models/pull", json={"name": args.name}))
    if c == "rm":
        _show(await r("DELETE", f"{A}/models", params={"name": args.name}))
        return 0
    if c == "cp":
        _show(await r("POST", f"{A}/models/copy", json={"source": args.src, "destination": args.dst}))
        return 0
    if c == "show":
        rec = await r("GET", f"{A}/models/show", params={"name": args.name})
        print(rec["modelfile"]) if not args.json else _show(rec)
        return 0
    if c == "import":
        _show(await r("POST", f"{A}/models/import", json={"name": args.name, "path": str(Path(args.path).resolve()),
                                                         "reference": args.reference}, timeout=600.0))
        return 0
    if c == "create":
        f = Path(args.file)
        _show(await r("POST", f"{A}/models/create", json={"name": args.name, "modelfile": f.read_text(encoding="utf-8"),
                                                         "dir": str(f.resolve().parent)}, timeout=600.0))
        return 0
    if c == "discover":
        d = await r("GET", f"{A}/discover", timeout=180.0)
        if args.json:
            _show(d)
            return 0
        for loc in d["locations"]:
            if loc["exists"]:
                print(f"{loc['app']:<13} {loc['models']:>4} model(s)  {loc['path']}")
        for it in d["found"]:
            tag = "in use" if it.get("adopted") else ", ".join(it.get("actions", []))
            print(f"  {it['kind']:<13} {it.get('name') or it['path']}  [{tag}]")
        return 0
    if c == "adopt-all":
        _show(await r("POST", f"{A}/discover/adopt-all", json={"gguf": "import" if args.imp else "reference"}, timeout=1800.0))
        return 0
    if c == "train":
        return await _train(args, client)
    return 2


async def _train(args, client) -> int:
    r = client._request
    a = args.action
    if a == "setup":
        return await _follow(client, await r("POST", f"{A}/train/setup"))
    if a == "list":
        t = await r("GET", f"{A}/train", timeout=120.0)
        if args.json:
            _show(t)
            return 0
        env = t["env"]
        print(f"environment: {'torch ' + str(env.get('torch')) + ' on ' + (env.get('device') or 'no GPU') if env.get('installed') else 'not set up'}")
        for x in t["runs"]:
            print(f"  {x['id']}  {x.get('state', ''):<9} {x.get('method', '')} {x.get('base', '')} -> {x.get('name', '')}  "
                  f"step {x.get('step', '-')}/{x.get('total_steps', '-')} loss {x.get('loss', '-')}")
        for d in t["datasets"]:
            print(f"  dataset ({d['origin']}): {d['path']} {d.get('rows') or ''} rows")
        return 0
    if a == "start":
        if len(args.rest) < 2:
            raise SystemExit("abp ai train start <base> <data.jsonl> [more data...]")
        body: dict = {"base": args.rest[0], "data": [str(Path(p).resolve()) for p in args.rest[1:]], "name": args.name,
                      "method": args.method, "export": args.export}
        for k, v in (("max_steps", args.steps), ("epochs", args.epochs), ("learning_rate", args.lr), ("rank", args.rank)):
            if v:
                body[k] = v
        st = await r("POST", f"{A}/train", json=body, timeout=600.0)
        _show(st)
        print(f"follow it: abp ai train status {st['id']}")
        return 0
    if not args.rest:
        raise SystemExit(f"abp ai train {a} <run>")
    rid = args.rest[0]
    if a == "status":
        st = await r("GET", f"{A}/train/{rid}")
        print(st.pop("log", "")) if not args.json else None
        _show(st)
        return 0
    if a == "stop":
        _show(await r("POST", f"{A}/train/{rid}/stop"))
        return 0
    if a == "export":
        return await _follow(client, await r("POST", f"{A}/train/{rid}/export", json={"quant": args.quant, "name": args.name}))
    if a == "amethyst":
        if len(args.rest) < 2:
            raise SystemExit("abp ai train amethyst <run> <module name>")
        return await _follow(client, await r("POST", f"{A}/train/{rid}/amethyst", json={"name": args.rest[1]}))
    return 2


def _spec_arg(s: str) -> dict:
    p = Path(s)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"design": s}


async def _lab(args, client) -> int:
    r = client._request
    c = args.lab_cmd
    if c == "status":
        o = await r("GET", L, timeout=120.0)
        if args.json:
            _show(o)
            return 0
        print("projects: " + ", ".join(f"{p['folder']}{'' if p['present'] else ' (missing)'}" for p in o["projects"]))
        print(f"designs: {len(o['designs'])}; telemetry {'recording' if o['recording'] else 'off'}")
        for x in o["runs"][:8]:
            fin = x.get("final") or {}
            print(f"  {x['id']}  {x.get('state', ''):<9} {x['name']:<28} {x['params']:>12,} params  "
                  + "  ".join(f"{k}={v}" for k, v in fin.items() if k.startswith("val_")))
        st = o["systune"]
        for k, v in st["models"].items():
            print(f"  system model {k:<10} {'adopted' if v['adopted'] else 'not yet'}  {v['measurements']}/{v['needed']} measurements  {v.get('metrics') or ''}")
        pol = st["cpu_policy"]
        print(f"  CPU policy: {pol['level']}, {pol['threads']} thread(s), avoiding {pol['avoid'] or 'nothing'}")
        for why in pol["reasons"]:
            print(f"    - {why}")
        return 0
    if c == "runs":
        _show(await r("GET", f"{L}/runs"))
        return 0
    if c == "run":
        _show(await r("GET", f"{L}/runs/{args.id}"))
        return 0
    if c == "stop":
        _show(await r("POST", f"{L}/runs/{args.id}/stop"))
        return 0
    if c == "designs":
        _show(await r("GET", f"{L}/designs"))
        return 0
    if c == "projects":
        _show({"projects": await r("GET", f"{L}/projects"), "brainbuilder": await r("GET", f"{L}/brainbuilder"),
               "kotmoe": await r("GET", f"{L}/kotmoe")})
        return 0
    if c == "validate":
        v = await r("POST", f"{L}/validate", json={"spec": json.loads(Path(args.spec).read_text(encoding="utf-8"))})
        print(v["text"]) if not args.json else _show(v)
        return 0
    if c == "train":
        sp = _spec_arg(args.spec)
        data = {"path": str(Path(args.data).resolve())}
        if args.target:
            data["target"] = args.target
        if args.tokenizer:
            data["tokenizer"] = args.tokenizer
        body = {**({"spec": sp} if "design" not in sp else sp), "data": data, "train": {"max_steps": args.steps} if args.steps else {}}
        st = await r("POST", f"{L}/runs", json=body, timeout=120.0)
        print(f"run {st['id']}: {st['name']} ({st['params']:,} parameters) on the GPU")
        if not args.wait:
            return 0
        while True:
            s = await r("GET", f"{L}/runs/{st['id']}")
            print(f"  {s.get('state')} step {s.get('step', '-')}/{s.get('total_steps', '-')} loss {s.get('loss', '-')} "
                  + " ".join(f"{k}={v}" for k, v in s.items() if k.startswith("val_")), flush=True)
            if s.get("state") not in ("starting", "loading", "training", "stopping"):
                _show({k: s.get(k) for k in ("state", "error", "final", "export")})
                return 0 if s.get("state") == "done" else 1
            await asyncio.sleep(3)
    if c == "import":
        kind = {"brainbuilder": "brainbuilder", "kotmoe": "kotmoe-registry", "kmoe": "kotmoe-checkpoint"}[args.kind]
        body = {"kind": kind, "name": args.name, **({"id": args.what} if kind == "kotmoe-registry" else {"path": str(Path(args.what).resolve())})}
        v = await r("POST", f"{L}/import", json=body)
        print(v["text"]) if not args.json else _show(v)
        return 0
    if c == "systune":
        _show(await r("GET", f"{L}/systune", timeout=120.0))
        return 0
    if c == "advice":
        params: dict = {"kind": args.kind}
        if args.kind == "transfer":
            if len(args.rest) < 2:
                raise SystemExit("abp lab advice transfer <src> <dst>")
            params.update({"src": args.rest[0], "dst": args.rest[1], "size_gb": args.size_gb, "files": args.files})
        elif args.kind == "llm":
            params["model"] = args.rest[0] if args.rest else ""
        _show(await r("GET", f"{L}/systune/advice", params=params, timeout=120.0))
        return 0
    if c == "bench":
        return await _follow(client, await r("POST", f"{L}/systune/bench", json={"kind": "transfer" if args.kind == "drives" else "llm"}))
    if c == "retrain":
        _show(await r("POST", f"{L}/systune/train", json={"kind": args.kind}))
        return 0
    if c == "telemetry":
        t = await r("GET", f"{L}/telemetry", params={"seconds": 300})
        _show(t["stats"])
        if t["samples"]:
            s = t["samples"][-1]
            print(f"now: CPU {s['cpu']}% (max core {s['cpu_max']}%), RAM {s['ram_used']}%, GPU {s.get('gpu_compute') or s.get('gpu_3d')}% "
                  f"{s.get('gpu_mem_gb')} GB, {time.strftime('%H:%M:%S', time.localtime(s['t']))}")
        return 0
    if c == "hw":
        _show(await r("GET", f"{L}/telemetry/hw", timeout=120.0))
        return 0
    return 2
