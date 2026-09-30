"""What a project is and what it can do, read from its files: its stacks, how to build it, its server (if any), its
commands, its MCP server, its windows, and every operation ABP can offer for it.

Nothing is run or installed; everything is read from the checkout. The result (a Plan) is what `adopt` writes out.
"""
from __future__ import annotations

import json
import re
import tomllib
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abp_modkit import routes as rt
from abp_modkit.spec import Op, Service

RESERVED_GROUPS = {"service", "jobs", "api", "project"}
AREAS = [("models", r"\bmodel|llm|inference|moe\b|gateway|embedding|transformer|gguf"),
         ("agents", r"\bagent|assistant|companion|copilot|autonomous"),
         ("security", r"malware|opsec|pentest|security|forensic|reverse.engineer"),
         ("devices", r"android|mobile|device|screen|input.forward|emulat"),
         ("infrastructure", r"cluster|mesh|nix|vm\b|virtual machine|power|cache|transfer|stream"),
         ("building", r"browser|extension|worker|build|code|ide\b|compil"),
         ("data", r"prompt|crm|task|project manag|spending|budget|finance|plant|monitor|transcri|caption")]


def slug(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    s = re.sub(r"-+", "-", s)
    if not s or not s[0].isalpha():
        s = "m-" + s
    return s[:41].rstrip("-")


def stable_port(mid: str, n: int = 0) -> int:
    """A port for the project's server that stays the same across runs (and machines) and avoids common ones."""
    return 21000 + (zlib.crc32(f"{mid}:{n}".encode()) % 18000)


@dataclass
class Plan:
    root: Path
    id: str
    name: str
    description: str = ""
    area: str = "tools"
    stacks: list[str] = field(default_factory=list)
    requires: list[dict] = field(default_factory=list)
    build: list[list[str]] = field(default_factory=list)
    build_env: dict[str, str] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    service: Service | None = None
    ops: list[Op] = field(default_factory=list)
    mcp_native: list[str] = field(default_factory=list)
    gui: list[str] = field(default_factory=list)
    tui: list[str] = field(default_factory=list)
    pipeline: list[str] = field(default_factory=list)
    host_os: list[str] = field(default_factory=lambda: ["windows", "linux", "macos"])
    host_needs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)

    def add_op(self, op: Op) -> None:
        if op.id.split(".")[0] in RESERVED_GROUPS:
            op.id = "x_" + op.id                      # service.*, jobs.*, api.* and project.* are the hub's own
        base, n = op.id, 2
        while any(o.id == op.id for o in self.ops):
            op.id = f"{base}_{n}"
            n += 1
        self.ops.append(op)

    def need(self, tool: str, url: str, min_: str = "") -> None:
        if not any(r["tool"] == tool for r in self.requires):
            self.requires.append({"tool": tool, **({"min": min_} if min_ else {}), "url": url})


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _toml(p: Path) -> dict:
    try:
        return tomllib.loads(_read(p))
    except tomllib.TOMLDecodeError:
        return {}


def _json(p: Path) -> dict:
    try:
        d = json.loads(_read(p))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _rel(root: Path, p: Path) -> str:
    r = p.relative_to(root).as_posix()
    return "." if r in ("", ".") else r


def _units(root: Path, max_depth: int = 3) -> list[tuple[str, Path]]:
    """(kind, folder) for every buildable part of the project, outermost first."""
    found: list[tuple[str, Path]] = []
    markers = {"Cargo.toml": "rust", "package.json": "node", "pyproject.toml": "python", "setup.py": "python",
               "requirements.txt": "python", "go.mod": "go", "wrangler.toml": "worker", "wrangler.jsonc": "worker",
               "build.gradle.kts": "gradle", "build.gradle": "gradle", "flake.nix": "nix", "Makefile": "make",
               "docker-compose.yml": "compose", "compose.yaml": "compose", "mix.exs": "elixir"}

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        names = {e.name for e in entries}
        for m, kind in markers.items():
            if m in names:
                found.append((kind, d))
        if any(e.suffix in (".psm1", ".psd1") for e in entries if e.is_file()):
            found.append(("powershell", d))
        if "boot.py" in names and "main.py" in names:
            found.append(("micropython", d))
        if depth >= max_depth:
            return
        for e in entries:
            if e.is_dir() and e.name not in rt.SKIP_DIRS and not e.name.startswith(".") and e.name not in (
                    "artifacts", "examples", "example", "docs", "tmp", "temp", "logs", "data", "vault", "models",
                    "node_modules", "assets", "static", "third_party", "external", "benchmarks", "samples"):
                walk(e, depth + 1)
    walk(root, 0)
    return found


# ---- Rust -------------------------------------------------------------------------------------------------------

def _cargo_packages(ws: Path) -> list[tuple[str, Path, dict]]:
    """(package name, folder, parsed Cargo.toml) for a workspace's members (or the single package)."""
    top = _toml(ws / "Cargo.toml")
    out = []
    if "package" in top:
        out.append((top["package"].get("name", ws.name), ws, top))
    for pat in (top.get("workspace") or {}).get("members") or []:
        pat = str(pat).strip().strip("/")
        if pat in ("", "."):
            continue
        for d in sorted(ws.glob(pat)):
            if (d / "Cargo.toml").is_file() and d != ws:
                t = _toml(d / "Cargo.toml")
                if "package" in t:
                    out.append((t["package"].get("name", d.name), d, t))
    return out


