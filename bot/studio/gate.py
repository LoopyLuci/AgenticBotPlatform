"""The sandbox gate (roadmap GEN-3): a variant is applied to a temporary copy of the UI first, and the targeted
checks run there inside a Sandbox Nervous System cell. Only a variant whose checks all pass reaches the live files.

    1. a temp workspace (under the system temp folder, always removed) gets a copy of the UI trees and of the tests
       that cover the changed files; the variant is applied to that copy exactly as variants.apply() would
       (the twin rule included)
    2. checks, each named in the result:
         validators   the Studio validators (HTML balance, inline and file JavaScript)
         parity       a panel script that exists in both UIs is byte-identical in both
         leak         the public-repo leak guard's rules over the changed files (when scripts/leak_guard.py exists)
         node:<file>  `node --check` on every changed JavaScript file, in a cell
         pytest       the tests mapped from the changed paths, in a "build"-preset cell with a time limit; every
                      failing test is its own entry
    3. the result is kept with the variant (meta["gate"]) and apply_checked() applies only when it passed.

Tests run with cwd = the workspace, so the files they read through `Path(__file__).parent.parent` are the workspace's
copy; `bot` itself is imported from the real install (PYTHONPATH). The live UI is read, never written, until the gate
passes. Config (config/backends.yaml, all optional):

    studio:
      gate:
        enabled: true          # false skips the gate (apply still runs the validators); env ABP_STUDIO_GATE=off too
        timeout_s: 240         # the whole gate; the checks that did not finish count as failed
        memory_mb: 0           # per-process memory cap for the test cell (0 = the preset's)
        always: [tests/test_csp.py]            # run for every change
        map:                                   # extra mapping: a glob of the changed path -> tests
          "desktop-app/ui/*-panel.js": [tests/test_studio.py]
"""
from __future__ import annotations

import dataclasses
import fnmatch
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from bot.envfile import CODE_ROOT
from bot.studio import variants
from bot.studio.variants import StudioError

DEFAULT_TIMEOUT_S = 240.0
DEFAULT_ALWAYS = ("tests/test_csp.py",)
TAIL_LINES = 25
TAIL_CHARS = 1800
_COPY_IGNORE = shutil.ignore_patterns("node_modules", "__pycache__", ".*")


def settings() -> dict:
    cfg = {}
    try:
        from bot.config import config
        cfg = dict(((config.current.get("studio") or {}).get("gate")) or {})
    except Exception:  # noqa: BLE001 - a worker, or a boot before config exists
        pass
    try:
        timeout = float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    off = os.environ.get("ABP_STUDIO_GATE", "").strip().lower() in ("0", "off", "false", "no")
    return {"enabled": bool(cfg.get("enabled", True)) and not off, "timeout_s": max(1.0, timeout),
            "memory_mb": int(cfg.get("memory_mb") or 0),
            "always": [str(x) for x in (cfg.get("always") if cfg.get("always") is not None else DEFAULT_ALWAYS)],
            "map": {str(k): [str(t) for t in (v or [])] for k, v in dict(cfg.get("map") or {}).items()}}


def tests_root() -> Path:
    """Where the tests to copy come from (the install's tests/ folder; absent in a shipped install)."""
    return Path(os.environ.get("ABP_STUDIO_GATE_TESTS") or CODE_ROOT / "tests")


def select_tests(changed: list[str], cfg: dict, troot: Optional[Path] = None) -> list[str]:
    """Test files (as tests/<name>) that cover the changed paths: by name (a changed `x-panel.js` or `x.html` ->
    test_x_panel.py, test_x_page.py), plus the configured always/map lists. Only files that exist."""
    troot = troot or tests_root()
    have = {p.name for p in troot.glob("test_*.py")} if troot.is_dir() else set()
    picked: list[str] = []

    def add(name: str) -> None:
        n = name.split("/")[-1]
        if n in have and f"tests/{n}" not in picked:
            picked.append(f"tests/{n}")

    for r in changed:
        stem = Path(r).stem.lower().replace("-", "_")
        base = stem[:-6] if stem.endswith("_panel") else stem
        for n in (f"test_{stem}.py", f"test_{base}_panel.py", f"test_{base}_page.py"):
            add(n)
        for pattern, tests in cfg["map"].items():
            if fnmatch.fnmatch(r, pattern):
                for t in tests:
                    add(t)
    for t in cfg["always"]:
        add(t)
    if any(Path(r).stem == "studio-panel" for r in changed):
        add("test_studio.py")
    return picked


