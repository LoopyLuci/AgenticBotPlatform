"""HTTP routes from source: Express / Fastify / Hono / Koa-style routers, Next.js API routes, FastAPI / Starlette /
Flask / aiohttp, axum / actix-web, Go (net/http, gin, echo, chi) and Cloudflare Workers' pathname checks.

Regex-level on purpose: it has to work on any checkout without installing or running anything. It finds most routes
of a conventional project; an op list is a starting point that abp-ops.toml lets a person correct.
"""
from __future__ import annotations

import re
from pathlib import Path

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", "test", "tests", "__tests__", "public",
             "target", ".venv", "venv", "vendor", "deps", "site-packages", "__pycache__", "out", ".svelte-kit",
             "fixtures", "e2e", "spec", "_build", "bin", "obj"}
_PARAM = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)\??|\{([A-Za-z_][A-Za-z0-9_]*)\}|\[([A-Za-z_][A-Za-z0-9_]*)\]|<(?:\w+:)?([A-Za-z_][A-Za-z0-9_]*)>")

_ROUTE = re.compile(r"""\b([A-Za-z_$][\w$]*)\.(get|post|put|patch|delete)\(\s*(['"`])(/[^'"`]*)\3""")
_USE = re.compile(r"""\b(?:app|router|[A-Za-z_$][\w$]*)\.(?:use|route)\(\s*(['"`])(/[^'"`]*)\1\s*,\s*([^\n;]*)""")
_REQUIRE = re.compile(r"""require\(\s*['"`](\.[^'"`]+)['"`]\s*\)""")
_ASSIGN_REQ = re.compile(r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['"`](\.[^'"`]+)['"`]\s*\)""")
_IMPORT = re.compile(r"""import\s+([A-Za-z_$][\w$]*)\s+from\s+['"`](\.[^'"`]+)['"`]""")
_REGISTER = re.compile(r"""\.register\(\s*([A-Za-z_$][\w$]*|require\(['"`][^'"`]+['"`]\))\s*,\s*\{[^}]*prefix\s*:\s*['"`](/[^'"`]*)['"`]""")
_PY_ROUTE = re.compile(r"""@(\w+)\.(get|post|put|patch|delete)\(\s*(['"])(/[^'"]*)\3""")
_PY_FLASK = re.compile(r"""@\w+\.route\(\s*(['"])(/[^'"]*)\1(?:\s*,\s*methods\s*=\s*(\[[^\]]*\]|\([^)]*\)))?""")
_PY_PREFIX = re.compile(r"""(\w+)\s*=\s*APIRouter\([^)]*prefix\s*=\s*['"](/[^'"]*)['"]""")
_PY_AIOHTTP = re.compile(r"""(?:web\.|\.add_)(get|post|put|patch|delete)\(\s*(['"])(/[^'"]*)\2""")
_RS_AXUM = re.compile(r"""\.route\(\s*"(/[^"]*)"\s*,\s*([^;]*?)\)\s*(?=\.|;|\n\s*\))""", re.S)
_RS_AXUM_M = re.compile(r"\b(get|post|put|patch|delete)(?:_service)?\(")
_RS_ACTIX = re.compile(r"""#\[(get|post|put|patch|delete)\(\s*"(/[^"]*)"\s*""")
_RS_ACTIX_RES = re.compile(r"""web::resource\(\s*"(/[^"]*)"\s*\)((?:\s*\.route\(\s*web::(?:get|post|put|patch|delete)\(\)[^)]*\))+)""")
_GO_STD = re.compile(r"""\.(?:HandleFunc|Handle)\(\s*"((?:(GET|POST|PUT|PATCH|DELETE)\s+)?/[^"]*)"\s*""")
_GO_FW = re.compile(r"""\b\w+\.(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)\(\s*"(/[^"]*)"\s*""")
_CF_PATH = re.compile(r"""pathname\s*(?:===|==|\.startsWith\()\s*['"`](/[^'"`]*)['"`]""")


def files(root: Path, exts: tuple[str, ...], limit: int = 6000) -> list[Path]:
    out: list[Path] = []
    stack = [root]
    while stack and len(out) < limit:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith(".") and not p.is_symlink():
                    stack.append(p)
            elif p.suffix in exts and ".test." not in p.name and ".spec." not in p.name and not p.name.startswith("test_"):
                try:
                    if p.stat().st_size < 1_500_000:
                        out.append(p)
                except OSError:
                    pass
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _resolve(base: Path, rel: str) -> Path | None:
    cand = base.parent / rel
    for c in (cand, cand.with_suffix(".js"), cand.with_suffix(".ts"), cand.with_suffix(".mjs"), cand.with_suffix(".cjs"),
              cand / "index.js", cand / "index.ts"):
        if c.is_file():
            return c.resolve()
    return None


def join(prefix: str, path: str) -> str:
    return "/" + "/".join(x for x in (prefix.strip("/"), path.strip("/")) if x)


_NOT_ROUTERS = {"axios", "http", "https", "fetch", "client", "request", "supertest", "agent", "res", "req", "localStorage",
                "map", "headers", "params", "searchParams", "cache", "store", "db", "Map", "session", "cookies", "ky",
                "got", "api", "$http", "this", "self", "window", "document", "env", "config", "redis", "kv", "state",
                "formData", "url", "query", "settings", "storage", "chrome", "browser", "context", "ctx", "c", "c.req"}


def js_routes(root: Path) -> set[tuple[str, str]]:
    fs = files(root, (".js", ".mjs", ".cjs", ".ts"))
    prefixes: dict[Path, set[str]] = {}
    texts = {f: _read(f) for f in fs}
    for f, text in texts.items():
        names = {m.group(1): m.group(2) for m in _ASSIGN_REQ.finditer(text)}
        names.update({m.group(1): m.group(2) for m in _IMPORT.finditer(text)})
        for m in _USE.finditer(text):
            prefix, arg = m.group(2), m.group(3)
            targets = [r.group(1) for r in _REQUIRE.finditer(arg)]
            targets += [names[a] for a in re.findall(r"[A-Za-z_$][\w$]*", arg) if a in names]
            for t in targets:
                r = _resolve(f, t)
                if r:
                    prefixes.setdefault(r, set()).add(prefix)
        for m in _REGISTER.finditer(text):
            arg, prefix = m.group(1), m.group(2)
            t = _REQUIRE.search(arg)
            rel = t.group(1) if t else names.get(arg)
            r = _resolve(f, rel) if rel else None
            if r:
                prefixes.setdefault(r, set()).add(prefix)
    routes: set[tuple[str, str]] = set()
    for f, text in texts.items():
        pre = prefixes.get(f.resolve(), {""})
        for m in _ROUTE.finditer(text):
            obj, method, path = m.group(1), m.group(2).upper(), m.group(4)
            if obj in _NOT_ROUTERS:
                continue
            for p in (pre if obj != "app" else {""}):
                routes.add((method, join(p, path)))
        if "fetch" in text and ("export default" in text or "addEventListener" in text):
            for m in _CF_PATH.finditer(text):
                routes.add(("GET", m.group(1)))
    # Next.js: pages/api/** (any method) and app/**/route.* (exported GET/POST/...)
    for f in files(root, (".js", ".ts", ".tsx", ".jsx")):
        parts = f.relative_to(root).parts
        if "pages" in parts and "api" in parts[parts.index("pages") + 1:parts.index("pages") + 2]:
            path = re.sub(r"/index$", "", "/" + "/".join(parts[parts.index("pages") + 1:])[: -len(f.suffix)])
            routes.update({("GET", path), ("POST", path)})
        elif f.stem == "route" and "app" in parts:
            sub = parts[parts.index("app") + 1:-1]
            if not sub or sub[0] != "api":
                continue
            text = _read(f)
            path = "/" + "/".join(s for s in sub if not s.startswith("("))
            for method in METHODS:
                if re.search(rf"export\s+(async\s+)?function\s+{method}\b|export\s+const\s+{method}\b", text):
                    routes.add((method, path))
    return routes


def py_routes(root: Path) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for f in files(root, (".py",)):
        text = _read(f)
        if "route" not in text and ".get(" not in text and ".post(" not in text:
            continue
        prefixes = {m.group(1): m.group(2) for m in _PY_PREFIX.finditer(text)}
        for m in _PY_ROUTE.finditer(text):
            obj = m.group(1)
            if obj in ("requests", "httpx", "client", "session", "self", "os", "dict", "d", "data", "params", "cache"):
                continue
            routes.add((m.group(2).upper(), join(prefixes.get(obj, ""), m.group(4))))
        for m in _PY_FLASK.finditer(text):
            methods = re.findall(r"['\"](GET|POST|PUT|PATCH|DELETE)['\"]", m.group(3) or "") or ["GET"]
            for method in methods:
                routes.add((method, m.group(2)))
        if "aiohttp" in text:
            for m in _PY_AIOHTTP.finditer(text):
                routes.add((m.group(1).upper(), m.group(3)))
    return routes


def rs_routes(root: Path) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for f in files(root, (".rs",)):
        text = _read(f)
        if "axum" in text or ".route(" in text:
            for m in _RS_AXUM.finditer(text):
                for meth in set(_RS_AXUM_M.findall(m.group(2)[:400])):
                    routes.add((meth.upper(), m.group(1)))
        if "actix" in text:
            for m in _RS_ACTIX.finditer(text):
                routes.add((m.group(1).upper(), m.group(2)))
            for m in _RS_ACTIX_RES.finditer(text):
                for meth in re.findall(r"web::(get|post|put|patch|delete)\(\)", m.group(2)):
                    routes.add((meth.upper(), m.group(1)))
    return routes


def go_routes(root: Path) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for f in files(root, (".go",)):
        text = _read(f)
        for m in _GO_STD.finditer(text):
            full, meth = m.group(1), m.group(2)
            path = full.split(" ", 1)[1] if meth else full
            routes.add((meth or "GET", path))
        for m in _GO_FW.finditer(text):
            routes.add((m.group(1).upper(), m.group(2)))
    return routes


def extract(root: str | Path) -> list[tuple[str, str]]:
    """(METHOD, path) for every HTTP route found under root, with {param} placeholders."""
    root = Path(root)
    routes = js_routes(root) | py_routes(root) | rs_routes(root) | go_routes(root)
    out = set()
    for method, path in routes:
        path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)          # Flask <int:id>
        path = re.sub(r"\{(\w+):[^}]*\}", r"{\1}", path)           # {id:int}, {rest:path}
        path = re.sub(r"\{\*(\w+)\}|\*(\w+)$", lambda m: "{" + (m.group(1) or m.group(2)) + "}", path)
        if re.search(r"[*()]|\$\{|\s", path) or len(path) > 200:
            continue
        out.add((method, path.rstrip("/") or "/"))
    return sorted(out)


def op_id(method: str, path: str) -> str:
    segs = [s for s in path.strip("/").split("/") if s]
    if segs and segs[0] == "api":
        segs = segs[1:]
    if segs and re.fullmatch(r"v\d+", segs[0]):
        segs = segs[1:]
    words = []
    for s in segs:
        m = _PARAM.fullmatch(s)
        if m:
            words.append("by_" + next(g for g in m.groups() if g))
        else:
            words.append(re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower() or "x")
    group = words[0] if words and not words[0].startswith("by_") else "root"
    rest = "_".join(words[1:]) if words and not words[0].startswith("by_") else "_".join(words)
    return f"{group}.{method.lower()}" + (f"_{rest}" if rest else "")