def _bins(name: str, d: Path, t: dict) -> list[str]:
    bins = [b.get("name") for b in t.get("bin") or [] if b.get("name")]
    if (d / "src" / "main.rs").is_file() and not any(b == name for b in bins):
        if not t.get("bin") or all(b.get("path") != "src/main.rs" for b in t.get("bin") or []):
            bins.insert(0, name)
    for f in sorted((d / "src" / "bin").glob("*.rs")) if (d / "src" / "bin").is_dir() else []:
        bins.append(f.stem)
    for f in sorted((d / "src" / "bin").glob("*/main.rs")) if (d / "src" / "bin").is_dir() else []:
        bins.append(f.parent.name)
    return list(dict.fromkeys(bins))


_CLAP_ENUM = re.compile(r"#\[derive\([^)]*Subcommand[^)]*\)\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?enum\s+(\w+)\s*\{", re.S)
_VARIANT = re.compile(r"^\s{4}(?:///[^\n]*\n\s*|#\[[^\]]*\]\s*)*([A-Z]\w*)\s*(?:\{|\(|,|$)", re.M)


def _enum_body(text: str, start: int) -> str:
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1]


def _kebab(s: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s).lower()


def _rust_subcommands(d: Path) -> list[tuple[str, str]]:
    """(subcommand, doc comment) of the first clap Subcommand enum in a crate's main files."""
    for f in [d / "src" / "main.rs", d / "src" / "cli.rs", *sorted((d / "src").glob("**/*.rs"))][:60]:
        text = _read(f)
        m = _CLAP_ENUM.search(text)
        if not m:
            continue
        body = _enum_body(text, m.end())
        out: list[tuple[str, str]] = []
        depth, doc, attrs = 0, "", ""
        for line in body.splitlines():
            s = line.strip()
            if depth == 0 and s:
                if s.startswith("///"):
                    doc = (doc + " " + s[3:].strip()).strip()
                elif s.startswith("#["):
                    attrs += s
                elif vm := re.match(r"([A-Z]\w*)\s*(\{|\(|,|$)", s):
                    renamed = re.search(r'(?:name|alias)\s*=\s*"([^"]+)"', attrs)
                    if "skip" not in attrs:
                        out.append((renamed.group(1) if renamed else _kebab(vm.group(1)), doc))
                    doc, attrs = "", ""
            depth = max(depth + s.count("{") - s.count("}"), 0)
        if out:
            return out
    return []


def _port_in(text: str) -> int | None:
    for rx in (r"(?:127\.0\.0\.1|0\.0\.0\.0|localhost):(\d{4,5})", r"(?:PORT|port)\D{0,25}?(\d{4,5})\b",
               r"\.listen\(\s*(\d{4,5})", r"port\s*=\s*(\d{4,5})"):
        m = re.search(rx, text)
        if m and 1024 < int(m.group(1)) < 65535:
            return int(m.group(1))
    return None


def detect_rust(plan: Plan, ws: Path) -> None:
    pkgs = _cargo_packages(ws)
    if not pkgs:
        return
    plan.need("cargo", "https://rustup.rs", "1.75")
    rel = _rel(plan.root, ws)
    to_build, tauri = [], []
    for name, d, t in pkgs:
        deps = {**(t.get("dependencies") or {}), **(t.get("build-dependencies") or {})}
        bins = _bins(name, d, t)
        if not bins:
            continue
        if "tauri" in deps or (d / "tauri.conf.json").is_file():
            tauri.append((name, d, bins))
            continue
        to_build.append(name)
        text = "\n".join(_read(f) for f in list((d / "src").glob("*.rs"))[:30])
        subs = _rust_subcommands(d)
        for b in bins:
            exe = f"{{target}}/release/{b}{{exe}}"
            plan.outputs.append(exe)
            low = b.lower()
            if "tui" in low and not plan.tui:
                plan.tui = [exe]
            elif re.search(r"\bgui\b|desktop|egui|iced", low) and not plan.gui:
                plan.gui = [exe]
            group = re.sub(r"[^a-z0-9_]+", "_", low)
            if subs and b == bins[0]:
                for sub, doc in subs:
                    plan.add_op(Op(id=f"{group}.{sub.replace('-', '_')}", kind="cmd", argv=[exe, sub], extra_args=True,
                                   summary=doc or f"`{b} {sub}`", timeout_s=600))
            else:
                plan.add_op(Op(id=f"{group}.run", kind="cmd", argv=[exe], extra_args=True, timeout_s=600,
                               summary=f"Run `{b}` with the given arguments"))
            if plan.service is None and ("axum" in deps or "actix-web" in deps or "warp" in deps or "rocket" in deps) \
                    and re.search(r"daemon|serve|server|gateway|api|hub|web|node|agent", low):
                port = _port_in(text) or stable_port(plan.id)
                start = [exe] + (["serve"] if any(s == "serve" for s, _ in subs) else
                                 ["daemon"] if any(s == "daemon" for s, _ in subs) else [])
                plan.service = Service(id=plan.id, name=plan.name, base_url=f"http://127.0.0.1:{port}", start=start,
                                       env={"PORT": str(port)})
                plan.notes.append(f"{b}: took it for the server on port {port}; check service.start in abp-ops.toml")
    if to_build:
        cmd = ["cargo", "build", "--release"] + [x for n in to_build for x in ("-p", n)]
        if rel != ".":
            cmd += ["--manifest-path", f"{rel}/Cargo.toml"]
        plan.build.append(cmd)
        plan.build_env["CARGO_TARGET_DIR"] = "{target}"
    for name, d, bins in tauri:
        conf = _json(d / "tauri.conf.json") or _json(d / "src-tauri" / "tauri.conf.json")
        product = (conf.get("productName") or (conf.get("package") or {}).get("productName") or bins[0])
        ui_dir = d.parent if d.name == "src-tauri" else d
        if (ui_dir / "package.json").is_file():
            plan.add_op(Op(id="app.dev", kind="cmd", argv=["npm", "run", "tauri", "dev"], cwd=_rel(plan.root, ui_dir),
                           background=True, timeout_s=86400, summary=f"Run the {product} desktop app in development mode"))
            plan.add_op(Op(id="app.build", kind="cmd", argv=["npm", "run", "tauri", "build"], cwd=_rel(plan.root, ui_dir),
                           background=True, timeout_s=7200, summary=f"Build the {product} desktop app (installer)"))
        exe = f"{{target}}/release/{bins[0]}{{exe}}"
        if not plan.gui:
            plan.gui = [exe]
            plan.notes.append(f"{product}: a Tauri app; its window is {exe} once built with app.build")
    rs = rt.rs_routes(ws)
    if rs and plan.service:
        for method, path in sorted(rs):
            plan.add_op(Op(id=rt.op_id(method, path), kind="http", method=method, path=path, summary=f"{method} {path}"))


