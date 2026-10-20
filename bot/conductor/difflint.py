"""ML diff-lint: an advisory review of the diff being pushed by the local coder, with fixes kept only when they verify.

    run(root, base, head)     the stage scripts/local_pipeline.py runs next to pytest; returns a Report (never raises)
    parse_findings(text)      the model's JSON answer -> Finding objects (unified-diff patches or {old, new} fixes)
    verify(...)               each suggested patch applied in a throwaway git worktree of `head`, then ruff on the touched files
                              and the affected tests (the branch's changed tests, proof.changed_tests, plus what codeintel says)
    apply_patch / mark        the owner's side: apply one verified patch to the working tree (marks it accepted), or dismiss one

What ruff cannot see is the point: logic slips, wrong API use, missing or weak tests, flaky test patterns, and this repo's own
rules (AGENTS.md). The rules a static check can decide (no feature removed, hot-reload classification, leak guard, tests) come
from the conductor's reviewer (reviewer.review, no model) and are passed to the model as already found, so it is not asked twice.

Only when the ModelMistress hub answers and the GPU lock (custom:gpu0 on the coordination board) is free; the lock is held for the
model call only. With start_hub (the pipeline's run: it stops live ABP, and live ABP's hub goes with it) a hub that is not running
is started for the model call alone (supervisor.start_transient, with every guard of guard.py) and stopped right after it. Local models only: the call goes through llm.transport_for_ref, which refuses any cloud ref. A hard time budget
(ABP_PIPELINE_DIFFLINT_BUDGET_S, default 180 s); a result is cached by the diff's hash. Nothing here changes the commit being
verified or the checkout: patches are applied in a temporary worktree, and only the owner applies one (scripts/difflint.py).

Precision is tracked per category in data/agent/difflint/stats.json (found / verified / accepted / dismissed), so a category can be
promoted to blocking once it proves accurate. This version is advisory: it never fails the pipeline.
"""
from __future__ import annotations

import asyncio
import datetime
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("bot.conductor.difflint")

CATEGORIES = ("logic", "api_misuse", "weak_tests", "flaky_test", "repo_rule")
SEVERITIES = ("low", "medium", "high")
_ALIASES = {"bug": "logic", "logic_error": "logic", "correctness": "logic", "api": "api_misuse", "wrong_api": "api_misuse",
            "tests": "weak_tests", "missing_tests": "weak_tests", "test": "weak_tests", "flaky": "flaky_test", "flake": "flaky_test",
            "rule": "repo_rule", "repo_rules": "repo_rule", "agents_md": "repo_rule", "policy": "repo_rule"}
PROMPT_VERSION = "difflint/1"
GPU_RESOURCE = "custom:gpu0"
AGENT = "abp-pipeline-difflint"
DEFAULT_BUDGET_S = 180.0
DIFF_CAP = 24000
MAX_FINDINGS = 8
MAX_TEST_FILES = 12
_EXCLUDE = (":(exclude)sdk", ":(exclude)docs/api", ":(exclude)*.lock", ":(exclude)*.min.js")

Complete = Callable[..., Awaitable[tuple[str, str]]]          # (prompt, system=, max_tokens=, model=, timeout_s=) -> (text, ref)
Runner = Callable[[str, list[str], Path, float], tuple[bool, str]]   # (kind "ruff"|"pytest", targets, worktree, timeout) -> (ok, out)


