"""Adopt a project as an ABP module: write its abp-module.toml and abp-ops.toml from what `detect` found.

Refreshing (adopting again) keeps what a person changed: an abp-module.toml without the generated marker is never
touched, hand-written summaries survive, ops added by hand stay, and service settings edited by hand win over the
detected ones (a different port, a start command, a health path).
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from abp_modkit import detect as dt
from abp_modkit import spec as sp

MARKER = "# written by abp_modkit"
OPS_MARKER = "(abp_modkit)"          # in the generated abp-ops.toml's first line
OPS_FILE = "abp-ops.toml"
MANIFEST_FILE = "abp-module.toml"


def _q(v: Any) -> str:
    return sp.q(v)


def manifest_text(plan: dt.Plan, repo: str, branch: str = "main") -> str:
    hub = ["{abp_python}", "-m", "abp_modkit", "serve", "--spec", "{repo}/" + OPS_FILE, "--project", "{repo}",
           "--home", "{data}", "--var", "target={target}"]
    mcp = plan.mcp_native or ["{abp_python}", "-m", "abp_modkit", "mcp", "--home", "{data}"]
    lines = [f"{MARKER} (`python -m abp_modkit adopt` refreshes this file; delete this line to keep it as it is)",
             "# How AgenticBotPlatform (ABP) builds, runs and drives this project as a module. Operations: abp-ops.toml.",
             "", "[module]", f"id = {_q(plan.id)}", f"name = {_q(plan.name)}", f"repo = {_q(repo)}",
             f"branch = {_q(branch)}", "api = 1", f"area = {_q(plan.area)}",
             f"description = {_q(plan.description or plan.name)}", "", "[checkout]",
             f"marker = {_q([MANIFEST_FILE, OPS_FILE])}"]
    if plan.requires:
        lines += ["", "[toolchain]", "require = [" + ", ".join(
            "{ " + ", ".join(f"{k} = {_q(v)}" for k, v in r.items()) + " }" for r in plan.requires) + "]"]
    if plan.build:
        lines += ["", "[build]", "steps = [", *[f"  {_q(s)}," for s in plan.build], "]"]
        if plan.outputs:
            lines.append(f"outputs = {_q(list(dict.fromkeys(plan.outputs))[:6])}")
        if plan.build_env:
            lines.append(f"env = {_q(plan.build_env)}")
    lines += ["", "[hub]", "# abp_modkit's hub: the operations in abp-ops.toml (routes, commands, the project's server)",
              f"start = {_q(hub)}", 'control_file = "{data}/control.json"', "start_timeout_s = 30", "", "[mcp]",
              f"stdio = {_q(mcp)}" + ("  # the project's own MCP server" if plan.mcp_native else "")]
    ui = [f"panel = {_q('operations')}"]
    if plan.gui:
        ui.insert(0, f"gui = {_q(plan.gui)}")
    if plan.tui:
        ui.insert(0, f"tui = {_q(plan.tui)}")
    if plan.service and plan.service.web:
        ui.append('web = "service"  # its web UI (abp-ops.toml service.web), shown in a pane once its server runs')
    lines += ["", "[ui]", *ui]
    if plan.service and plan.service.openai:
        lines += ["", "[provider]", "# an OpenAI-compatible API: ABP offers it as a model provider while its server runs",
                  'openai = "service"']
    if sorted(plan.host_os) != ["linux", "macos", "windows"] or plan.host_needs:
        lines += ["", "[host]", f"os = {_q(plan.host_os)}"] + ([f"needs = {_q(plan.host_needs)}"] if plan.host_needs else [])
    if plan.pipeline:
        lines += ["", "[pipeline]", f"run = {_q(plan.pipeline)}"]
    return "\n".join(lines) + "\n"


def _merge(old: sp.Spec, new: sp.Spec) -> sp.Spec:
    """New detection, with what a person changed in the old spec kept."""
    kept = {o.id: o for o in old.ops}
    detected = {o.id for o in new.ops}
    for o in new.ops:
        prev = kept.get(o.id)
        if prev is not None:
            if prev.summary and prev.summary != o.summary:
                o.summary = prev.summary
            if prev.kind == "cmd" and o.kind == "cmd" and prev.inputs:
                o.inputs = {**o.inputs, **prev.inputs}
            for flag in ("mutating_flag", "destructive_flag"):
                if getattr(prev, flag) is not None:
                    setattr(o, flag, getattr(prev, flag))
    new.ops += [o for o in old.ops if o.id not in detected and not o.summary.startswith("[generated]")]
    s, o = new.service, old.service
    for k in ("base_url", "health", "start", "cwd", "env", "ready_timeout_s", "auth", "web", "openai", "description"):
        ov = getattr(o, k)
        if ov not in ("", [], {}, None, 60.0, ".", "none"):
            setattr(s, k, ov)
    return new


def adopt(path: str | Path, *, mid: str = "", name: str = "", repo: str = "", branch: str = "", dry_run: bool = False,
          force: bool = False, description: str = "") -> dict:
    root = Path(path).resolve()
    plan = dt.detect(root, mid=mid, name=name)
    ops_path, man_path = root / OPS_FILE, root / MANIFEST_FILE
    new = sp.Spec(plan.service or sp.Service(plan.id, plan.name), plan.ops)
    wrote: list[str] = []
    kept: list[str] = []
    if ops_path.is_file() and not force:
        try:
            new = _merge(sp.load(ops_path), new)
        except sp.SpecError as e:
            raise sp.SpecError(f"{ops_path} does not load, so it was not refreshed: {e}") from None
    new.service.id, new.service.name = plan.id, plan.name
    if description:
        new.service.description = description
    # one description everywhere: the one given, else the one a person kept in abp-ops.toml, else the README's
    plan.description = new.service.description or plan.description
    ops_text = sp.dump(new)
    sp.parse(__import__("tomllib").loads(ops_text), str(ops_path))          # what we write must load
    if not repo:
        import subprocess
        try:
            repo = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True,
                                  timeout=15).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            repo = ""
        repo = repo or f"https://github.com/LoopyLuci/{root.name}.git"
        if repo.startswith("git@github.com:"):
            repo = "https://github.com/" + repo.split(":", 1)[1]
    man_text = manifest_text(plan, repo, branch or "main")
    # An abp-ops.toml whose first line is not the generated one was taken over by a person (a curated list of
    # operations): it is left exactly as it is, like a manifest without its marker.
    ops_kept = ops_path.is_file() and not force and \
        OPS_MARKER not in (ops_path.read_text(encoding="utf-8").splitlines() or [""])[0]
    if ops_kept:
        hand = sp.load(ops_path)
        kept.append(OPS_FILE)
        ops_text = ops_path.read_text(encoding="utf-8")
        new = hand
        plan.description = description or hand.service.description or plan.description
        plan.service = hand.service
    if not dry_run:
        if not ops_kept and (ops_path.read_text(encoding="utf-8") != ops_text if ops_path.is_file() else True):
            ops_path.write_text(ops_text, encoding="utf-8", newline="\n")
            wrote.append(OPS_FILE)
        if man_path.is_file() and not force and MARKER not in man_path.read_text(encoding="utf-8").splitlines()[0]:
            kept.append(MANIFEST_FILE)
        elif not man_path.is_file() or man_path.read_text(encoding="utf-8") != man_text:
            man_path.write_text(man_text, encoding="utf-8", newline="\n")
            wrote.append(MANIFEST_FILE)
    summary = dt.summary(plan)
    if ops_kept:
        by_kind: dict[str, int] = {}
        for o in new.ops:
            by_kind[o.kind] = by_kind.get(o.kind, 0) + 1
        summary.update(operations=len(new.ops), by_kind=by_kind, server=new.service.base_url,
                       server_start=new.service.start, web=bool(new.service.web), openai=bool(new.service.openai))
    return {**summary, "path": str(root), "repo": repo, "wrote": wrote, "kept": kept,
            "files": {MANIFEST_FILE: man_text, OPS_FILE: ops_text} if dry_run else None,
            "service": {k: v for k, v in asdict(new.service).items() if v not in ("", [], {})}}