# ---- Node ---------------------------------------------------------------------------------------------------------

_LONG_SCRIPTS = re.compile(r"^(dev|start|serve|watch|preview)(:|$)")
_SKIP_SCRIPTS = {"preinstall", "postinstall", "prepare", "prepublishOnly", "prepack", "postpack", "install"}


def _pm(d: Path, root: Path) -> str:
    for p in (d, *d.parents):
        if (p / "pnpm-lock.yaml").is_file():
            return "pnpm"
        if (p / "yarn.lock").is_file():
            return "yarn"
        if (p / "bun.lockb").is_file():
            return "bun"
        if p == root:
            break
    return "npm"


def detect_node(plan: Plan, d: Path, workspace_member: bool, many: bool = False) -> None:
    pkg = _json(d / "package.json")
    if not pkg:
        return
    if many:
        # one of many small packages (a template library): an install op, no build step, no script ops
        rel = _rel(plan.root, d)
        plan.need("node", "https://nodejs.org", "20")
        plan.add_op(Op(id=f"install.{re.sub(r'[^a-z0-9_]+', '_', d.name.lower())}", kind="cmd", argv=["npm", "install"],
                       cwd=rel, timeout_s=1800, summary=f"Install {rel}'s dependencies"))
        return
    plan.need("node", "https://nodejs.org", "20")
    rel = _rel(plan.root, d)
    pm = _pm(d, plan.root)
    if pm != "npm":
        plan.need(pm, {"pnpm": "https://pnpm.io/installation", "yarn": "https://yarnpkg.com",
                       "bun": "https://bun.sh"}[pm])
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    scripts = pkg.get("scripts") or {}
    if not workspace_member:
        install = [pm, "ci"] if pm == "npm" and (d / "package-lock.json").is_file() else [pm, "install"]
        if rel != ".":
            install += ["--prefix", rel] if pm == "npm" else ["--dir", rel] if pm == "pnpm" else ["--cwd", rel]
        plan.build.append(install)
        if "build" in scripts and "tauri" not in scripts["build"] and not re.search(r"electron-builder|pkg\b",
                                                                                     scripts["build"]):
            plan.build.append([pm, "run", "build"] + (["--prefix", rel] if pm == "npm" and rel != "." else
                                                      ["--dir", rel] if pm == "pnpm" and rel != "." else []))
    label = re.sub(r"[^a-z0-9_]+", "_", (pkg.get("name") or d.name).split("/")[-1].lower()).strip("_") or "npm"
    group = "npm" if rel == "." else label
    for s, body in scripts.items():
        if s in _SKIP_SCRIPTS or s.startswith(("pre", "post")) and s[3:] in scripts:
            continue
        long = bool(_LONG_SCRIPTS.match(s)) or "--watch" in body
        plan.add_op(Op(id=f"{group}.{re.sub(r'[^A-Za-z0-9_]+', '_', s).strip('_').lower() or 'x'}", kind="cmd",
                       argv=[pm, "run", s], cwd=rel, background=long or s.startswith(("build", "test", "e2e")),
                       timeout_s=86400 if long else 3600, extra_args=True, summary=f"`{pm} run {s}`: {body[:120]}"))
    bins = pkg.get("bin")
    if isinstance(bins, str):
        bins = {pkg.get("name", d.name).split("/")[-1]: bins}
    for b, path in (bins or {}).items():
        if "@modelcontextprotocol/sdk" in deps and not plan.mcp_native and re.search(r"mcp", b + path, re.I):
            plan.mcp_native = ["node", f"{{repo}}/{rel}/{path}".replace("/./", "/")]
    if "@modelcontextprotocol/sdk" in deps and not plan.mcp_native:
        for cand in ("dist/index.js", "build/index.js", "dist/server.js", "index.js", "server.js"):
            if (d / cand).is_file() or (d / "src" / Path(cand).with_suffix(".ts").name).is_file():
                plan.mcp_native = ["node", f"{{repo}}/{rel}/{cand}".replace("/./", "/")]
                break
    server_fw = next((f for f in ("express", "fastify", "hono", "koa", "@nestjs/core", "next", "@sveltejs/kit", "nuxt",
                                  "vite", "astro", "@remix-run/node") if f in deps), None)
    if server_fw and plan.service is None:
        run = "start" if "start" in scripts and server_fw in ("express", "fastify", "hono", "koa", "@nestjs/core") else \
            "dev" if "dev" in scripts else "start" if "start" in scripts else ""
        if run:
            src = "\n".join(_read(f) for f in rt.files(d, (".js", ".ts", ".mjs"), limit=200)[:200])
            uses_env = "process.env.PORT" in src or server_fw in ("next", "vite", "astro", "@sveltejs/kit", "nuxt")
            port = stable_port(plan.id) if uses_env else (_port_in(src) or stable_port(plan.id))
            start = [pm, "run", run]
            if server_fw in ("vite", "@sveltejs/kit", "astro") and run == "dev":
                start += ["--", "--port", str(port), "--strictPort"]
            elif server_fw == "next" and run == "dev":
                start += ["--", "-p", str(port)]
            web = "/" if server_fw in ("next", "vite", "@sveltejs/kit", "nuxt", "astro", "@remix-run/node") else ""
            plan.service = Service(id=plan.id, name=plan.name, base_url=f"http://127.0.0.1:{port}", start=start,
                                   cwd=rel, env={"PORT": str(port), "HOST": "127.0.0.1"}, web=web, ready_timeout_s=120)
            if not uses_env:
                plan.notes.append(f"{rel}: the server does not read $PORT; took port {port} from its source")


