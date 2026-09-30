"""Making a project an ABP module from ABP itself: adopt (abp_modkit writes its abp-module.toml and abp-ops.toml), register
it (modules.projects), forget it, publish it to its own private GitHub repo, and find projects that are not modules yet.

The agent tool module_adopt, POST /api/modules/adopt, `abp module adopt` and the Modules page all come here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bot.modules import registry
from bot.modules.client import ModuleError


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
