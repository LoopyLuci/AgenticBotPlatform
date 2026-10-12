"""Refreshing a module after upstream changed (Module Management Hub, MH-C): fetch upstream, fast-forward the checkout,
read the project again, merge the regenerated module files with what a person made, and say what changed in the
operations. Nothing hand-made is lost, and nothing in the checkout is ever reset or discarded.

For a module added from a URL (an overlay on ABP's side) or an adopted project (module files in its checkout):

  1. git fetch, then the checkout must be clean (tracked files; the module files of an adopted project are allowed
     to differ) and able to fast-forward. Local changes or a diverged branch stop the refresh, untouched.
  2. The upstream tree is read into a scratch folder and `abp_modkit.adopt` runs over a copy of the module files:
     the same merge as a re-adopt (MK-B). Hand-kept operations stay, summaries/inputs/flags and service settings
     edited by hand win over detection, an abp-ops.toml or abp-module.toml without the generated marker is kept as
     it is, overlay files are never replaced by detection.
  3. The difference between the module files before and after is the report: operations added, removed and changed
     (kind/method/path/inputs = signature, argv/cwd/env/timeouts = command, summary = description) and the manifest
     and service fields that changed.
  4. Only with apply=True the checkout is fast-forwarded and the merged files written. A dry run (the default)
     writes nothing but git's own remote-tracking refs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bot.modules import registry
from bot.modules.client import ModuleError

MODULE_FILES = ("abp-module.toml", "abp-ops.toml")
_OP_FIELDS = {
    "signature": ("kind", "method", "path", "inputs"),
    "command": ("argv", "cwd", "env", "timeout_s", "background", "extra_args"),
    "description": ("summary",),
    "flags": ("mutating_flag", "destructive_flag"),
}


def _git(d: Path, *args: str, check: bool = True, env: dict | None = None, timeout: float = 600) -> tuple[int, str]:
    r = subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(d), *args], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace",
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    if check and r.returncode != 0:
        raise ModuleError(f"git {args[0]} failed: {(r.stdout + r.stderr).strip()[-500:]}", code="git", status=502)
    return r.returncode, r.stdout.strip()


def _target(m) -> tuple[Path, Path | None]:
    """(checkout, overlay folder or None) of a module that can be refreshed."""
    if m.overlay:
        if Path(m.overlay).resolve().parent != registry.user_overlay_root().resolve():
            raise ModuleError(f"{m.name} uses an overlay that ships with ABP (catalog); change it there, a refresh only "
                              "handles overlays made on this machine", code="unsupported", status=400)
        overlay: Path | None = Path(m.overlay)
    elif m.source and m.source != "builtin":
        overlay = None
    else:
        raise ModuleError(f"{m.name} is built in; it has no module files to refresh", code="unsupported", status=400)
    d = registry.install_dir(m)
    if not (d / ".git").exists():
        raise ModuleError(f"{m.name} has no git checkout at {d}", code="not_installed", status=409)
    return d, overlay


def _ahead_behind(d: Path, ref: str) -> tuple[bool, bool]:
    """(a fast-forward to `ref` is possible and moves, diverged)."""
    if _git(d, "rev-parse", "HEAD")[1] == _git(d, "rev-parse", ref)[1]:
        return False, False
    if _git(d, "merge-base", "--is-ancestor", "HEAD", ref, check=False)[0] == 0:
        return True, False
    if _git(d, "merge-base", "--is-ancestor", ref, "HEAD", check=False)[0] == 0:
        return False, False                     # only local commits ahead of upstream: nothing to fast-forward
    return False, True


def _flat(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _changes(old: dict, new: dict, skip: tuple[str, ...] = ()) -> dict[str, dict]:
    return {k: {"from": old.get(k), "to": new.get(k)} for k in sorted(set(old) | set(new))
            if k not in skip and old.get(k) != new.get(k)}


def _brief(o) -> dict:
    return {"id": o.id, "kind": o.kind, "summary": o.summary,
            "signature": (f"{o.method} {o.path}" if o.kind == "http" else " ".join(o.argv))}


def diff_specs(old, new) -> dict:
    """Operations added / removed / changed between two specs, and the service fields that changed."""
    a, b = {o.id: o for o in old.ops}, {o.id: o for o in new.ops}
    changed = []
    for oid in a.keys() & b.keys():
        fields: dict[str, dict] = {}
        for group, names in _OP_FIELDS.items():
            for n in names:
                if getattr(a[oid], n) != getattr(b[oid], n):
                    fields[n] = {"group": group, "from": getattr(a[oid], n), "to": getattr(b[oid], n)}
        if fields:
            changed.append({**_brief(b[oid]), "groups": sorted({f["group"] for f in fields.values()}),
                            "fields": fields})
    return {"added": [_brief(b[i]) for i in b if i not in a], "removed": [_brief(a[i]) for i in a if i not in b],
            "changed": sorted(changed, key=lambda c: c["id"]),
            "service": _changes(asdict(old.service), asdict(new.service), skip=("id", "name"))}


def _load_spec(path: Path):
    from abp_modkit import spec as sp
    if path.is_file():
        return sp.load(path)
    return sp.Spec(sp.Service("", ""), [])


def _read_manifest(path: Path) -> dict:
    try:
        return _flat(tomllib.loads(path.read_text(encoding="utf-8"))) if path.is_file() else {}
    except tomllib.TOMLDecodeError:
        return {}


def _first_line(path: Path) -> str:
    return (path.read_text(encoding="utf-8").splitlines() or [""])[0] if path.is_file() else ""


def _extract(d: Path, ref: str, dest: Path, tmp: Path) -> Path:
    """The tree at `ref` copied into `dest` (git's own checkout-index with a scratch index: the checkout is not touched)."""
    dest.mkdir(parents=True)
    env = {"GIT_INDEX_FILE": str(tmp / f"scratch-{abs(hash(str(dest)))}.index")}
    _git(d, "read-tree", ref, env=env)
    _git(d, "checkout-index", "-a", "-f", f"--prefix={dest.as_posix()}/", env=env)
    return dest


def _norm(op):
    """An operation as it reads back from abp-ops.toml (so it compares equal to the one in the file)."""
    from abp_modkit import spec as sp
    spec = sp.Spec(sp.Service("x", "x"), [op])
    return sp.parse(tomllib.loads(sp.dump(spec))).ops[0]


def _reconcile(m, d: Path, ref: str, src_new: Path, tmp: Path, merged, before) -> tuple[list[str], list[str], dict[str, str]]:
    """What the plain merge cannot know, from detection on the checkout as it is now against upstream's tree.
    (prune, keep, summaries). Prune: operations detection no longer produces and nobody edited; keep: such ones a
    person edited, left in place and reported; summaries: new descriptions for operations whose summary is still
    the one detection wrote (a summary a person changed is never replaced). Operations a person added are in none."""
    from abp_modkit import detect as dt
    src_old = _extract(d, "HEAD", tmp / "old" / d.name, tmp) if ref != "HEAD" else src_new
    old = {o.id: o for o in dt.detect(src_old, mid=m.id, name=m.name).ops}
    new = {o.id: o for o in dt.detect(src_new, mid=m.id, name=m.name).ops}
    have = {o.id: o for o in merged.ops}
    was = {o.id: o for o in before.ops}
    gone = [i for i in old if i not in new and i in have]
    summaries = {i: new[i].summary for i in new if i in old and i in have and i in was and
                 was[i].summary == _norm(old[i]).summary and new[i].summary != old[i].summary}
    return ([i for i in gone if have[i] == _norm(old[i])], [i for i in gone if have[i] != _norm(old[i])], summaries)


def _edit(ops_path: Path, prune: list[str], summaries: dict[str, str]) -> None:
    from abp_modkit import spec as sp
    if not (prune or summaries) or not ops_path.is_file():
        return
    spec = sp.load(ops_path)
    spec.ops = [o for o in spec.ops if o.id not in prune]
    for o in spec.ops:
        if o.id in summaries:
            o.summary = summaries[o.id]
    ops_path.write_text(sp.dump(spec), encoding="utf-8", newline="\n")


def _preview(m, d: Path, overlay: Path | None, ref: str, tmp: Path) -> dict:
    """Run the re-adopt over the tree at `ref` and a copy of the module files; returns what it would give."""
    from abp_modkit import adopt as ad
    from abp_modkit import detect as dt
    src = _extract(d, ref, tmp / "new" / d.name, tmp)
    home = overlay if overlay is not None else d
    work = tmp / "overlay" if overlay is not None else src
    if overlay is not None:
        shutil.copytree(overlay, work)
    else:
        for f in MODULE_FILES:                       # what is on disk (hand edits included) beats upstream's copy
            if (d / f).is_file():
                shutil.copy2(d / f, src / f)
            elif (src / f).is_file():
                (src / f).unlink()
    res = ad.adopt(src, mid=m.id, name=m.name, repo=m.repo, branch=m.branch,
                   overlay=work if overlay is not None else None, record_checkout=False)
    old_ops = _load_spec(home / ad.OPS_FILE)
    hand_owned = (home / ad.OPS_FILE).is_file() and ad.OPS_MARKER not in _first_line(home / ad.OPS_FILE)
    prune: list[str] = []
    summaries: dict[str, str] = {}
    kept_stale: list[str] = []
    if not hand_owned:                               # upstream dropped a script/route: its operation goes, unless edited
        prune, kept_stale, summaries = _reconcile(m, d, ref, src, tmp, _load_spec(work / ad.OPS_FILE), old_ops)
        _edit(work / ad.OPS_FILE, prune, summaries)
    new_ops = _load_spec(work / ad.OPS_FILE)
    not_adopted: list[dict] = []
    if hand_owned:                                   # kept as it is: say what detection finds that it does not list
        have = {o.id for o in old_ops.ops}
        not_adopted = [_brief(o) for o in dt.detect(src, mid=m.id, name=m.name).ops if o.id not in have]
    return {"operations": diff_specs(old_ops, new_ops),
            "manifest": _changes(_read_manifest(home / ad.MANIFEST_FILE), _read_manifest(work / ad.MANIFEST_FILE),
                                 skip=("checkout.dir",)),
            "ops_hand_owned": hand_owned,
            "manifest_hand_owned": (home / ad.MANIFEST_FILE).is_file() and
            ad.MARKER not in _first_line(home / ad.MANIFEST_FILE),
            "detected_not_in_ops_file": not_adopted, "prune": prune, "summaries": summaries, "kept_stale": kept_stale, "stacks": res.get("stacks"), "notes": res.get("notes") or []}


def refresh(mid: str, *, apply: bool = False) -> dict:
    """Fetch upstream and show (apply=False) or apply (apply=True) the module's refresh. See the module docstring."""
    from abp_modkit import adopt as ad
    m = registry.get(mid)
    d, overlay = _target(m)
    exclude = [] if overlay is not None else [f":(exclude){f}" for f in MODULE_FILES]
    dirty = _git(d, "status", "--porcelain", "--untracked-files=no", "--", ".", *exclude)[1]
    if dirty:
        files = [ln.split(None, 1)[-1] for ln in dirty.splitlines()][:10]
        raise ModuleError(f"{m.name}'s checkout has local changes ({', '.join(files)}); nothing was refreshed. "
                          "Commit or stash them yourself first", code="dirty", status=409)
    branch = _git(d, "rev-parse", "--abbrev-ref", "HEAD")[1]
    if branch == "HEAD":
        raise ModuleError(f"{m.name}'s checkout is on a detached HEAD; check out a branch first", code="detached",
                          status=409)
    _git(d, "fetch", "--quiet", "origin")
    rc, ref = _git(d, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if rc != 0:
        ref = f"origin/{branch}"
        if _git(d, "rev-parse", "--verify", "--quiet", ref, check=False)[0] != 0:
            raise ModuleError(f"upstream has no branch {branch!r}", code="git", status=409)
    can_ff, diverged = _ahead_behind(d, ref)
    if diverged:
        raise ModuleError(f"{m.name}'s branch {branch} has diverged from {ref} (local commits and upstream commits); "
                          "nothing was refreshed. Rebase or merge it yourself", code="diverged", status=409)
    before = _git(d, "rev-parse", "--short", "HEAD")[1]
    target = ref if can_ff else "HEAD"
    commits = _git(d, "log", "--format=%h %s", "-n", "30", f"HEAD..{ref}")[1].splitlines() if can_ff else []
    with tempfile.TemporaryDirectory(prefix="abp-refresh-") as t:
        pv = _preview(m, d, overlay, target, Path(t))
    ops = pv["operations"]
    result: dict[str, Any] = {
        "id": m.id, "applied": False, "dry_run": not apply, "branch": branch, "upstream": ref,
        "from_commit": before, "to_commit": _git(d, "rev-parse", "--short", target)[1],
        "new_commits": len(commits), "commits": commits, "overlay": str(overlay) if overlay else None,
        "operations": ops, "manifest": pv["manifest"], "ops_hand_owned": pv["ops_hand_owned"],
        "manifest_hand_owned": pv["manifest_hand_owned"], "detected_not_in_ops_file": pv["detected_not_in_ops_file"],
        "kept_stale": pv["kept_stale"], "stacks": pv["stacks"], "notes": pv["notes"], "wrote": [],
        "changed": bool(commits or ops["added"] or ops["removed"] or ops["changed"] or ops["service"] or pv["manifest"]),
    }
    if not apply:
        return result
    if can_ff:
        _git(d, "merge", "--ff-only", "--quiet", ref)
    real = ad.adopt(d, mid=m.id, name=m.name, repo=m.repo, branch=m.branch,
                    overlay=overlay, record_checkout=bool(m.checkout_dir))
    if not pv["ops_hand_owned"]:
        _edit((overlay or d) / ad.OPS_FILE, pv["prune"], pv["summaries"])
    registry.modules(refresh=True)
    wrote = list(real.get("wrote") or [])
    if not pv["ops_hand_owned"] and (pv["prune"] or pv["summaries"]) and ad.OPS_FILE not in wrote:
        wrote.append(ad.OPS_FILE)
    result.update(applied=True, dry_run=False, wrote=wrote, kept=real.get("kept") or [])
    return result