# ---- Python ---------------------------------------------------------------------------------------------------

_ARGPARSE_SUB = re.compile(r"""add_parser\(\s*['"]([\w-]+)['"](?:[^)]*help\s*=\s*['"]([^'"]*)['"])?""")
_CLICK_CMD = re.compile(r"""@(\w+)\.command\((?:\s*(?:name\s*=\s*)?['"]([\w-]+)['"])?[^)]*\)\s*(?:@[^\n]*\n\s*)*(?:async\s+)?def\s+(\w+)\(""")
_FASTAPI = re.compile(r"^(\w+)\s*(?::\s*\w+\s*)?=\s*(?:fastapi\.)?FastAPI\(", re.M)
_FLASK = re.compile(r"^(\w+)\s*=\s*(?:flask\.)?Flask\(", re.M)
_FASTMCP = re.compile(r"FastMCP\(|mcp\.server|from mcp\.server")


def _pymod(root: Path, f: Path) -> tuple[str, Path]:
    """The dotted module name of a file and the folder to import it from (the first parent without __init__.py,
    or its src/ layout parent)."""
    parts = [f.stem]
    p = f.parent
    while (p / "__init__.py").is_file() and p != root:
        parts.insert(0, p.name)
        p = p.parent
    return ".".join(parts), p


def detect_python(plan: Plan, d: Path, seen_venv: list[bool]) -> None:
    plan.need("python", "https://www.python.org/downloads/", "3.10")
    rel = _rel(plan.root, d)
    py = _toml(d / "pyproject.toml")
    if not seen_venv[0]:
        plan.build.append(["python", "-m", "venv", ".venv"])
        seen_venv[0] = True
    if (d / "pyproject.toml").is_file() and ((py.get("project") or {}).get("name") or (py.get("tool") or {}).get("poetry")) \
            or (d / "setup.py").is_file():
        plan.build.append(["{venv_python}", "-m", "pip", "install", "-q", "-e", rel])
    elif (d / "requirements.txt").is_file():
        plan.build.append(["{venv_python}", "-m", "pip", "install", "-q", "-r", f"{rel}/requirements.txt".lstrip("./")
                           if rel != "." else "requirements.txt"])
    scripts = (py.get("project") or {}).get("scripts") or ((py.get("tool") or {}).get("poetry") or {}).get("scripts") or {}
    for name, target in scripts.items():
        mod = str(target).split(":")[0]
        cands = [d / Path(*mod.split(".")).with_suffix(".py"), d / "src" / Path(*mod.split(".")).with_suffix(".py"),
                 d / Path(*mod.split(".")) / "__main__.py", d / "src" / Path(*mod.split(".")) / "__main__.py"] if mod else []
        mfile = next((c for c in cands if c.is_file()), None)
        text = _read(mfile) if mfile else ""
        subs = [(m.group(1), m.group(2) or "") for m in _ARGPARSE_SUB.finditer(text)]
        subs += [((m.group(2) or m.group(3).replace("_", "-")), "") for m in _CLICK_CMD.finditer(text)]
        exe = f"{{venv_bin}}/{name}{{exe}}"
        group = re.sub(r"[^a-z0-9_]+", "_", name.lower())
        if subs:
            for s, h in dict(subs).items():
                plan.add_op(Op(id=f"{group}.{s.replace('-', '_')}", kind="cmd", argv=[exe, s], extra_args=True,
                               timeout_s=900, summary=h or f"`{name} {s}`"))
        else:
            plan.add_op(Op(id=f"{group}.run", kind="cmd", argv=[exe], extra_args=True, timeout_s=900,
                           summary=f"Run `{name}` with the given arguments"))
    pyfiles = rt.files(d, (".py",), limit=1500)
    for f in pyfiles:
        text = _read(f)
        if plan.service is None and (m := _FASTAPI.search(text)):
            mod, imp = _pymod(d, f)
            port = stable_port(plan.id)
            cwd = _rel(plan.root, imp)
            plan.service = Service(id=plan.id, name=plan.name, base_url=f"http://127.0.0.1:{port}",
                                   start=["{python}", "-m", "uvicorn", f"{mod}:{m.group(1)}", "--host", "127.0.0.1",
                                          "--port", str(port)], cwd=cwd,
                                   ready_timeout_s=90)
            if "uvicorn" not in _read(d / "requirements.txt") + json.dumps(py):
                plan.build.append(["{venv_python}", "-m", "pip", "install", "-q", "uvicorn"])
        elif plan.service is None and (m := _FLASK.search(text)):
            mod, imp = _pymod(d, f)
            port = stable_port(plan.id)
            plan.service = Service(id=plan.id, name=plan.name, base_url=f"http://127.0.0.1:{port}",
                                   start=["{python}", "-m", "flask", "--app", f"{mod}:{m.group(1)}", "run", "--host",
                                          "127.0.0.1", "--port", str(port)], cwd=_rel(plan.root, imp), web="/")
        if not plan.mcp_native and _FASTMCP.search(text) and ("mcp.run(" in text or "__main__" in text):
            mod, imp = _pymod(d, f)
            plan.mcp_native = ["{venv_python}", f"{{repo}}/{_rel(plan.root, f)}"] if imp == f.parent else \
                ["{venv_python}", "-m", mod]
    # top-level scripts with a __main__ guard (not tests, not setup)
    for f in sorted(p for p in d.glob("*.py") if p.name not in ("setup.py", "conftest.py", "__init__.py", "manage.py")):
        text = _read(f)
        if "__name__" in text and "__main__" in text and not _FASTMCP.search(text):
            subs = [(m.group(1), m.group(2) or "") for m in _ARGPARSE_SUB.finditer(text)]
            group = re.sub(r"[^a-z0-9_]+", "_", f.stem.lower())
            if subs:
                for s, h in dict(subs).items():
                    plan.add_op(Op(id=f"{group}.{s.replace('-', '_')}", kind="cmd",
                                   argv=["{python}", f"{rel}/{f.name}" if rel != "." else f.name, s], extra_args=True,
                                   timeout_s=900, summary=h or f"`{f.name} {s}`"))
            else:
                plan.add_op(Op(id=f"script.{group}", kind="cmd", argv=["{python}", f"{rel}/{f.name}" if rel != "." else
                                                                         f.name], extra_args=True, timeout_s=900,
                               summary=_docline(text) or f"Run {f.name}"))
    if (d / "manage.py").is_file():
        plan.add_op(Op(id="django.manage", kind="cmd", argv=["{python}", f"{rel}/manage.py".lstrip("./")],
                       extra_args=True, summary="Django's manage.py with the given arguments"))