@dataclass
class Finding:
    id: str
    file: str
    line: int
    category: str
    severity: str
    explanation: str
    patch: str = ""
    source: str = "model"                 # "model", or "static" (the conductor's reviewer)
    verified: Optional[bool] = None       # None: no patch to verify
    check: str = ""                       # what the verification ran, or why the patch was dropped

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    status: str = "ok"                    # ok | cached | skipped | error
    reason: str = ""
    base: str = ""
    head: str = ""
    diff_hash: str = ""
    model: str = ""
    elapsed_s: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    json_path: str = ""
    patch_path: str = ""
    started_hub: bool = False             # the stage started (and stopped) its own hub for the model call

    @property
    def verified(self) -> list[Finding]:
        return [f for f in self.findings if f.verified]

    def to_dict(self) -> dict:
        return {"version": PROMPT_VERSION, "status": self.status, "reason": self.reason, "base": self.base, "head": self.head,
                "diff_hash": self.diff_hash, "model": self.model, "elapsed_s": self.elapsed_s, "started_hub": self.started_hub,
                "findings": [f.to_dict() for f in self.findings]}

    def summary(self) -> str:
        if self.status == "skipped":
            return f"ML diff-lint skipped: {self.reason}"
        if self.status == "error":
            return f"ML diff-lint did not finish ({self.reason}) - advisory, the pipeline goes on"
        where = f" - {self.json_path}" if self.json_path else ""
        cached = " (cached)" if self.status == "cached" else ""
        own = " (own hub)" if self.started_hub else ""
        return (f"ML diff-lint{cached}: {len(self.findings)} finding(s), {len(self.verified)} verified fix(es)"
                f" in {self.elapsed_s:.0f}s{own}{where}")


def budget_s() -> float:
    try:
        return max(10.0, float(os.environ.get("ABP_PIPELINE_DIFFLINT_BUDGET_S", "") or DEFAULT_BUDGET_S))
    except ValueError:
        return DEFAULT_BUDGET_S


