"""Sharing module overlays through a catalog repo (Module Management Hub, MH-E).

A catalog is a git repo with one folder per entry: the overlay files of a module (abp-module.toml, abp-ops.toml, ...)
plus an `entry.toml` (name, upstream git URL, optional pinned ref, description, platforms, tools), and a top-level
`index.json` generated from the entries. Catalogs are listed in config, none by default:

    modules:
      catalogs:
        - {name: mine, url: "https://github.com/<owner>/<catalog-repo>.git", branch: main}

  * sync     shallow fetch of each catalog into data/module-catalogs/<name> (read-only use, never edited by hand)
  * list     the entries of the synced catalogs
  * install  clone the entry's upstream (shallow; https on an allowed host; its pinned ref if it has one) into the normal
             module location, copy the entry's overlay into data/module-overlays/, run `abp_modkit check --project`,
             register. Refused if a module of that name (or a folder where its checkout goes) exists.
  * publish  copy a module's overlay files, and nothing else from its checkout, into a working clone of the catalog
             (data/module-catalogs/<name>.work), regenerate index.json, commit on a new branch. Machine paths and
             secrets stop it. Pushing the branch is a separate step a person takes (`push=True` here, `--push` in the CLI).

The API, `abp modules catalog ...` and the Modules page come here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from bot.modules import manifest as mf
from bot.modules import registry
from bot.modules.client import ModuleError

ENTRY_FILE = "entry.toml"
INDEX_FILE = "index.json"
MAX_FILES = 200
MAX_FILE_BYTES = 1_000_000
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_ENTRY = re.compile(r"^[A-Za-z0-9][\w.-]{0,80}$")
_REF = re.compile(r"^[\w][\w./+-]{0,119}$")
_ALLOW_LOCAL = False        # tests only: let catalogs and upstreams be local folders (the real thing is https only)


def _err(msg: str, code: str = "invalid", status: int = 400) -> ModuleError:
    return ModuleError(msg, code=code, status=status)


def _git(args: list[str], cwd: Path | None, timeout: float = 900, check: bool = True) -> tuple[int, str]:
    pol = [] if _ALLOW_LOCAL else ["-c", "protocol.file.allow=never"]
    r = subprocess.run(["git", *pol, "-c", "protocol.ext.allow=never", "-c", "core.longpaths=true", *args], cwd=cwd,
                       capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"})
    out = (r.stdout + r.stderr).strip()
    if check and r.returncode != 0:
        raise ModuleError(f"git {args[0]} failed: {out[-500:]}", code="git", status=502)
    return r.returncode, r.stdout.strip() if r.returncode == 0 else out


def _url_ok(url: str, *, upstream: bool) -> str:
    """https only, no credentials; an upstream must also be on an allowed host (the skills fetch's list)."""
    u = str(url or "").strip()
    if _ALLOW_LOCAL and u and not u.startswith(("http://", "https://", "ssh://", "git@")):
        return u
    from urllib.parse import urlparse
    p = urlparse(u)
    if p.scheme != "https" or not p.hostname or not p.path.strip("/"):
        raise _err(f"{u!r}: only https:// git URLs are accepted")
    if p.username or p.password:
        raise _err("URLs with embedded credentials are not accepted")
    if upstream:
        from bot import skill_install
        if p.hostname.lower() not in skill_install.allowed_hosts():
            raise _err(f"{p.hostname} is not an allowed source (allowed: {', '.join(sorted(skill_install.allowed_hosts()))}; "
                       "add more under native_agent.skills.allowed_hosts)")
    return u


def root() -> Path:
    d = registry.abp_root() / "data" / "module-catalogs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def catalogs() -> list[dict]:
    """The configured catalogs ({name, url, branch}); anything malformed is skipped."""
    out, seen = [], set()
    for c in registry._cfg().get("catalogs") or []:
        if not isinstance(c, dict):
            continue
        name, url, branch = str(c.get("name") or ""), str(c.get("url") or "").strip(), str(c.get("branch") or "main")
        if _NAME.match(name) and url and _REF.match(branch) and name not in seen:
            seen.add(name)
            out.append({"name": name, "url": url, "branch": branch})
    return out


def _catalog(name: str) -> dict:
    cs = catalogs()
    for c in cs:
        if c["name"] == name:
            return c
    if not name and len(cs) == 1:
        return cs[0]
    raise _err(f"no catalog {name!r} (configured: {', '.join(c['name'] for c in cs) or 'none; add modules.catalogs'})",
               code="not_found", status=404)


def _fetch(c: dict, dest: Path, depth: bool = True) -> bool:
    """Make `dest` hold the catalog's branch (initialising it when new). False when the catalog has no such branch yet."""
    _url_ok(c["url"], upstream=False)
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        _git(["init", "-q"], dest)
        _git(["remote", "add", "origin", c["url"]], dest)
    else:
        _git(["remote", "set-url", "origin", c["url"]], dest)
    rc, out = _git(["fetch", "-q", *(["--depth", "1"] if depth else []), "--no-tags", "origin", c["branch"]], dest,
                   check=False)
    if rc != 0:
        if "couldn't find remote ref" in out or "Couldn't find remote ref" in out:
            return False
        raise ModuleError(f"git fetch failed: {out[-500:]}", code="git", status=502)
    return True


def sync(name: str = "") -> list[dict]:
    """Fetch each configured catalog (or one) into the data folder, read-only use. [{name, commit, entries, note}]."""
    res = []
    for c in ([_catalog(name)] if name else catalogs()):
        dest = root() / c["name"]
        row: dict[str, Any] = {"name": c["name"], "commit": "", "note": ""}
        try:
            if _fetch(c, dest):
                _git(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "-f", "FETCH_HEAD"], dest)
                row["commit"] = _git(["rev-parse", "--short", "HEAD"], dest)[1]
            else:
                row["note"] = f"the catalog has no branch {c['branch']!r} yet (empty?)"
        except ModuleError as e:
            row["note"] = str(e)
        row["entries"] = len(_read_entries(c["name"])[0])
        res.append(row)
    return res