def _docline(text: str) -> str:
    m = re.match(r'\s*(?:#![^\n]*\n\s*)?(?:#[^\n]*\n\s*)*[rbuRBU]?("""|\'\'\')\s*([^\n]+)', text)
    return m.group(2).strip()[:160] if m else ""


# ---- PowerShell, Go, workers, Gradle, Nix, make, compose ---------------------------------------------------------

_PS_FUNC = re.compile(r"^\s*function\s+([A-Za-z]+-[A-Za-z0-9]+)\s*\{?", re.M | re.I)
_PS_PARAM = re.compile(r"(\[Parameter\([^)]*\)\]\s*)?\[(\w+(?:\[\])?)\]\s*\$(\w+)", re.I)


def _ps_params(text: str, fname: str) -> dict[str, dict]:
    m = re.search(rf"function\s+{re.escape(fname)}\b[^{{]*\{{(.*?)\n\}}", text, re.S | re.I)
    body = m.group(1) if m else ""
    pm = re.search(r"param\s*\((.*?)\)\s*(?:\n|$)(?=\s*(?:begin|process|end|\S))", body, re.S | re.I)
    if not pm:
        return {}
    inputs = {}
    for a in _PS_PARAM.finditer(pm.group(1)):
        attr, typ, name = a.group(1) or "", a.group(2).lower(), a.group(3)
        t = {"switch": "boolean", "bool": "boolean", "int": "integer", "int32": "integer", "int64": "integer",
             "double": "number"}.get(typ, "array" if typ.endswith("[]") else "string")
        inputs[name] = {"type": t, **({"required": True} if "mandatory" in attr.lower() and "=$false" not in
                                      attr.replace(" ", "").lower() else {})}
    return inputs


def detect_powershell(plan: Plan, d: Path) -> None:
    psd1 = sorted(d.glob("*.psd1"))
    manifests: list[tuple[Path, list[str], str]] = []      # (file to import, exported names or [] = all, source text)
    alltext = "\n".join(_read(f) for f in sorted(d.glob("*.psm1")))
    if psd1:
        text = _read(psd1[0])
        m = re.search(r"FunctionsToExport\s*=\s*@?\(?([^)\n]*)", text, re.I)
        names = re.findall(r"['\"]([\w-]+)['\"]", m.group(1)) if m else []
        manifests.append((psd1[0], [n for n in names if n != "*"], alltext))
    else:
        for psm in sorted(d.glob("*.psm1")):
            text = _read(psm)
            exported = re.findall(r"Export-ModuleMember\s+-Function\s+([^\n]+)", text, re.I)
            names = [n.strip(" '\"@(),") for e in exported for n in re.split(r"[,\s]+", e) if n.strip(" '\"@(),")]
            manifests.append((psm, [n for n in names if n != "*"], text))
    for psm, names, text in manifests:
        funcs = [f for f in dict.fromkeys(_PS_FUNC.findall(text)) if not names or f in names]
        rel = _rel(plan.root, psm)
        for f in funcs:
            inputs = _ps_params(text, f)
            script = (f"$ErrorActionPreference='Stop'; Import-Module (Join-Path '{{project}}' '{rel}') -Force; "
                      f"$h=@{{}}; if ($env:ABP_OP_ARGS) {{ ($env:ABP_OP_ARGS | ConvertFrom-Json).PSObject.Properties | "
                      f"ForEach-Object {{ $h[$_.Name] = $_.Value }} }}; {f} @h | ConvertTo-Json -Depth 6")
            verb = f.split("-")[0].lower()
            plan.add_op(Op(id=f"ps.{f.replace('-', '_').lower()}", kind="cmd",
                           argv=["{pwsh}", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                                 script], inputs=inputs,
                           mutating_flag=verb not in ("get", "test", "find", "measure", "show", "read", "search",
                                                      "resolve", "compare", "select"),
                           summary=f"{f} (PowerShell, {psm.name})"))
    plan.need("powershell", "https://aka.ms/powershell")


