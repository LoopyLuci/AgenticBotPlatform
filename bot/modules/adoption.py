"""Making a project an ABP module from ABP itself: adopt (abp_modkit writes its abp-module.toml and abp-ops.toml), register
it (modules.projects), forget it, publish it to its own private GitHub repo, and find projects that are not modules yet.
And the Module Management Hub's front door, add_from_url: any git repo (a GitHub URL, or owner/name) becomes a module
in one step, as an overlay (its module files on ABP's side, its checkout exactly as upstream has it).

The agent tools module_adopt and module_add, POST /api/modules/adopt and /api/modules/add, `abp modules adopt|add` and
the Modules page all come here.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bot.modules import registry
from bot.modules.client import ModuleError
from bot.modules.manifest import Manifest

_SHORT = re.compile(r"^([A-Za-z0-9][\w.-]*)/([\w.-]+?)(?:\.git)?$")
_HTTPS = re.compile(r"^https://([\w.-]+)/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def parse_repo(url: str) -> tuple[str, str, str]:
    """(clone URL, owner, name) for a repo given as https://host/owner/name[.git], github.com/owner/name, or
    owner/name (GitHub). Only https: no ssh, file or local paths."""
    u = (url or "").strip()
    if u.startswith("github.com/"):
        u = "https://" + u
    m = _HTTPS.match(u)
    if m:
        return f"https://{m[1]}/{m[2]}/{m[3]}.git", m[2], m[3]
    m = _SHORT.match(u)
    if m and "." not in m[1]:
        return f"https://github.com/{m[1]}/{m[2]}.git", m[1], m[2]
    raise ModuleError("give a repo as https://github.com/owner/name (any https git host), or owner/name",
                      code="invalid", status=400)


def clone_root() -> Path:
    """Where repos added from a URL are cloned: modules.clone_root, else data/module-checkouts under ABP."""
    root = registry._cfg().get("clone_root")
    d = Path(str(root)).expanduser() if root else registry.abp_root() / "data" / "module-checkouts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _git(args: list[str], cwd: Path | None, log, timeout: float = 1800) -> str:
    cmd = ["git", "-c", "protocol.file.allow=never", "-c", "core.longpaths=true", *args]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                       errors="replace", env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"})
    out = (r.stdout + r.stderr).strip()
    for line in out.splitlines()[-8:]:
        log("  " + line)
    if r.returncode != 0:
        raise ModuleError(f"git {args[0]} failed: {out[-600:]}", code="git", status=502)
    return r.stdout.strip()


def add_from_url(url: str, *, mid: str = "", name: str = "", shallow: bool = True, branch: str = "") -> dict:
    """Clone a repo (or update the clone already here), make it a module as an overlay, check it, register it.
    A background job: follow it with harness.job(id)."""
    from abp_modkit import adopt as ad
    from abp_modkit import cli as modkit_cli
    from abp_modkit.detect import slug
    from bot.modules import harness
    clone_url, owner, repo_name = parse_repo(url)
    if branch and not re.fullmatch(r"[\w./-]{1,120}", branch):
        raise ModuleError("branch: letters, digits, . / - _ only", code="invalid", status=400)
    mid = slug(mid or repo_name)
    if mid in registry.modules() and not registry.modules()[mid].overlay:
        raise ModuleError(f"there is already a module {mid!r}; give another id", code="exists", status=409)
    placeholder = Manifest(id=mid, name=name or repo_name, repo=clone_url)

    def go(log):
        dest = clone_root() / repo_name
        if (dest / ".git").exists():
            origin = _git(["-C", str(dest), "remote", "get-url", "origin"], None, log, 60)
            if origin.rstrip("/").removesuffix(".git").lower() != clone_url.removesuffix(".git").lower():
                raise ModuleError(f"{dest} is a clone of {origin}, not {clone_url}", code="exists", status=409)
            log(f"updating the clone in {dest}")
            _git(["-C", str(dest), "pull", "--ff-only"], None, log)
        else:
            log(f"cloning {clone_url} into {dest}" + (" (latest commit only)" if shallow else ""))
            _git(["clone", *(["--depth", "1"] if shallow else []), *(["--branch", branch] if branch else []),
                  clone_url, str(dest)], None, log)
        overlay = registry.user_overlay_root() / mid
        log("reading the project and writing its module files (an overlay: nothing is written into the clone)")
        res = ad.adopt(dest, mid=mid, name=name or repo_name, repo=clone_url, overlay=overlay, record_checkout=True)
        log(f"{res.get('operations')} operations ({', '.join(res.get('stacks') or []) or 'no stack recognized'})")
        for n in res.get("notes") or []:
            log("  note: " + n)
        log("checking it: a real hub over the clone, its operations, MCP")
        check = modkit_cli.check(overlay, verbose=False, project=dest)
        for c in check["checks"]:
            log(("  ok    " if c["ok"] else "  FAIL  ") + c["check"] + (f" ({c['detail']})" if c.get("detail") else ""))
        registry.modules(refresh=True)
        m = registry.modules().get(mid)
        return {"id": mid, "name": name or repo_name, "owner": owner, "repo": clone_url, "path": str(dest),
                "overlay": str(overlay), "stacks": res.get("stacks"), "operations": res.get("operations"),
                "description": res.get("description"), "checked": check["ok"], "checks": check["checks"],
                "module": m.public() if m else None}
    return harness._start_job(placeholder, "add", go)


def _projects() -> list[str]:
    return [str(p) for p in (registry._cfg().get("projects") or [])]


def _norm(p: str | Path) -> str:
    return str(Path(p).expanduser().resolve()).replace("\\", "/")


def register(path: str | Path) -> bool:
    from bot.config import config
    p = _norm(path)
    projects = _projects()
    if any(_norm(x) == p for x in projects):
        return False
    config.set_value(["modules", "projects"], [*projects, p], actor="modules")
    registry.modules(refresh=True)
    return True


def unregister(mid: str) -> dict:
    """Stop listing an adopted project (its files are left as they are)."""
    from bot.config import config
    m = registry.get(mid)
    if m.overlay and Path(m.overlay).resolve().parent == registry.user_overlay_root().resolve():
        # a module added from a URL: its overlay (the module files ABP wrote) goes; the clone stays where it is
        shutil.rmtree(m.overlay)
        registry.modules(refresh=True)
        return {"forgotten": m.id, "path": str(registry.install_dir(m)), "overlay_removed": m.overlay}
    d = registry._project_dirs.get(m.id)
    if d is None:
        raise ModuleError(f"{m.name} is not an adopted project (it is built in)", code="unsupported", status=400)
    keep = [x for x in _projects() if _norm(x) != _norm(d)]
    config.set_value(["modules", "projects"], keep, actor="modules")
    registry._project_dirs.pop(m.id, None)
    registry.modules(refresh=True)
    return {"forgotten": m.id, "path": str(d)}


def adopt(path: str, *, mid: str = "", name: str = "", dry_run: bool = False, force: bool = False,
          do_register: bool = True) -> dict:
    from abp_modkit import adopt as ad
    from abp_modkit.spec import SpecError
    root = Path(str(path)).expanduser()
    if not root.is_dir():
        raise ModuleError(f"{root} is not a folder on this machine", code="not_found", status=404)
    if (root / "abp-module.toml").is_file() and "abp_modkit" not in (root / "abp-module.toml").read_text(
            encoding="utf-8", errors="replace")[:200] and not force:
        # a module with its own hand-written manifest: just register it
        res: dict[str, Any] = {"path": str(root.resolve()), "wrote": [], "kept": ["abp-module.toml"],
                               "note": "it already has its own abp-module.toml; registered it as it is"}
    else:
        try:
            res = ad.adopt(root, mid=mid, name=name, dry_run=dry_run, force=force)
        except SpecError as e:
            raise ModuleError(str(e), code="invalid", status=400) from e
    if do_register and not dry_run:
        res["registered"] = register(root)
        registry.modules(refresh=True)
        try:
            res["module"] = registry.find(res.get("id") or "").public() if res.get("id") else None
        except Exception:  # noqa: BLE001
            res["module"] = None
        err = registry.manifest_errors()
        mine = {k: v for k, v in err.items() if k == res.get("id") or str(root.name) in k}
        if mine:
            res["manifest_errors"] = mine
    return res


def publish(mid: str, *, push: bool = False, owner: str = "LoopyLuci") -> dict:
    """Commit the module files and create the project's private GitHub repo (a background job)."""
    from abp_modkit import detect as dt
    from abp_modkit import repo as rp
    from bot.modules import harness
    m = registry.get(mid)
    d = registry.install_dir(m)
    if not d.is_dir():
        raise ModuleError(f"{m.name} is not on this machine", code="not_installed")

    def go(log):
        plan = dt.detect(d, mid=m.id, name=m.name)
        res = rp.publish(d, stacks=plan.stacks, owner=owner, only=["abp-module.toml", "abp-ops.toml"],
                         push=push or not (d / ".git").exists(), description=m.description)
        for line in res["log"]:
            log(line)
        return res
    return harness._start_job(m, "publish", go)


def candidates(folder: str) -> list[dict]:
    """The project folders in `folder` and whether each is a module already."""
    from abp_modkit import detect as dt
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ModuleError(f"{root} is not a folder on this machine", code="not_found", status=404)
    known = {_norm(registry.install_dir(m)): m.id for m in registry.modules().values()}
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        markers = [f for f in ("package.json", "Cargo.toml", "pyproject.toml", "requirements.txt", "go.mod", "flake.nix",
                               "README.md", "abp-module.toml") if (d / f).is_file()]
        if not markers and not any(d.glob("*.py")) and not any(d.glob("*.ps*1")):
            continue
        out.append({"path": str(d), "name": d.name, "id": dt.slug(d.name), "module": known.get(_norm(d)),
                    "has_manifest": (d / "abp-module.toml").is_file(), "markers": markers,
                    "git": (d / ".git").exists()})
    return out