def _entry_meta(d: Path) -> dict:
    try:
        t = tomllib.loads((d / ENTRY_FILE).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise _err(f"{d.name}/{ENTRY_FILE}: {e}") from e
    name = str(t.get("name") or d.name)
    if name != d.name or not _ENTRY.match(name):
        raise _err(f"{d.name}/{ENTRY_FILE}: name must equal the folder name")
    ref = str(t.get("ref") or "")
    if ref and not _REF.match(ref):
        raise _err(f"{name}: not a valid ref")
    lst = lambda k: [str(x) for x in (t.get(k) or []) if isinstance(x, (str, int))]  # noqa: E731
    return {"name": name, "upstream": str(t.get("upstream") or ""), "ref": ref, "description": str(t.get("description") or ""),
            "platforms": lst("platforms"), "tools": lst("tools")}


def _read_entries(cat: str) -> tuple[list[dict], dict[str, str]]:
    base = root() / cat
    out, errors = [], {}
    for d in sorted(p for p in base.iterdir() if (p / ENTRY_FILE).is_file()) if base.is_dir() else []:
        try:
            e = _entry_meta(d)
            e["files"] = sorted(f.name for f in d.iterdir() if f.is_file() and f.name != ENTRY_FILE)
            try:
                e["module_id"] = mf.load(d / mf.MANIFEST_FILE).id
            except (mf.ManifestError, OSError):
                e["module_id"] = ""
            out.append({"catalog": cat, **e})
        except ModuleError as ex:
            errors[f"{cat}/{d.name}"] = str(ex)
    return out, errors


def entries(name: str = "") -> dict:
    """The entries of the synced catalogs. {catalogs: [...], entries: [...], errors: {...}}."""
    cs = [_catalog(name)] if name else catalogs()
    known = registry.modules()
    rows, errors = [], {}
    for c in cs:
        es, er = _read_entries(c["name"])
        errors.update(er)
        for e in es:
            e["installed"] = e["module_id"] in known
        rows += es
    return {"catalogs": [{**c, "synced": (root() / c["name"] / ".git").exists()} for c in cs], "entries": rows,
            "errors": errors}


def _drop_checkout_dir(text: str) -> str:
    """The manifest without `[checkout] dir`: that is a path on one machine, and a catalog entry must work on any."""
    out, sect = [], ""
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("["):
            sect = s.strip("[] ")
        if sect == "checkout" and re.match(r"^dir\s*=", s):
            continue
        out.append(ln)
    return "\n".join(out) + "\n"


def _rmtree(p: Path) -> None:
    """Remove a folder this module made (git marks its object files read-only, which Windows will not delete)."""
    def writable(fn, path, _exc):
        os.chmod(path, 0o700)
        fn(path)
    shutil.rmtree(p, onerror=writable) if p.exists() else None


def _platform() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def _fetch_upstream(url: str, ref: str, branch: str, dest: Path, log) -> str:
    """Shallow checkout of `ref` (a tag, branch or commit) or, without one, of the branch's tip (else upstream's
    default branch). Returns the short commit."""
    dest.mkdir(parents=True)
    _git(["init", "-q"], dest)
    _git(["remote", "add", "origin", url], dest)
    fetch = ["fetch", "-q", "--depth", "1", "--no-tags", "origin"]
    if ref:
        rc, out = _git([*fetch, ref], dest, timeout=1800, check=False)
        if rc != 0:
            log(f"  the server would not give {ref} shallow; fetching the whole history")
            _git(["fetch", "-q", "--no-tags", "origin"], dest, timeout=1800)
            if _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], dest, check=False)[0] != 0:
                raise ModuleError(f"upstream has no ref {ref!r}", code="git", status=409)
            _git(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", f"{ref}^{{commit}}"], dest)
        else:
            _git(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"], dest)
    else:
        name = branch or "main"
        rc, out = _git([*fetch, name], dest, timeout=1800, check=False)
        if rc != 0:
            name = "main"
            _git([*fetch, "HEAD"], dest, timeout=1800)
        _git(["-c", "advice.detachedHead=false", "checkout", "-q", "-B", name, "FETCH_HEAD"], dest)
        if rc == 0:
            _git(["branch", "-q", f"--set-upstream-to=origin/{name}"], dest, check=False)
    return _git(["rev-parse", "--short", "HEAD"], dest)[1]


def _plan_install(cat: str, entry: str) -> tuple[dict, Path, mf.Manifest, Path]:
    """(catalog, entry folder, its manifest, where the checkout goes), refusing a clash before anything is cloned."""
    c = _catalog(cat)
    if not _ENTRY.match(entry or ""):
        raise _err("entry: letters, digits, . _ - only")
    src = root() / c["name"] / entry
    if not (src / ENTRY_FILE).is_file() and not (root() / c["name"] / ".git").exists():
        sync(c["name"])                                   # never synced: fetch it once
    if not (src / ENTRY_FILE).is_file():
        raise _err(f"the catalog {c['name']!r} has no entry {entry!r} (sync it first?)", code="not_found", status=404)
    meta = _entry_meta(src)
    try:
        m = mf.load(src / mf.MANIFEST_FILE)
    except (mf.ManifestError, OSError) as e:
        raise _err(f"{entry}: its abp-module.toml is not valid: {e}") from e
    m.checkout_dir = ""
    _url_ok(meta["upstream"], upstream=True)
    if m.id in registry.modules():
        raise _err(f"there is already a module {m.id!r}; nothing was installed", code="exists", status=409)
    overlay = registry.user_overlay_root() / m.id
    dest = registry.install_dir(m)
    for p in (overlay, dest):
        if p.exists() and (not p.is_dir() or any(p.iterdir())):
            raise _err(f"{p} exists already; nothing was installed", code="exists", status=409)
    return {**meta, "catalog": c["name"]}, src, m, dest


def install(cat: str, entry: str) -> dict:
    """Install a catalog entry as a module (a background job: follow it with harness.job(id))."""
    from bot.modules import harness
    meta, src, m, dest = _plan_install(cat, entry)

    def go(log):
        from abp_modkit import cli as modkit_cli
        meta2, src2, m2, dest2 = _plan_install(cat, entry)           # again: another job may have won meanwhile
        log(f"cloning {meta2['upstream']}" + (f" at {meta2['ref']}" if meta2["ref"] else "") + f" into {dest2}")
        overlay = registry.user_overlay_root() / m2.id
        made: list[Path] = []
        try:
            made.append(dest2)
            commit = _fetch_upstream(meta2["upstream"], meta2["ref"], m2.branch if not meta2["ref"] else "", dest2, log)
            log(f"checked out {commit}")
            made.append(overlay)
            overlay.mkdir(parents=True)
            for f in sorted(src2.iterdir()):
                if f.is_file() and f.name != ENTRY_FILE and not f.is_symlink():
                    data = f.read_text(encoding="utf-8") if f.name == mf.MANIFEST_FILE else None
                    if data is None:
                        shutil.copy2(f, overlay / f.name)
                    else:
                        (overlay / f.name).write_text(_drop_checkout_dir(data), encoding="utf-8", newline="\n")
        except BaseException:
            for p in made:                                          # only what this install made
                _rmtree(p)
            raise
        plat = _platform()
        if meta2["platforms"] and plat not in meta2["platforms"]:
            log(f"  warning: the entry says it is for {', '.join(meta2['platforms'])}; this is {plat}")
        missing = [t for t in meta2["tools"] if not shutil.which(t)]
        for t in missing:
            log(f"  warning: the tool {t!r} is not on PATH")
        log("checking it: a real hub over the clone, its operations, MCP")
        check = modkit_cli.check(overlay, verbose=False, project=dest2)
        for c in check["checks"]:
            log(("  ok    " if c["ok"] else "  FAIL  ") + c["check"] + (f" ({c['detail']})" if c.get("detail") else ""))
        registry.modules(refresh=True)
        mm = registry.modules().get(m2.id)
        return {"id": m2.id, "entry": entry, "catalog": meta2["catalog"],
                "path": str(dest2), "overlay": str(overlay), "commit": commit, "ref": meta2["ref"],
                "checked": check["ok"], "checks": check["checks"], "missing_tools": missing,
                "module": mm.public() if mm else None}
    return harness._start_job(m, "catalog-install", go)


# ---- publishing -----------------------------------------------------------------------------------------------
def _leak_rules():
    """The leak guard's machine-path rules (scripts/leak_guard.py) as (module, rules), or (None, []) if it is absent."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "leak_guard.py"
    if not path.is_file():
        return None, []
    spec = importlib.util.spec_from_file_location("catalog_leak_guard", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    finally:
        sys.modules.pop(spec.name, None)
    return mod, [r for r in mod._build_rules(None) if r[0] == "machine_paths"]


def scrub(files: dict[str, bytes]) -> list[str]:
    """What must not be published: machine paths, secret-shaped strings, secrets files, links, big or binary files.
    Lines like 'file:line [kind]'; never the matched value."""
    from abp_modkit import repo as rp
    bad: list[str] = []
    mod, rules = _leak_rules()
    if len(files) > MAX_FILES:
        bad.append(f"{len(files)} files (limit {MAX_FILES})")
    for rel, data in sorted(files.items()):
        if rp.SECRET_FILES.search(rel) and not rp.SAFE_ENV.search(rel):
            bad.append(f"{rel} [a secrets file]")
            continue
        if len(data) > MAX_FILE_BYTES:
            bad.append(f"{rel} [{len(data) >> 10} KB: an overlay file is text, under {MAX_FILE_BYTES >> 10} KB]")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{rel} [not text]")
            continue
        for kind, pat in rp.PATTERNS.items():
            if re.search(pat, text):
                bad.append(f"{rel} [{kind}]")
        if mod is not None:
            for f in mod.scan_text(rel, text, rules):
                bad.append(f"{rel}:{f.line} [machine path]")
    return bad


def _overlay_files(m: mf.Manifest) -> dict[str, bytes]:
    """The module files of `m` and nothing else: its overlay folder, or abp-module.toml and abp-ops.toml of a project."""
    if m.overlay:
        base, names = Path(m.overlay), None
    else:
        base, names = registry.install_dir(m), [mf.MANIFEST_FILE, "abp-ops.toml", "abp-panel.json"]
    out: dict[str, bytes] = {}
    for p in sorted(base.rglob("*")) if names is None else [base / n for n in names]:
        if not p.is_file() and not p.is_symlink():
            continue
        rel = p.relative_to(base).as_posix()
        if names is None and (".git" in p.relative_to(base).parts or "__pycache__" in rel):
            continue
        if p.is_symlink():
            raise _err(f"{rel} is a link; an overlay is plain files", code="leak", status=422)
        out[rel] = p.read_bytes()
    if mf.MANIFEST_FILE not in out:
        raise _err(f"{m.name} has no {mf.MANIFEST_FILE} to publish", code="not_found", status=404)
    out[mf.MANIFEST_FILE] = _drop_checkout_dir(out[mf.MANIFEST_FILE].decode("utf-8")).encode("utf-8")
    out.pop(ENTRY_FILE, None)
    return out


def _toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def _entry_toml(name: str, upstream: str, ref: str, desc: str, platforms: list[str], tools: list[str]) -> str:
    arr = lambda xs: "[" + ", ".join(_toml_str(x) for x in xs) + "]"  # noqa: E731
    lines = [f"# {name}: a module overlay for ABP. Install: abp modules catalog install {name}", f"name = {_toml_str(name)}",
             f"upstream = {_toml_str(upstream)}"]
    if ref:
        lines.append(f"ref = {_toml_str(ref)}")
    lines += [f"description = {_toml_str(desc)}", f"platforms = {arr(platforms)}", f"tools = {arr(tools)}"]
    return "\n".join(lines) + "\n"


def _write_index(work: Path) -> None:
    rows = []
    for d in sorted(p for p in work.iterdir() if p.is_dir() and (p / ENTRY_FILE).is_file()):
        try:
            e = _entry_meta(d)
        except ModuleError:
            continue
        rows.append({**e, "files": sorted(f.name for f in d.iterdir() if f.is_file() and f.name != ENTRY_FILE)})
    (work / INDEX_FILE).write_text(json.dumps({"version": 1, "entries": rows}, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8", newline="\n")


def publish(mid: str, cat: str = "", *, ref: str = "", push: bool = False) -> dict:
    """Commit a module's overlay files to a new branch of the catalog's working clone. Pushes that branch only if
    `push` is true (the CLI's --push; the API never does). Refuses if a machine path or a secret is in the files."""
    c = _catalog(cat)
    try:
        m = registry.get(mid)
    except registry.UnknownModule as e:
        raise _err(str(e).strip('"'), code="not_found", status=404) from e
    if ref and not _REF.match(ref):
        raise _err("ref: letters, digits, . / _ - + only")
    if not _ENTRY.match(m.id):
        raise _err(f"{m.id!r} is not usable as an entry name")
    upstream = _url_ok(m.repo, upstream=True)
    files = _overlay_files(m)
    bad = scrub(files)
    if bad:
        raise ModuleError("not published, these must not go into a catalog: " + "; ".join(bad[:20]), code="leak",
                          status=422)
    work = root() / (c["name"] + ".work")
    if (work / ".git").exists() and _git(["status", "--porcelain"], work)[1]:
        raise _err(f"{work} has uncommitted changes; clean it up first", code="dirty", status=409)
    has_branch = _fetch(c, work)
    branch = f"publish/{m.id}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
    if has_branch:
        _git(["checkout", "-q", "-B", branch, "FETCH_HEAD"], work)
    else:
        _git(["checkout", "-q", "--orphan", branch], work)
        _git(["rm", "-rq", "--cached", "--ignore-unmatch", "."], work, check=False)
        for p in list(work.iterdir()):
            if p.name != ".git":
                _rmtree(p) if p.is_dir() else p.unlink()
    dest = work / m.id
    if dest.exists():
        _rmtree(dest)
    dest.mkdir(parents=True)
    for rel, data in files.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
    plats = [o for o in m.host_os] if set(m.host_os) != set(mf.OS_NAMES) else []
    (dest / ENTRY_FILE).write_text(_entry_toml(m.id, upstream, ref, m.description, plats,
                                               sorted({r.tool for r in m.requires})), encoding="utf-8", newline="\n")
    _write_index(work)
    _git(["add", "-A"], work)
    if not _git(["status", "--porcelain"], work)[1]:
        return {"catalog": c["name"], "entry": m.id, "changed": False, "branch": None, "pushed": False,
                "note": "the catalog already has exactly this", "work": str(work)}
    ident = [] if _git(["config", "user.name"], work, check=False)[0] == 0 else ["-c", "user.name=ABP", "-c", "user.email=abp@localhost"]
    _git([*ident, "commit", "-q", "-m", f"{m.id}: overlay for {m.name}"], work)
    commit = _git(["rev-parse", "--short", "HEAD"], work)[1]
    diff = _git(["show", "--stat", "--patch", "--format=%h %s", "HEAD"], work)[1]
    res = {"catalog": c["name"], "entry": m.id, "changed": True, "branch": branch, "commit": commit, "work": str(work),
           "files": sorted(files), "diff": diff[:20000], "pushed": False}
    if push:
        push_branch(work, branch)
        res["pushed"] = True
    return res


def push_branch(work: str | Path, branch: str) -> str:
    """Push a prepared branch (never anything else, never forced)."""
    work = Path(work)
    if not (work / ".git").exists() or not branch.startswith("publish/") or not _REF.match(branch):
        raise _err("not a prepared catalog branch")
    return _git(["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], work, timeout=600)[1]