# ---- the workspace ---------------------------------------------------------------------------------------------------
def _build_workspace(vid: str, ws: Path, tests: list[str], troot: Path) -> list[str]:
    """Copy the UI trees (and the chosen tests) into `ws`, apply the variant there. Returns the changed paths."""
    live = variants.code_root()
    for root in variants.UI_ROOTS:
        if (live / root).is_dir():
            shutil.copytree(live / root, ws / root, ignore=_COPY_IGNORE)
    plan = variants.plan(vid, ws)
    for r, src in plan.items():
        dest = ws / variants.check_rel(r)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    if tests:
        (ws / "tests").mkdir()
        for name in ("conftest.py", "no_windows_plugin.py"):
            if (troot / name).is_file():
                shutil.copy2(troot / name, ws / "tests" / name)
        for t in tests:
            shutil.copy2(troot / Path(t).name, ws / "tests" / Path(t).name)
        if (CODE_ROOT / "pytest.ini").is_file():
            shutil.copy2(CODE_ROOT / "pytest.ini", ws / "pytest.ini")
    return sorted(plan)


# ---- output hygiene --------------------------------------------------------------------------------------------------
_SECRET = re.compile(r"(?i)\b([\w.-]*(?:token|secret|password|passwd|api[_-]?key|authorization)[\w.-]*)\s*[=:]\s*\S+")
_LONG = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def tail(text: str, ws: Optional[Path] = None) -> str:
    """The last few lines of a check's output, with the workspace path hidden and anything secret-shaped masked."""
    text = text or ""
    if ws is not None:
        text = text.replace(str(ws), "<ws>").replace(str(ws).replace("\\", "/"), "<ws>")
    text = _SECRET.sub(lambda m: m.group(1) + "=***", text)
    text = _LONG.sub("***", text)
    out = "\n".join(text.strip().splitlines()[-TAIL_LINES:])
    return out[-TAIL_CHARS:]


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": ok, **({"tail": detail} if detail and not ok else {})}


# ---- the checks ------------------------------------------------------------------------------------------------------
def _validators(vid: str) -> list[dict]:
    from bot.studio import generate
    problems = generate.validate(vid)
    return [_check("validators", not problems, "\n".join(problems))]


def _parity(ws: Path, changed: list[str]) -> list[dict]:
    bad = []
    for r in changed:
        t = variants._twin(r)
        if t and (ws / t).is_file() and (ws / r).is_file() and (ws / t).read_bytes() != (ws / r).read_bytes():
            bad.append(f"{r} and {t} differ (they must be identical)")
    return [_check("parity", not bad, "\n".join(bad))]


def _leak(ws: Path, changed: list[str]) -> list[dict]:
    path = CODE_ROOT / "scripts" / "leak_guard.py"
    if not path.is_file():
        return []
    try:
        spec = importlib.util.spec_from_file_location("studio_gate_leak_guard", path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[spec.name] = mod            # its dataclasses look themselves up here
        try:
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        finally:
            sys.modules.pop(spec.name, None)
        rules = mod._build_rules(None)
        found = []
        for r in changed:
            for f in mod.scan_text(r, (ws / r).read_text(encoding="utf-8", errors="replace"), rules):
                found.append(f"{r}:{getattr(f, 'line', '')} {getattr(f, 'category', '')}")
    except Exception as e:  # noqa: BLE001 - a guard that cannot run must not pass a change silently
        return [_check("leak", False, f"the leak guard could not run: {type(e).__name__}")]
    return [_check("leak", not found, "\n".join(found[:20]))]


def _env(ws: Path, policy) -> dict:
    env = policy.environment()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(CODE_ROOT), env.get("PYTHONPATH", "")]))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ABP_STUDIO_GATE"] = "off"          # a test that applies a variant must not start a gate inside the gate
    for k in ("TEMP", "TMP", "TMPDIR"):
        env[k] = str(ws / ".tmp")
    return env