def detect_go(plan: Plan, d: Path) -> None:
    plan.need("go", "https://go.dev/dl/", "1.21")
    rel = _rel(plan.root, d)
    plan.build.append(["go", "build", "./..."] if rel == "." else ["go", "-C", rel, "build", "./..."])
    for cmd in sorted((d / "cmd").glob("*/")) if (d / "cmd").is_dir() else []:
        plan.add_op(Op(id=f"go.{cmd.name.replace('-', '_')}", kind="cmd", argv=["go", "run", f"./cmd/{cmd.name}"],
                       cwd=rel, extra_args=True, summary=f"`go run ./cmd/{cmd.name}`"))


def detect_worker(plan: Plan, d: Path) -> None:
    rel = _rel(plan.root, d)
    conf = _toml(d / "wrangler.toml") if (d / "wrangler.toml").is_file() else {}
    name = re.sub(r"[^a-z0-9_]+", "_", (conf.get("name") or d.name).lower())
    plan.add_op(Op(id=f"worker.{name}_dev", kind="cmd", argv=["npx", "wrangler", "dev", "--port", "{port}"], cwd=rel,
                   background=True, timeout_s=86400, inputs={"port": {"type": "string", "default": "8787"}},
                   mutating_flag=False, summary=f"Run the {name} worker locally (wrangler dev)"))
    plan.add_op(Op(id=f"worker.{name}_deploy", kind="cmd", argv=["npx", "wrangler", "deploy"], cwd=rel, timeout_s=900,
                   destructive_flag=True, summary=f"Deploy the {name} worker to Cloudflare (public; needs a login)"))


def detect_gradle(plan: Plan, d: Path) -> None:
    rel = _rel(plan.root, d)
    gw = "{project}/" + (f"{rel}/" if rel != "." else "") + "gradlew{bat}"
    wrapper = (d / "gradlew").is_file() or (d / "gradlew.bat").is_file()
    exe = gw if wrapper else "gradle"
    group = "gradle" if rel == "." else "gradle_" + re.sub(r"[^a-z0-9_]+", "_", d.name.lower())
    text = _read(d / "build.gradle.kts") + _read(d / "build.gradle") + _read(d / "settings.gradle.kts") + \
        _read(d / "settings.gradle")
    android = "com.android" in text or (d / "app" / "src" / "main" / "AndroidManifest.xml").is_file()
    tasks = ["assembleDebug", "test", "lint", "installDebug"] if android else ["build", "test", "run"]
    for t in tasks:
        plan.add_op(Op(id=f"{group}.{t.lower()}", kind="cmd", argv=[exe, t], cwd=rel, background=True, timeout_s=7200,
                       extra_args=True, summary=f"Gradle {t}" + (" (Android)" if android else "")))
    if android and "android-sdk" not in plan.host_needs:
        plan.host_needs.append("android-sdk")
    plan.need("java", "https://adoptium.net", "17")


def detect_nix(plan: Plan, d: Path) -> None:
    rel = _rel(plan.root, d)
    for t, s in (("build", "Build the flake's default package"), ("check", "Run the flake's checks"),
                 ("show", "What the flake provides")):
        plan.add_op(Op(id=f"nix.{t}" if rel == "." else f"nix_{d.name.lower()}.{t}", kind="cmd",
                       argv=["nix", "--extra-experimental-features", "nix-command flakes", "flake" if t != "build"
                             else "build", *([t] if t != "build" else []), f"./{rel}" if rel != "." else "."],
                       background=t != "show", timeout_s=7200, mutating_flag=t == "build", summary=s + " (Linux/macOS)"))


def detect_make(plan: Plan, d: Path) -> None:
    rel = _rel(plan.root, d)
    text = _read(d / "Makefile")
    targets = [t for t in re.findall(r"^([A-Za-z][\w.-]*)\s*:(?!=)", text, re.M) if not t.startswith(".")]
    for t in list(dict.fromkeys(targets))[:25]:
        plan.add_op(Op(id=f"make.{re.sub(r'[^a-z0-9_]+', '_', t.lower())}" if rel == "." else
                       f"make_{d.name.lower()}.{re.sub(r'[^a-z0-9_]+', '_', t.lower())}", kind="cmd",
                       argv=["make", t], cwd=rel, background=True, timeout_s=3600, summary=f"`make {t}`"))


def detect_compose(plan: Plan, d: Path) -> None:
    rel = _rel(plan.root, d)
    f = "docker-compose.yml" if (d / "docker-compose.yml").is_file() else "compose.yaml"
    g = "compose" if rel == "." else "compose_" + re.sub(r"[^a-z0-9_]+", "_", d.name.lower())
    for t, argv, s, bg in (("up", ["up", "-d", "--build"], "Start its containers (docker compose up -d)", True),
                           ("down", ["down"], "Stop and remove its containers", False),
                           ("ps", ["ps", "--format", "json"], "Its containers and their state", False),
                           ("logs", ["logs", "--tail", "200"], "Its containers' recent logs", False)):
        plan.add_op(Op(id=f"{g}.{t}", kind="cmd", argv=["docker", "compose", "-f", f, *argv], cwd=rel, background=bg,
                       timeout_s=3600 if bg else 120, mutating_flag=t in ("up", "down"), summary=s))


def detect_elixir(plan: Plan, d: Path) -> None:
    rel = _rel(plan.root, d)
    plan.need("mix", "https://elixir-lang.org/install.html")
    plan.add_op(Op(id="mix.deps_get", kind="cmd", argv=["mix", "deps.get"], cwd=rel, timeout_s=1800,
                   summary="Fetch its Elixir dependencies"))
    plan.add_op(Op(id="mix.test", kind="cmd", argv=["mix", "test"], cwd=rel, background=True, timeout_s=3600,
                   mutating_flag=False, summary="Run its Elixir tests"))
    if "phoenix" in _read(d / "mix.exs") and plan.service is None:
        port = stable_port(plan.id, 1)
        plan.service = Service(id=plan.id, name=plan.name, base_url=f"http://127.0.0.1:{port}",
                               start=["mix", "phx.server"], cwd=rel, env={"PORT": str(port)}, web="/", ready_timeout_s=180)