# ------------------------------------------------------------------- git
def _git(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def diff_text(root: Path, base: str, head: str) -> str:
    """The diff the model reviews: generated files (SDK, OpenAPI, lock files) left out."""
    return _git(root, "diff", "-U3", "-M", "--no-color", "--no-ext-diff", f"{base}..{head}", "--", ".", *_EXCLUDE)


def diff_hash(diff: str) -> str:
    return hashlib.sha256((PROMPT_VERSION + "\n" + diff).encode("utf-8", errors="replace")).hexdigest()


def finding_id(category: str, file: str, line: int, explanation: str) -> str:
    key = f"{category}|{file}|{line}|{' '.join(str(explanation).lower().split())}"
    return "dl-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------- parsing
def _clean_path(p: Any) -> str:
    p = str(p or "").strip().replace("\\", "/")
    for prefix in ("./", "a/", "b/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        return ""
    return p[:240]


def _category(value: Any) -> str:
    c = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    c = _ALIASES.get(c, c)
    return c if c in CATEGORIES else "logic"


def _severity(value: Any) -> str:
    s = str(value or "").strip().lower()
    return {"critical": "high", "error": "high", "warning": "medium", "info": "low", "minor": "low"}.get(s, s) \
        if s not in SEVERITIES else s


def make_patch(rel: str, src: str, old: str, new: str) -> str:
    """A unified diff replacing the one exact occurrence of `old` in `src` (the file `rel` at head) with `new`; "" when `old` is
    missing or not unique (a fix that cannot be placed exactly is not offered)."""
    if not old or src.count(old) != 1 or old == new:
        return ""
    after = src.replace(old, new, 1)
    a, b = src.splitlines(keepends=True), after.splitlines(keepends=True)
    out = []
    for ln in difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3):
        out.append(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n")
    return "".join(out)


def _patch_files(patch: str) -> list[str]:
    files = []
    for ln in patch.splitlines():
        if ln.startswith("+++ "):
            p = _clean_path(ln[4:].split("\t")[0])
            if p and p != "dev/null":
                files.append(p)
    return list(dict.fromkeys(files))


def parse_findings(text: str, read: Optional[Callable[[str], Optional[str]]] = None,
                   limit: int = MAX_FINDINGS) -> list[Finding]:
    """Findings from the model's answer: {"findings": [...]} or a bare list, fenced or surrounded by talk. A finding names a file
    and explains itself or it is dropped. A fix is a unified diff ("patch") or {"old", "new"} ("fix"), the latter placed against
    the file as `read(path)` returns it at head. Raises ValueError when the answer holds no JSON at all."""
    from bot.conductor.planner import PlanError, extract_json

    try:
        data = extract_json(text)
    except PlanError:
        m = re.search(r"\[.*\]", str(text or ""), re.S)
        if not m:
            raise ValueError("the answer is not JSON") from None
        try:
            data = json.loads(m.group(0))
        except ValueError as exc:
            raise ValueError("the answer is not JSON") from exc
    items = data.get("findings") if isinstance(data, dict) else data
    out: list[Finding] = []
    seen: set[str] = set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        rel = _clean_path(it.get("file") or it.get("path"))
        why = " ".join(str(it.get("explanation") or it.get("message") or it.get("why") or "").split())[:600]
        if not rel or not why:
            continue
        try:
            line = max(0, int(it.get("line") or 0))
        except (TypeError, ValueError):
            line = 0
        cat = _category(it.get("category"))
        sev = _severity(it.get("severity"))
        patch = str(it.get("patch") or "").strip()
        if patch and "@@" in patch and _patch_files(patch):
            patch = patch.replace("\r\n", "\n") + "\n"
        else:
            patch = ""
            fix = it.get("fix")
            if isinstance(fix, dict) and read is not None:
                src = read(rel)
                if src is not None:
                    patch = make_patch(rel, src, str(fix.get("old") or ""), str(fix.get("new") or ""))
        fid = finding_id(cat, rel, line, why)
        if fid in seen:
            continue
        seen.add(fid)
        out.append(Finding(fid, rel, line, cat, sev if sev in SEVERITIES else "medium", why, patch))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------- static rules (reviewer)
_RULE_CATEGORY = {"tests": "weak_tests"}
_RULE_SEVERITY = {"no_feature_removed": "high", "leak_guard": "high", "hotreload": "high", "no_deletions": "medium"}


def static_findings(root: Path, base: str, head: str) -> list[Finding]:
    """The conductor reviewer's static checks (no model, no ruff: the pipeline runs that) as findings without a patch."""
    from bot.conductor import reviewer

    raw = os.environ.get("ABP_PRIVATE_DIR", "").strip()
    private = Path(os.path.expandvars(raw)).expanduser() if raw else None
    try:
        rep = reviewer.review(root, base, head, run_ruff=False, private_dir=private)
    except Exception as exc:  # noqa: BLE001 - the static rules are a bonus here, not the point
        logger.info("difflint: static review failed: %s", exc)
        return []
    out = []
    for f in rep.findings:
        cat = _RULE_CATEGORY.get(f.rule, "repo_rule")
        msg = f"[{f.rule}] {f.message}"
        out.append(Finding(finding_id(cat, f.file, f.line, msg), f.file, f.line, cat, _RULE_SEVERITY.get(f.rule, "medium"), msg,
                           source="static"))
    return out


# --------------------------------------------------------------- context
def context(root: Path, diff: str, tokens: int = 1800) -> tuple[str, list[str]]:
    """(a small codeintel context for the touched symbols, the test files codeintel says the change affects). Best effort."""
    try:
        from bot.codeintel import tools

        imp = tools.impact(Path(root), diff, max_tokens=700)
    except Exception as exc:  # noqa: BLE001 - no index: the model sees the diff alone
        logger.debug("difflint: impact unavailable: %s", exc)
        return "", []
    data = imp.data or {}
    tests = [t for t in data.get("tests") or [] if isinstance(t, str)]
    parts = [str(imp.text or "").strip()]
    syms = [s for s in data.get("symbols") or [] if isinstance(s, str)][:12]
    if syms:
        from bot.conductor import navctx

        pack = navctx.task_pack(Path(root), "review the change to: " + ", ".join(syms), tokens)
        if pack:
            parts.append(pack)
    return "\n\n".join(p for p in parts if p), tests


SYSTEM = "You are a careful senior reviewer of a Python repository. You report only real problems a linter cannot find. JSON only."

PROMPT = """Review the change below (the diff being pushed). Report only what a linter such as ruff cannot see:
- logic: a wrong condition, off-by-one, unhandled None, wrong return value, a swallowed error, a race
- api_misuse: wrong use of a library or of this repository's own functions (wrong arguments, a blocking call in async code,
  a missing await, a resource never closed)
- weak_tests: new behaviour without a test, a test that asserts nothing meaningful
- flaky_test: an event loop left open, a fixture or patch without try/finally or cleanup, sleeps and timing, real network or
  ports, order dependence between tests
- repo_rule: this repository's rules - every new bot/* module is classified in bot/hotreload.py; no machine paths, user names
  or private project names (the repo is public); never remove a feature (route, command, flag, tool, screen, config key);
  agent swarms run only OS-confined; safety tooling only proposes, it never changes code by itself

Already reported by the static checks (do not repeat these):
{static}

CODE CONTEXT (codeintel: the changed symbols, their callers and tests):
{context}

DIFF (may be cut):
{diff}

Reply with ONLY JSON:
{{"findings": [{{"file": "path/in/repo.py", "line": 12, "category": "logic|api_misuse|weak_tests|flaky_test|repo_rule",
  "severity": "low|medium|high", "explanation": "one or two sentences",
  "fix": {{"old": "the exact lines as they are in the file after the diff", "new": "the replacement lines"}}}}]}}
Leave "fix" out when you are not sure of it. At most {limit} findings; an empty list is a good answer when the change is fine."""


def build_prompt(diff: str, ctx: str, static: list[Finding]) -> str:
    st = "\n".join(f"- {f.file}:{f.line} {f.explanation}" for f in static[:20]) or "(none)"
    return PROMPT.format(static=st, context=(ctx or "(none)")[:9000], diff=diff[:DIFF_CAP], limit=MAX_FINDINGS)


# ----------------------------------------------------------------- gate
@dataclass
class Gate:
    ok: bool
    reason: str = ""
    model: str = ""
    release: Optional[Callable[[], None]] = None
    started_hub: bool = False             # the hub was started for this call (and stops on release)


def _pick_model(models: list[dict]) -> str:
    want = os.environ.get("ABP_PIPELINE_DIFFLINT_MODEL", "").strip()
    if want:
        return want.split("/", 1)[1] if want.startswith("modelmistress/") else want
    ids = [str(m.get("id")) for m in models if m.get("id")]
    coder = [i for i in ids if "coder" in i.lower()]
    return (coder or ids or [""])[0]


def hub_gate() -> tuple[str, str]:
    """(model ref "modelmistress/<name>", "") when the hub answers with a model fit for the job, else ("", why not)."""
    from bot.modelmistress import client, guard

    hub = client.find(refresh=True)
    if hub is None:
        return "", "the ModelMistress hub is not running"
    try:
        models = client.models(hub)
    except Exception as exc:  # noqa: BLE001
        return "", f"the ModelMistress hub did not list its models ({type(exc).__name__})"
    name = _pick_model(models)
    if not name:
        return "", "the ModelMistress hub serves no model"
    try:
        from bot import conductor

        need = int(conductor.settings().get("min_context") or 0)
    except Exception:  # noqa: BLE001
        need = 16384
    problem = guard.context_problem(models, name, need) if need else None
    if problem:
        return "", problem
    return f"modelmistress/{name}", ""


def gpu_gate(lease_s: float, board: Any = None) -> tuple[Optional[Callable[[], None]], str]:
    """(release, "") with custom:gpu0 taken on the coordination board, else (None, who holds it / why it cannot be taken)."""
    from bot import coordination
    from bot.coordination.model import BoardError

    try:
        board = board if board is not None else coordination.get_board()
        res = board.acquire(AGENT, [GPU_RESOURCE], task=None, lease_s=lease_s)
    except BoardError as exc:
        return None, f"the GPU lock {GPU_RESOURCE} is not free ({exc})"
    except Exception as exc:  # noqa: BLE001 - a board that cannot be asked is not a free GPU
        return None, f"the GPU lock {GPU_RESOURCE} could not be checked ({type(exc).__name__}: {exc})"
    fence = res.get("fence")

    def release() -> None:
        try:
            board.release_locks(AGENT, fence)
        except Exception as exc:  # noqa: BLE001 - the lease runs out on its own
            logger.info("difflint: releasing %s failed: %s", GPU_RESOURCE, exc)
    return release, ""


HUB_START_WAIT_S = 45.0
_own_hub_stop: list[Callable[[], None]] = []     # the stop of a hub this stage started and has not stopped yet


def stop_own_hub() -> None:
    """Stop the hub the stage started, if one still runs (the pipeline calls this when it leaves an overrunning stage behind)."""
    while _own_hub_stop:
        try:
            _own_hub_stop.pop()()
        except Exception as exc:  # noqa: BLE001 - its cell dies with this process anyway
            logger.info("difflint: stopping its hub failed: %s", exc)


def default_gate(lease_s: float, *, start_hub: bool = False, start_wait_s: float = HUB_START_WAIT_S) -> Gate:
    ref, why = hub_gate()
    if ref or not start_hub or not why.endswith("is not running"):
        if not ref:
            return Gate(False, why)
        release, why = gpu_gate(lease_s)
        if release is None:
            return Gate(False, why)
        return Gate(True, model=ref, release=release)
    # No hub: start one for this call only, the GPU lock taken first (loading a model is GPU work too).
    release, why = gpu_gate(lease_s)
    if release is None:
        return Gate(False, why)
    from bot.modelmistress import supervisor

    hub, why, stop = supervisor.start_transient(start_wait_s)
    if hub is None or stop is None:
        release()
        return Gate(False, f"the ModelMistress hub is not running and the stage could not start one: {why}")
    _own_hub_stop.append(stop)
    ref, why = hub_gate()
    if not ref:
        stop_own_hub()
        release()
        return Gate(False, why)

    def release_both() -> None:
        try:
            stop_own_hub()
        finally:
            release()
    return Gate(True, model=ref, release=release_both, started_hub=True)


async def hub_complete(prompt: str, *, system: str = "", max_tokens: int = 2500, model: str = "",
                       timeout_s: float = 120.0) -> tuple[str, str]:
    """One call to the ModelMistress hub. Only a modelmistress/ ref; transport_for_ref refuses anything that is not local."""
    from bot.conductor import llm

    if not str(model).startswith("modelmistress/"):
        raise llm.NoLocalModel(f"{model!r} is not a ModelMistress model; the diff-lint uses the local hub only")
    transport, name = llm.transport_for_ref(model)
    resp = await asyncio.wait_for(
        transport.send(model=name, history=[transport.user_message(prompt)], tool_schemas=[], max_tokens=max_tokens,
                       timeout_s=timeout_s, system_prompt=system), timeout=timeout_s + 5)
    return (resp.text or "").strip(), model


# ---------------------------------------------------------- verification
def sandbox_runner(kind: str, targets: list[str], ws: Path, timeout: float) -> tuple[bool, str]:
    """ruff / pytest the way a conductor proof runs them: through the configured sandbox (the patch is model-written code)."""
    from bot.conductor import proof

    try:
        return asyncio.run(proof._execute(kind, targets, "", Path(ws), int(max(5, timeout))))
    except Exception as exc:  # noqa: BLE001 - a timeout or a sandbox that cannot start: not verified
        return False, f"{type(exc).__name__}: {exc}"[:500]


def verify(root: Path, head: str, findings: list[Finding], *, deadline: float, runner: Runner, base: str = "",
           tests: Optional[list[str]] = None) -> None:
    """Set `verified`/`check` on every finding with a patch: applied alone in a throwaway worktree of `head`, then ruff on the
    touched .py files and the affected tests (the branch's changed tests, `tests`, and test files the patch touches) must pass.
    The checkout and the commit are never touched; the worktree is removed afterwards."""
    from bot.conductor import proof

    cands = [f for f in findings if f.patch]
    if not cands:
        return
    tmp = Path(tempfile.mkdtemp(prefix="abp-difflint-"))
    wt = tmp / "wt"
    try:
        _git(root, "worktree", "add", "--detach", str(wt), head, timeout=180)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        for f in cands:
            f.verified, f.check = False, f"not verified: no worktree ({exc})"[:300]
        shutil.rmtree(tmp, ignore_errors=True)
        return
    try:
        changed = proof.changed_tests(wt, base) if base else []
        for f in cands:
            left = deadline - time.monotonic()
            if left < 5:
                f.verified, f.check = False, "not verified: out of time"
                continue
            pfile = tmp / f"{f.id}.patch"
            pfile.write_text(f.patch, encoding="utf-8", newline="\n")
            ap = subprocess.run(["git", "-C", str(wt), "apply", "--recount", "--whitespace=nowarn", str(pfile)],
                                capture_output=True, text=True, timeout=60)
            if ap.returncode != 0:
                f.verified, f.check = False, "dropped: the patch does not apply (" + (ap.stderr.strip()[:200] or "git apply") + ")"
                continue
            try:
                touched = _patch_files(f.patch)
                py = [t for t in touched if t.endswith(".py") and (wt / t).is_file()]
                ran = []
                ok, out = True, ""
                if py:
                    ok, out = runner("ruff", py, wt, deadline - time.monotonic())
                    ran.append(f"ruff on {len(py)} file(s)")
                own = [t for t in touched if t.startswith("tests/") and Path(t).name.startswith("test_") and (wt / t).is_file()]
                targets = [t for t in dict.fromkeys([*changed, *(tests or []), *own]) if (wt / t).is_file()][:MAX_TEST_FILES]
                if ok and targets:
                    left = deadline - time.monotonic()
                    if left < 5:
                        ok, out = False, "out of time before the tests"
                    else:
                        ok, out = runner("pytest", targets, wt, left)
                        ran.append(f"{len(targets)} test file(s)")
                what = " + ".join(ran) or "applied cleanly (nothing to lint or test)"
                f.verified = bool(ok)
                f.check = (f"passed: {what}" if ok else f"dropped: {what} failed: " + " ".join(str(out).split())[-300:])
            finally:
                subprocess.run(["git", "-C", str(wt), "checkout", "--", "."], capture_output=True, timeout=60)
                subprocess.run(["git", "-C", str(wt), "clean", "-fdq"], capture_output=True, timeout=60)
    finally:
        try:
            _git(root, "worktree", "remove", "--force", str(wt), check=False, timeout=120)
            _git(root, "worktree", "prune", check=False, timeout=60)
        except subprocess.SubprocessError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------ outputs and stats
def state_root() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("difflint")


def write_outputs(rep: Report, out_dir: Path, run: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / f"{run}-difflint.json"
    ppath = out_dir / f"{run}-difflint.patch"
    body = []
    for f in rep.verified:
        body.append(f"# {f.id} [{f.category}/{f.severity}] {f.file}:{f.line}: {f.explanation[:200]}\n")
        body.append(f.patch if f.patch.endswith("\n") else f.patch + "\n")
    ppath.write_text("".join(body), encoding="utf-8", newline="\n")
    rep.json_path, rep.patch_path = str(jpath), str(ppath)
    jpath.write_text(json.dumps({**rep.to_dict(), "patch_file": ppath.name}, indent=1), encoding="utf-8")


def _empty_stats() -> dict:
    return {"categories": {c: {"found": 0, "verified": 0, "accepted": 0, "dismissed": 0} for c in CATEGORIES}, "findings": {}}


def load_stats(state: Optional[Path] = None) -> dict:
    path = Path(state or state_root()) / "stats.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("categories"), dict) and isinstance(data.get("findings"), dict):
            for c, row in _empty_stats()["categories"].items():
                data["categories"].setdefault(c, row)
            return data
    except (OSError, ValueError):
        pass
    return _empty_stats()


def _save_stats(data: dict, state: Optional[Path] = None) -> None:
    path = Path(state or state_root()) / "stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def record(findings: list[Finding], run_file: str, state: Optional[Path] = None) -> None:
    """Count the model's findings by category, each id once however many runs report it again."""
    data = load_stats(state)
    for f in findings:
        if f.source != "model" or f.id in data["findings"]:
            continue
        row = data["categories"].setdefault(f.category, {"found": 0, "verified": 0, "accepted": 0, "dismissed": 0})
        row["found"] += 1
        row["verified"] += 1 if f.verified else 0
        data["findings"][f.id] = {"category": f.category, "file": f.file, "line": f.line, "verified": bool(f.verified),
                                  "state": "open", "run": Path(run_file).name if run_file else "",
                                  "seen": datetime.datetime.now().isoformat(timespec="seconds")}
    _save_stats(data, state)


def mark(fid: str, verdict: str, state: Optional[Path] = None) -> dict:
    """The owner's verdict on a finding: "accepted" or "dismissed" (a changed verdict moves the count). Returns the entry."""
    if verdict not in ("accepted", "dismissed"):
        raise ValueError("a verdict is accepted or dismissed")
    data = load_stats(state)
    entry = data["findings"].get(fid)
    if entry is None:
        raise KeyError(f"no finding {fid} in the stats")
    row = data["categories"].setdefault(entry["category"], {"found": 0, "verified": 0, "accepted": 0, "dismissed": 0})
    if entry.get("state") in ("accepted", "dismissed"):
        row[entry["state"]] = max(0, row[entry["state"]] - 1)
    row[verdict] += 1
    entry["state"] = verdict
    _save_stats(data, state)
    return entry


def precision(state: Optional[Path] = None) -> dict[str, Optional[float]]:
    """accepted / (accepted + dismissed) per category; None until the owner has judged one."""
    out = {}
    for c, row in load_stats(state)["categories"].items():
        judged = row.get("accepted", 0) + row.get("dismissed", 0)
        out[c] = round(row.get("accepted", 0) / judged, 3) if judged else None
    return out


def find_finding(fid: str, out_dir: Path) -> tuple[Optional[dict], Optional[Path]]:
    """The newest run file that reports `fid`, and the finding in it."""
    for path in sorted(Path(out_dir).glob("*-difflint.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for f in data.get("findings") or []:
            if f.get("id") == fid:
                return f, path
    return None, None


def apply_patch(fid: str, root: Path, out_dir: Path, state: Optional[Path] = None) -> str:
    """Apply one verified fix to the working tree of `root` for the owner to review and commit; marks the finding accepted.
    Raises ValueError with the reason when it cannot (unknown id, no verified patch, does not apply). Commits nothing."""
    f, _path = find_finding(fid, out_dir)
    if f is None:
        raise ValueError(f"no finding {fid} in {out_dir}")
    if not f.get("patch") or not f.get("verified"):
        raise ValueError(f"{fid} has no verified fix ({f.get('check') or 'no patch suggested'})")
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8", newline="\n") as fh:
        fh.write(f["patch"])
        pfile = fh.name
    try:
        for args in (["--check"], []):
            proc = subprocess.run(["git", "-C", str(root), "apply", "--recount", "--whitespace=nowarn", *args, pfile],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise ValueError(f"{fid} does not apply to the working tree: {proc.stderr.strip()[:300]}")
    finally:
        os.unlink(pfile)
    try:
        mark(fid, "accepted", state)
    except KeyError:
        pass
    return ", ".join(_patch_files(f["patch"]))


# --------------------------------------------------------------- the run
def run(root: Path, base: str, head: str = "HEAD", *, budget: Optional[float] = None, complete: Optional[Complete] = None,
        gate: Optional[Callable[[float], Gate]] = None, runner: Optional[Runner] = None, out_dir: Optional[Path] = None,
        state: Optional[Path] = None, run_name: str = "", use_cache: bool = True, with_static: bool = True,
        with_context: bool = True, start_hub: bool = False) -> Report:
    """The advisory stage: review base..head with the local coder, verify the suggested fixes, write the run's JSON and .patch
    and update the stats. Never raises: a skip or a failure is a Report with status "skipped" or "error". start_hub: when no hub
    answers, start one for the model call (within the budget) and stop it after."""
    t0 = time.monotonic()
    limit = float(budget if budget is not None else budget_s())
    deadline = t0 + limit
    root = Path(root)
    out_dir = Path(out_dir) if out_dir else root / "logs" / "local_pipeline"
    run_name = run_name or f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    rep = Report()
    try:
        rep.base = _git(root, "rev-parse", base).strip()
        rep.head = _git(root, "rev-parse", head).strip()
        diff = diff_text(root, rep.base, rep.head)
        if not diff.strip():
            rep.status, rep.reason = "skipped", "nothing to review in this diff"
            return rep
        rep.diff_hash = diff_hash(diff)
        cache_file = Path(state or state_root()) / "cache" / f"{rep.diff_hash[:32]}.json"
        if use_cache and cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                rep.findings = [Finding(**{k: v for k, v in f.items() if k in Finding.__dataclass_fields__})
                                for f in cached.get("findings") or []]
                rep.model, rep.status = str(cached.get("model") or ""), "cached"
                rep.elapsed_s = round(time.monotonic() - t0, 1)
                write_outputs(rep, out_dir, run_name)
                return rep
            except (OSError, ValueError, TypeError):
                pass
        if gate is not None:
            g = gate(limit + 30)
        else:
            g = default_gate(limit + 30, start_hub=start_hub, start_wait_s=min(HUB_START_WAIT_S, max(5.0, limit / 4)))
        if not g.ok:
            rep.status, rep.reason = "skipped", g.reason
            return rep
        rep.model = g.model
        rep.started_hub = g.started_hub
        try:
            static = static_findings(root, rep.base, rep.head) if with_static else []
            ctx, tests = context(root, diff) if with_context else ("", [])
            left = deadline - time.monotonic()
            if left < 2:
                raise TimeoutError("no time left for the model")
            ask = complete or hub_complete
            text, ref = asyncio.run(asyncio.wait_for(
                ask(build_prompt(diff, ctx, static), system=SYSTEM, max_tokens=3000, model=g.model,
                    timeout_s=max(1.0, min(left - 1, limit * 0.65))), timeout=max(1.0, left)))
            rep.model = ref or g.model
        finally:
            if g.release:
                g.release()

        def read(rel: str) -> Optional[str]:
            proc = subprocess.run(["git", "-C", str(root), "show", f"{rep.head}:{rel}"], capture_output=True, timeout=60)
            return proc.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n") if proc.returncode == 0 else None

        found = parse_findings(text, read)
        verify(root, rep.head, found, deadline=deadline, runner=runner or sandbox_runner, base=rep.base, tests=tests)
        rep.findings = static + found
        rep.elapsed_s = round(time.monotonic() - t0, 1)
        write_outputs(rep, out_dir, run_name)
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(rep.to_dict(), indent=1), encoding="utf-8")
        except OSError:
            pass
        record(rep.findings, rep.json_path, state)
        return rep
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 - advisory: report, never raise
        rep.status = "error"
        rep.reason = ("over its time budget" if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                      else f"{type(exc).__name__}: {exc}")[:300]
        return rep
    finally:
        rep.elapsed_s = round(time.monotonic() - t0, 1)