def _run(policy, argv: list, ws: Path, timeout: float, name: str) -> tuple[Optional[int], str]:
    """One command in a cell of its own (a cell closes when its first process exits). (exit code or None on timeout,
    its combined output)."""
    from bot.sandbox_ns import spawn as sp
    from bot.sandbox_ns.cell import new_cell
    cell = new_cell("build", policy=policy, name=name, owner="studio-gate")
    try:
        done = sp.run(argv, timeout=timeout, cell=cell, cwd=ws, env=_env(ws, policy), name=name, owner="studio-gate",
                      stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired as e:
        out = e.output if isinstance(e.output, str) else (e.output or b"").decode("utf-8", "replace")
        return None, out or ""
    finally:
        cell.close()
    return done.returncode, done.stdout or ""


def _node_checks(policy, ws: Path, changed: list[str], deadline: float) -> list[dict]:
    js = [r for r in changed if r.endswith(".js")]
    if not js:
        return []
    node = shutil.which("node")
    if not node:
        return [_check("node", True)]       # validators already fell back to the balance check
    out = []
    for r in js:
        left = deadline - time.monotonic()
        if left <= 0:
            out.append(_check(f"node:{r}", False, "gate timeout"))
            continue
        code, text = _run(policy, [node, "--check", str(ws / r)], ws, left, "node --check")
        out.append(_check(f"node:{r}", code == 0, "timed out" if code is None else tail(text, ws)))
    return out


_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$", re.M)


def _pytest(policy, ws: Path, tests: list[str], deadline: float) -> list[dict]:
    if not tests:
        return []
    left = deadline - time.monotonic()
    if left <= 0:
        return [_check("pytest", False, "gate timeout")]
    argv = [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider", "-W", "ignore", "-rfE",
            "--tb=short", "--no-header"]
    code, text = _run(policy, argv, ws, left, "pytest (studio gate)")
    if code is None:
        return [_check("pytest", False, f"timed out\n{tail(text, ws)}")]
    if code == 0:
        return [_check("pytest", True)]
    names = _FAILED.findall(text)
    if not names:
        return [_check("pytest", False, tail(text, ws))]
    return [_check(f"pytest:{n}", False, tail((why or "") + "\n" + _section(text, n), ws)) for n, why in names[:20]]


def _section(text: str, nodeid: str) -> str:
    """The short traceback of one failing test, if pytest printed one under its name."""
    short = nodeid.split("::")[-1]
    m = re.search(r"_{3,} " + re.escape(short) + r" _{3,}\n(.*?)(?=\n_{3,} |\n={3,} |\Z)", text, re.S)
    return m.group(1) if m else ""


# ---- the gate --------------------------------------------------------------------------------------------------------
def run(vid: str, *, cfg: Optional[dict] = None) -> dict:
    """Run the gate for a variant. Never touches the live UI. Returns
    {passed, skipped, checks: [{name, ok, tail?}], failed: [names], tests: [...], seconds, at}; the result is also
    stored with the variant."""
    cfg = cfg or settings()
    t0 = time.monotonic()
    result = {"passed": True, "skipped": False, "checks": [], "failed": [], "tests": [], "seconds": 0.0,
              "at": time.time()}
    if not cfg["enabled"]:
        result.update(skipped=True)
        variants.record_gate(vid, result)
        return result
    deadline = t0 + cfg["timeout_s"]
    troot = tests_root()
    ws = Path(tempfile.mkdtemp(prefix="abp-studio-gate-"))
    try:
        planned = sorted(variants.plan(vid))
        tests = select_tests(planned, cfg, troot)
        result["tests"] = tests
        changed = _build_workspace(vid, ws, tests, troot)
        (ws / ".tmp").mkdir()
        checks = _validators(vid) + _parity(ws, changed) + _leak(ws, changed)
        from bot.sandbox_ns import policy as policy_mod
        pol = policy_mod.policy_for("build")
        pol = dataclasses.replace(pol, timeout_s=cfg["timeout_s"],
                                  **({"memory_mb": cfg["memory_mb"]} if cfg["memory_mb"] else {}))
        checks += _node_checks(pol, ws, changed, deadline)
        checks += _pytest(pol, ws, tests, deadline)
        result["checks"] = checks
    except StudioError:
        raise
    except Exception as e:  # noqa: BLE001 - a gate that broke is a gate that stopped the change
        result["checks"].append(_check("gate", False, f"{type(e).__name__}: {tail(str(e), ws)}"))
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    result["failed"] = [c["name"] for c in result["checks"] if not c["ok"]]
    result["passed"] = not result["failed"]
    result["seconds"] = round(time.monotonic() - t0, 2)
    variants.record_gate(vid, result)
    from bot.studio import log
    log.event("gate", vid=vid, passed=result["passed"], failed=result["failed"], seconds=result["seconds"])
    return result


def failure_message(result: dict) -> str:
    bits = []
    for c in result["checks"]:
        if not c["ok"]:
            bits.append(f"{c['name']}" + (f":\n{c['tail']}" if c.get("tail") else ""))
    return "not applied, the sandbox gate failed (" + ", ".join(result["failed"][:8]) + ")\n" + "\n\n".join(bits)[:4000]


def apply_checked(vid: str) -> dict:
    """The validators, then the gate (unless switched off), then variants.apply(). The live files are written only
    when everything passed."""
    from bot.studio import generate
    problems = generate.validate(vid)
    if problems:
        raise StudioError("not applied, it has problems: " + "; ".join(problems[:5]))
    result = run(vid)
    if not result["passed"]:
        raise StudioError(failure_message(result))
    res = variants.apply(vid)
    res["gate"] = {k: result[k] for k in ("passed", "skipped", "seconds")}
    return res