def detect_micropython(plan: Plan, d: Path) -> None:
    rel = _rel(plan.root, d)
    name = re.sub(r"[^a-z0-9_]+", "_", d.name.lower())
    plan.need("mpremote", "https://docs.micropython.org/en/latest/reference/mpremote.html")
    port = {"port": {"type": "string", "default": "auto", "description": "serial port, or auto"}}
    plan.add_op(Op(id=f"firmware.{name}_deploy", kind="cmd", argv=["mpremote", "connect", "{port}", "fs", "cp", "-r", ".",
                                                                    ":", "+", "reset"], cwd=rel, timeout_s=600,
                   inputs=dict(port), summary=f"Copy the {d.name} firmware to a connected MicroPython board, then reset it"))
    plan.add_op(Op(id=f"firmware.{name}_run", kind="cmd", argv=["mpremote", "connect", "{port}", "run", "main.py"], cwd=rel,
                   timeout_s=120, inputs=dict(port), mutating_flag=False,
                   summary=f"Run {d.name}'s main.py on a connected board without copying it"))
    if (d / "tests").is_dir():
        plan.add_op(Op(id=f"firmware.{name}_test", kind="cmd", argv=["{python}", "-m", "pytest", "-q", "tests"], cwd=rel,
                       timeout_s=900, mutating_flag=False, summary=f"Run {d.name}'s tests on this computer"))
    if not any(o.id == "device.list" for o in plan.ops):
        plan.add_op(Op(id="device.list", kind="cmd", argv=["mpremote", "devs"], timeout_s=30, mutating_flag=False,
                       summary="Connected MicroPython boards"))


def detect_worker_library(plan: Plan, dirs: list[Path]) -> None:
    """Many workers (a template library): four ops that take the worker's folder, instead of three per worker."""
    names = [_rel(plan.root, d) for d in dirs]
    pick = {"worker": {"type": "string", "required": True, "enum": names, "description": "the worker's folder"}}
    plan.add_op(Op(id="worker.list", kind="cmd", argv=["{python}", "-c", "import json,sys; print(json.dumps(sys.argv[1:]))",
                                                        *names[:400]], mutating_flag=False, timeout_s=30,
                   summary=f"The {len(names)} workers in this library"))
    plan.add_op(Op(id="worker.install", kind="cmd", argv=["npm", "install"], cwd="{worker}", inputs=dict(pick),
                   timeout_s=1800, summary="Install one worker's dependencies"))
    plan.add_op(Op(id="worker.dev", kind="cmd", argv=["npx", "wrangler", "dev", "--port", "{port}"], cwd="{worker}",
                   inputs={**pick, "port": {"type": "string", "default": "8787"}}, background=True, timeout_s=86400,
                   mutating_flag=False, summary="Run one worker locally (wrangler dev)"))
    plan.add_op(Op(id="worker.deploy", kind="cmd", argv=["npx", "wrangler", "deploy"], cwd="{worker}", inputs=dict(pick),
                   timeout_s=900, destructive_flag=True, summary="Deploy one worker to Cloudflare (public; needs a login)"))


# ---- the whole project --------------------------------------------------------------------------------------------

def _readme(root: Path) -> tuple[str, str]:
    f = next((root / n for n in ("README.md", "readme.md", "Readme.md", "README.txt", "README") if (root / n).is_file()),
             None)
    if f is None:
        subs = sorted(p for p in root.glob("*/README.md")
                      if not p.parent.name.startswith(".") and p.parent.name not in rt.SKIP_DIRS)
        f = subs[0] if subs else None
    if f is None:
        return "", ""
    text = _read(f)
    title = ""
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#") and not title:
            title = re.sub(r"[*_`]|[^\w\s&.,:()'/-]", "", s.lstrip("#")).strip()
            continue
        s = re.sub(r"\*\*|__|`", "", s)
        # the first real sentence: not a badge, table, list item, "Status: ..." line or a lead-in ending with ":"
        if s and len(s) > 25 and not s.startswith(("![", "[!", "<", "---", "```", "|", ">", "#", "[![", "- ", "* ", "+ ")) \
                and not re.match(r"^(\d+\.|[A-Za-z ]{1,20}:)\s", s) and not s.endswith(":"):
            return title, re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)[:300]
    return title, ""


def detect(root: str | Path, mid: str = "", name: str = "") -> Plan:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a folder")
    title, desc = _readme(root)
    plan = Plan(root=root, id=mid or slug(root.name), name=name or root.name, description=desc or title)
    blob = f"{root.name} {title} {desc}".lower()
    plan.area = next((a for a, rx in AREAS if re.search(rx, blob)), "tools")
    units = _units(root)
    plan.units = [f"{k}:{_rel(root, d)}" for k, d in units]
    kinds = [k for k, _ in units]
    plan.stacks = list(dict.fromkeys(kinds))
    seen_venv = [False]
    n_node = kinds.count("node")
    # Python without a marker file (a folder of scripts, or a package with no requirements.txt yet)
    if "python" not in kinds and "micropython" not in kinds:
        for cand in (root, *sorted(x for x in root.iterdir() if x.is_dir() and x.name not in rt.SKIP_DIRS
                                   and not x.name.startswith("."))):
            if sum(1 for _ in cand.glob("*.py")) >= 1 and (cand == root or (cand / "__init__.py").is_file()
                                                           or any(cand.glob("*/__init__.py")) or len(list(cand.glob("*.py"))) > 2):
                units.append(("python", root))
                kinds.append("python")
                plan.stacks.append("python")
                break
    rust_ws: list[Path] = []
    node_roots: list[Path] = []
    for kind, d in units:
        try:
            if kind == "rust":
                if any(w in d.parents for w in rust_ws):
                    continue            # a member of a workspace already handled
                t = _toml(d / "Cargo.toml")
                if "workspace" not in t and any((p / "Cargo.toml").is_file() and "workspace" in _toml(p / "Cargo.toml")
                                                for p in d.parents if root in p.parents or p == root):
                    continue
                rust_ws.append(d)
                detect_rust(plan, d)
            elif kind == "node":
                member = any(n in d.parents and (_json(n / "package.json").get("workspaces")
                                                 or (n / "pnpm-workspace.yaml").is_file()) for n in node_roots)
                if (d / "src-tauri").is_dir() and not member:
                    pass
                node_roots.append(d)
                if kinds.count("worker") > 12 and ("worker", d) in units:
                    continue
                detect_node(plan, d, member, many=n_node > 12 and d != root)
            elif kind == "python":
                if any(u == ("python", d) for u in units[:units.index((kind, d))]) or ("micropython", d) in units:
                    continue
                detect_python(plan, d, seen_venv)
            elif kind == "go":
                detect_go(plan, d)
            elif kind == "worker":
                if kinds.count("worker") > 12:
                    continue                                   # a template library: handled once, below
                detect_worker(plan, d)
            elif kind == "micropython":
                detect_micropython(plan, d)
            elif kind == "gradle":
                if any(g in d.parents for k2, g in units if k2 == "gradle" and g != d):
                    continue
                detect_gradle(plan, d)
            elif kind == "nix":
                detect_nix(plan, d)
            elif kind == "make":
                detect_make(plan, d)
            elif kind == "compose":
                detect_compose(plan, d)
            elif kind == "powershell":
                detect_powershell(plan, d)
            elif kind == "elixir":
                detect_elixir(plan, d)
        except Exception as e:  # noqa: BLE001 - one odd sub-project must not stop the rest
            plan.notes.append(f"{kind}:{_rel(root, d)}: could not read it ({type(e).__name__}: {e})")
    if kinds.count("worker") > 12:
        detect_worker_library(plan, [d for k, d in units if k == "worker"])
    # loose PowerShell scripts at the top level (a folder of .ps1 tools)
    ps1 = sorted(root.glob("*.ps1")) + sorted(root.glob("*/*.ps1"))[:40]
    for f in ps1[:60]:
        if any(p.name in rt.SKIP_DIRS for p in f.parents):
            continue
        plan.add_op(Op(id=f"ps1.{re.sub(r'[^a-z0-9_]+', '_', f.stem.lower())}", kind="cmd",
                       argv=["{pwsh}", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f"{{project}}/{_rel(root, f)}"],
                       extra_args=True, timeout_s=900, summary=f"Run {_rel(root, f)} (PowerShell)"))
        if "powershell" not in plan.stacks:
            plan.stacks.append("powershell")
            plan.need("powershell", "https://aka.ms/powershell")
    # HTTP routes of the project's server
    if plan.service and plan.service.base_url:
        have = {(o.method, o.path) for o in plan.ops if o.kind == "http"}
        src = root / plan.service.cwd if plan.service.cwd not in ("", ".") else root
        for method, path in rt.extract(src if src.is_dir() else root):
            if (method, path) not in have:
                plan.add_op(Op(id=rt.op_id(method, path), kind="http", method=method, path=path,
                               summary=f"{method} {path}"))
        paths = {o.path for o in plan.ops if o.kind == "http"}
        if "/v1/chat/completions" in paths or "/chat/completions" in paths:
            plan.service.openai = "/v1" if "/v1/chat/completions" in paths else ""
        if not plan.service.health:
            for h in ("/health", "/healthz", "/api/health", "/v1/health", "/status", "/api/status"):
                if h in paths:
                    plan.service.health = h
                    break
        if not plan.service.health and "uvicorn" in plan.service.start:
            plan.service.health = "/docs"                      # FastAPI's own page, when it has no health route
        if not plan.service.web and any((root / p).is_dir() for p in ("static", "templates", "web", "ui", "frontend")):
            plan.service.web = "/"
    elif not plan.service:
        plan.service = Service(id=plan.id, name=plan.name)
    for ci in ("ci/pipeline.py", "scripts/pipeline.py", "scripts/ci.py"):
        if (root / ci).is_file():
            plan.pipeline = ["{venv_python}" if (root / "requirements.txt").is_file() or (root / "pyproject.toml").is_file()
                             else "python", ci]
            break
    for ci in ("scripts/pipeline.mjs", "scripts/ci/pipeline.mjs"):
        if not plan.pipeline and (root / ci).is_file():
            plan.pipeline = ["node", ci]
    if plan.host_needs == [] and any(k == "gradle" for k in kinds) and not any(k in ("rust", "python", "node")
                                                                              for k in kinds):
        plan.host_needs.append("android-sdk")
    if not plan.ops and not plan.mcp_native:
        plan.notes.append("found nothing to run: add ops to abp-ops.toml by hand (see docs/modules/modkit.md)")
    plan.service.description = plan.description
    return plan


def summary(plan: Plan) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for o in plan.ops:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1
    return {"id": plan.id, "name": plan.name, "area": plan.area, "description": plan.description, "stacks": plan.stacks,
            "units": plan.units, "build_steps": len(plan.build), "server": plan.service.base_url if plan.service else "",
            "server_start": plan.service.start if plan.service else [], "web": bool(plan.service and plan.service.web),
            "openai": bool(plan.service and plan.service.openai), "operations": len(plan.ops), "by_kind": kinds,
            "mcp": "native" if plan.mcp_native else "bridge", "gui": bool(plan.gui), "tui": bool(plan.tui),
            "requires": [r["tool"] for r in plan.requires], "notes": plan.notes}
