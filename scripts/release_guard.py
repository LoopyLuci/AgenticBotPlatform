"""Pre-flight checks, lock healing, a step journal and retry helpers shared by
scripts/publish_release.py and scripts/local_pipeline.py.

Why this exists: a release used to bump versions, commit, and only THEN find
out (mid-build) that a leftover backend process still held a file in the
staged venv open, leaving a half-made release commit to undo by hand. Every
failure mode we have hit is now either checked BEFORE anything is modified,
healed automatically, retried when it is transient, or rolled back when it
can be.

    preflight checks   -> Check(...) results; anything blocking stops the run
                          before git/GitHub/the build tree is touched
    lock healing       -> find_lock_holders / heal_locks: stop only OUR build
                          outputs' processes and prove the files are free
    PipelineLock       -> one release/pipeline at a time (descendants may re-enter)
    Journal / rollback -> which release steps completed; undo the ones that
                          never left this machine, never touch pushed ones
    run_with_retry     -> transient (lock / network) failures retry with backoff
    smoke_test_bundle  -> boot the staged bundle before it is ever tagged

Nothing here imports the app; it only needs the standard library and psutil
(already a project requirement).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import psutil

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_REL = Path("desktop-app") / "src-tauri"
IS_WINDOWS = sys.platform.startswith("win")

# Source trees whose files must never be silently ignored by .gitignore
# (an unanchored `data/` once hid the whole Android `data` package).
SOURCE_PREFIXES = (
    "android-app/app/src", "bot", "tests", "scripts",
    "desktop-app/ui", "desktop-app/src-tauri/src",
)
_IGNORED_NOISE = ("__pycache__", ".pyc", "/build/", "/.gradle/", "node_modules", ".pytest_cache",
                  "/target/", "/stage/", "/.venv/", ".egg-info")

LOCK_MARKERS = (
    "os error 32", "being used by another process", "access is denied", "permissionerror",
    "text file busy", "the process cannot access the file", "cannot remove", "error 5:",
)
NETWORK_MARKERS = (
    "could not resolve host", "connection reset", "timed out", "tls handshake", "early eof",
    "unexpected eof", "remote end hung up", "unable to access", "bad gateway", "service unavailable",
    "gateway time-out", "rate limit", "temporary failure in name resolution", "network is unreachable",
    "connection refused", "broken pipe", "dial tcp",
)

PIPELINE_PASSED_ENV = "ABP_PIPELINE_PASSED_SHA"


# --------------------------------------------------------------------------- #
# Small process/git helpers
# --------------------------------------------------------------------------- #
@dataclass
class CmdResult:
    rc: int
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _run(cmd: list[str], cwd: Path = ROOT, timeout: Optional[float] = 60,
         stderr_on_success: bool = True) -> CmdResult:
    """Never raises: a missing tool is rc 127, a hang is rc 124. With
    stderr_on_success=False, stderr is only included when the command failed,
    so stdout can be parsed without warnings mixed into it."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return CmdResult(127, f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return CmdResult(124, f"{' '.join(cmd)}: timed out after {timeout}s")
    out, err = p.stdout or "", p.stderr or ""
    return CmdResult(p.returncode, out + err if (stderr_on_success or p.returncode != 0) else out)


def git(*args: str, root: Path = ROOT, timeout: Optional[float] = 60) -> CmdResult:
    """Output is stdout only on success: git prints warnings (CRLF conversion,
    hints) to stderr, and callers parse this output as file lists, SHAs and
    branch names. A CRLF warning once got parsed as a list of changed files."""
    return _run(["git", *args], cwd=root, timeout=timeout, stderr_on_success=False)


def _norm(p: object) -> str:
    s = str(p)
    if s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(os.path.abspath(s))


def _is_under(path: object, dirs: Iterable[str]) -> bool:
    n = _norm(path)
    return any(n == d or n.startswith(d + os.sep) for d in dirs)


def _ancestor_pids() -> set[int]:
    pids: set[int] = set()
    try:
        p: Optional[psutil.Process] = psutil.Process()
        while p is not None:
            pids.add(p.pid)
            p = p.parent()
    except psutil.Error:
        pass
    pids.add(os.getpid())
    return pids


# --------------------------------------------------------------------------- #
# Pre-flight checks
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    fixed: bool = False   # a problem this check repaired by itself
    warn: bool = False    # not blocking, but worth saying

    @property
    def blocking(self) -> bool:
        return not self.ok


def _git_dir(root: Path) -> Path:
    r = git("rev-parse", "--git-dir", root=root)
    p = Path(r.output.strip() or ".git")
    return p if p.is_absolute() else root / p


def check_git_state(root: Path = ROOT, branch: str = "main", fetch: bool = True) -> list[Check]:
    out: list[Check] = []
    gd = _git_dir(root)
    busy = [m for m in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
            if (gd / m).exists()]
    out.append(Check("no git operation in progress", not busy, ", ".join(busy),
                     "finish or abort it (git rebase --abort / git merge --abort)"))

    cur = git("rev-parse", "--abbrev-ref", "HEAD", root=root).output.strip()
    out.append(Check(f"on branch {branch}", cur == branch, f"currently on {cur!r}", f"git switch {branch}"))

    if fetch:
        f = git("fetch", "origin", "--quiet", root=root, timeout=90)
        if not f.ok:
            out.append(Check("origin reachable for fetch", True, f.output.strip()[:200],
                             warn=True))
    behind = git("rev-list", "--count", f"HEAD..origin/{branch}", root=root)
    n = int(behind.output.strip()) if behind.ok and behind.output.strip().isdigit() else 0
    out.append(Check("not behind origin", n == 0, f"{n} commit(s) behind origin/{branch}",
                     f"git pull --rebase origin {branch}"))
    return out


def check_index_lock(root: Path = ROOT) -> Check:
    """A stale .git/index.lock (a crashed git) blocks every commit. Remove it
    only when no git process is alive to own it."""
    lock = _git_dir(root) / "index.lock"
    if not lock.exists():
        return Check("no stale git index lock", True)
    live = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() in ("git", "git.exe"):
                live.append(p.pid)
        except psutil.Error:
            pass
    if live:
        return Check("no stale git index lock", False, f"git is running (pid {live[0]}) and holds index.lock",
                     "wait for it to finish")
    try:
        lock.unlink()
    except OSError as exc:
        return Check("no stale git index lock", False, f"couldn't remove {lock}: {exc}", "delete it by hand")
    return Check("no stale git index lock", True, "removed a leftover index.lock", fixed=True)


def check_clean_tree(root: Path = ROOT) -> Check:
    """Tracked files must match HEAD in CONTENT. Compared with `git diff` (which
    applies the same line-ending conversion as a commit) rather than `git
    status`: after a build, status can flag a file as modified when only its
    timestamp or CRLF/LF form changed and the diff is empty - that phantom once
    aborted a release at the very last step."""
    git("update-index", "-q", "--refresh", root=root)
    r = git("diff", "--name-only", "HEAD", root=root)
    dirty = [ln.strip() for ln in r.output.splitlines() if ln.strip()]
    return Check("tracked files are committed", not dirty and r.ok, "; ".join(dirty[:6]),
                 "commit or stash them — a release commit must contain only the version bump")


def check_ignored_source(root: Path = ROOT, prefixes: Iterable[str] = SOURCE_PREFIXES) -> Check:
    r = git("ls-files", "--others", "--ignored", "--exclude-standard", "--", *prefixes, root=root, timeout=120)
    bad = [ln for ln in r.output.splitlines()
           if ln.strip() and not any(noise in "/" + ln.replace("\\", "/") for noise in _IGNORED_NOISE)]
    return Check("no source files hidden by .gitignore", not bad, ", ".join(bad[:5]),
                 "a .gitignore rule is too broad — anchor it with a leading /")


def _parse_ver(s: str) -> tuple[int, int, int]:
    a, b, c = s.lstrip("v").split(".")
    return int(a), int(b), int(c)


def check_version(version: str, root: Path = ROOT, check_remote: bool = True,
                  allow_existing_tag: bool = False) -> list[Check]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return [Check("version is X.Y.Z", False, repr(version), "e.g. 0.7.28")]
    out = [Check("version is X.Y.Z", True)]
    tag = f"v{version}"
    tags = [t for t in git("tag", "-l", "v*", root=root).output.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    if tag in tags and not allow_existing_tag:
        out.append(Check(f"tag {tag} is unused", False, "already exists locally", "pick a new version"))
    else:
        out.append(Check(f"tag {tag} is unused", True))
    others = [t for t in tags if t != tag]
    newest = max(others, key=_parse_ver) if others else None
    if newest and _parse_ver(version) <= _parse_ver(newest):
        out.append(Check("version is newer than the latest tag", False, f"latest is {newest}",
                         f"use something above {newest}"))
    else:
        out.append(Check("version is newer than the latest tag", True))
    if check_remote and not allow_existing_tag:
        r = git("ls-remote", "--tags", "origin", f"refs/tags/{tag}", root=root, timeout=45)
        if not r.ok:
            out.append(Check("tag not already on origin", True, "couldn't reach origin", warn=True))
        else:
            out.append(Check("tag not already on origin", not r.output.strip(),
                             "origin already has it", "pick a new version"))
    return out


def sync_cargo_lock(version: str, root: Path = ROOT, apply: bool = True) -> Check:
    """Cargo.lock records this package's own version; if it lags the release
    version, cargo rewrites it during the build and leaves the tree dirty.
    Fix it up front (the release commit includes it). With apply=False (a dry
    run) it only reports what it would change."""
    desk = root / DESKTOP_REL
    toml = desk / "Cargo.toml"
    lock = desk / "Cargo.lock"
    if not toml.is_file() or not lock.is_file():
        return Check("Cargo.lock matches the release version", True, "no Cargo.lock", warn=True)
    m = re.search(r'(?m)^name = "([^"]+)"', toml.read_text(encoding="utf-8"))
    if not m:
        return Check("Cargo.lock matches the release version", False, "couldn't read the package name", "check Cargo.toml")
    pat = re.compile(r'(\[\[package\]\]\r?\nname = "' + re.escape(m.group(1)) + r'"\r?\nversion = ")([^"]*)(")')
    text = lock.read_bytes().decode("utf-8")
    hit = pat.search(text)
    if not hit:
        return Check("Cargo.lock matches the release version", False, "package not found in Cargo.lock",
                     "run cargo generate-lockfile")
    if hit.group(2) == version:
        return Check("Cargo.lock matches the release version", True)
    if not apply:
        return Check("Cargo.lock matches the release version", True,
                     f"would update {hit.group(2)} -> {version}", warn=True)
    lock.write_bytes(pat.sub(lambda mm: mm.group(1) + version + mm.group(3), text, count=1).encode("utf-8"))
    return Check("Cargo.lock matches the release version", True, f"updated {hit.group(2)} -> {version}", fixed=True)


def check_tools(root: Path = ROOT) -> list[Check]:
    out = []
    for tool, hint in (("git", "install git"), ("cargo", "install Rust (rustup)"), ("gh", "install the GitHub CLI")):
        out.append(Check(f"{tool} is installed", shutil.which(tool) is not None, hint=hint))
    tauri = _run(["cargo", "tauri", "--version"], cwd=root, timeout=30)
    out.append(Check("cargo-tauri is installed", tauri.ok, tauri.output.strip()[:120], "cargo install tauri-cli"))
    auth = _run(["gh", "auth", "status"], cwd=root, timeout=30)
    out.append(Check("gh is logged in", auth.ok, auth.output.strip()[:160], "gh auth login"))
    gradlew = root / "android-app" / ("gradlew.bat" if IS_WINDOWS else "gradlew")
    out.append(Check("gradle wrapper present", gradlew.is_file(), str(gradlew)))
    return out


def check_disk(root: Path = ROOT, min_gb: float = 5.0) -> Check:
    free = shutil.disk_usage(root).free / 1e9
    return Check(f"at least {min_gb:g} GB free", free >= min_gb, f"{free:.1f} GB free",
                 "free up space (cargo clean, gradle caches)")


def check_network(root: Path = ROOT) -> Check:
    r = git("ls-remote", "--heads", "origin", "HEAD", root=root, timeout=30)
    return Check("GitHub is reachable", r.ok, r.output.strip()[:160], "check your connection")


def check_signing(scripts_dir: Optional[Path] = None) -> Check:
    """The update-signing key must exist and match the public key baked into
    the app — proven now, not after a 10-minute build."""
    sd = str(scripts_dir or Path(__file__).resolve().parent)
    if sd not in sys.path:
        sys.path.insert(0, sd)
    try:
        import update_signing
        key = update_signing.load_private_key()
        if update_signing.public_key_bytes(key) != update_signing.embedded_public_key():
            return Check("update signing key matches the app", False,
                         "the key on disk is not the one embedded in updater.rs",
                         "restore the right key from ~/.abp-release/")
    except Exception as exc:  # noqa: BLE001 - any failure here means "can't sign"
        return Check("update signing key matches the app", False, f"{type(exc).__name__}: {exc}",
                     "restore ~/.abp-release/update_signing_key.pem")
    return Check("update signing key matches the app", True)


def run_preflight(version: str, root: Path = ROOT, *, fetch: bool = True, resume: bool = False) -> list[Check]:
    checks: list[Check] = []
    checks.append(check_index_lock(root))
    checks += check_git_state(root, fetch=fetch)
    checks.append(check_clean_tree(root))
    checks.append(check_ignored_source(root))
    checks += check_version(version, root, check_remote=fetch, allow_existing_tag=resume)
    checks.append(check_disk(root))
    checks += check_tools(root)
    checks.append(check_signing())
    if fetch:
        checks.append(check_network(root))
    return checks


def report(checks: list[Check], out=print) -> list[Check]:
    """Prints a compact table; returns the blocking failures."""
    blocking = []
    for c in checks:
        if c.blocking:
            blocking.append(c)
            out(f"  [FAIL] {c.name}" + (f" - {c.detail}" if c.detail else ""))
            if c.hint:
                out(f"         fix: {c.hint}")
        elif c.fixed:
            out(f"  [fixed] {c.name} - {c.detail}")
        elif c.warn:
            out(f"  [warn] {c.name} - {c.detail}")
        else:
            out(f"  [ok]   {c.name}")
    return blocking


# --------------------------------------------------------------------------- #
# Lock healing
# --------------------------------------------------------------------------- #
# Build products live under these; nothing that is currently *building* runs
# out of them (rustc/cargo run from the toolchain, build scripts from
# target/<profile>/build|deps, which are excluded below).
def _guarded_dirs(root: Path) -> list[str]:
    d = root / DESKTOP_REL
    return [_norm(d / "target"), _norm(d / "stage")]


def _is_build_tool_path(exe: str, root: Path) -> bool:
    n = _norm(exe)
    tgt = _norm(root / DESKTOP_REL / "target")
    if not n.startswith(tgt + os.sep):
        return False
    parts = n[len(tgt) + 1:].split(os.sep)
    return len(parts) >= 2 and parts[1] in ("build", "deps", "incremental", ".fingerprint")


def find_lock_holders(root: Path = ROOT) -> list[psutil.Process]:
    """Processes running out of the repo's built app / staged venv — the ones
    that pin files a rebuild must overwrite. Never our own process tree, and
    never a build tool. Anything installed elsewhere (Program Files) is not
    ours to touch."""
    dirs = _guarded_dirs(root)
    protected = _ancestor_pids()
    found = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = p.info.get("exe")
            if not exe or p.info["pid"] in protected:
                continue
            if _is_under(exe, dirs) and not _is_build_tool_path(exe, root):
                found.append(p)
        except psutil.Error:
            continue
    return found


def stop_processes(procs: list[psutil.Process], grace: float = 6.0) -> list[str]:
    """Stops each process and its children: polite terminate, then kill."""
    protected = _ancestor_pids()
    victims: dict[int, psutil.Process] = {}
    for p in procs:
        try:
            for c in p.children(recursive=True):
                victims[c.pid] = c
            victims[p.pid] = p
        except psutil.Error:
            continue
    for pid in list(victims):
        if pid in protected:
            victims.pop(pid)
    names = []
    for p in victims.values():
        try:
            names.append(f"{p.name()}({p.pid})")
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(list(victims.values()), timeout=grace)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=grace)
    return names


def locked_files(root: Path = ROOT, limit: int = 4000) -> list[str]:
    """Files a build must overwrite that something still holds open. Loaded
    .pyd/.dll/.exe files refuse a write-open on Windows; that's the probe."""
    d = root / DESKTOP_REL
    roots = [d / "stage" / ".venv", d / "target" / "release" / ".venv"]
    files: list[Path] = []
    for base in roots:
        if not base.is_dir():
            continue
        for dirpath, _dirs, names in os.walk(base):
            for n in names:
                if n.lower().endswith((".pyd", ".dll", ".exe")):
                    files.append(Path(dirpath) / n)
            if len(files) >= limit:
                break
    for pattern_dir in (d / "target" / "release", d / "target" / "release" / "bundle" / "nsis"):
        if pattern_dir.is_dir():
            files += [p for p in pattern_dir.iterdir() if p.suffix.lower() == ".exe"]
    locked = []
    for f in files[:limit]:
        try:
            with open(f, "r+b"):
                pass
        except PermissionError:
            locked.append(str(f))
        except OSError:
            pass
    return locked


@dataclass
class HealResult:
    stopped: list[str] = field(default_factory=list)
    still_locked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.still_locked


def heal_locks(root: Path = ROOT, *, wait_s: float = 20.0, sleep: Callable[[float], None] = time.sleep,
               log: Callable[[str], None] = print) -> HealResult:
    """Stop whatever runs out of the built app / staged venv, then wait until
    the files are provably free (or report which stay locked)."""
    holders = find_lock_holders(root)
    res = HealResult()
    if holders:
        res.stopped = stop_processes(holders)
        log(f"  stopped processes holding build outputs: {', '.join(res.stopped)}")
    deadline = time.monotonic() + wait_s
    res.still_locked = locked_files(root)
    while res.still_locked and time.monotonic() < deadline:
        sleep(1.0)
        res.still_locked = locked_files(root)
    return res


# --------------------------------------------------------------------------- #
# One release / pipeline at a time
# --------------------------------------------------------------------------- #
class Busy(RuntimeError):
    pass


class PipelineLock:
    """`.pipeline.lock` holds {pid, started}. A live owner blocks a second
    run; a dead/recycled owner is stale and replaced. A descendant of the
    owner (the pre-push hook a release's `git push` spawns) may re-enter."""

    def __init__(self, path: Path, label: str = ""):
        self.path = Path(path)
        self.label = label
        self._owned = False

    def _owner_alive(self, data: dict) -> bool:
        pid = data.get("pid")
        if not isinstance(pid, int) or not psutil.pid_exists(pid):
            return False
        try:
            started = psutil.Process(pid).create_time()
        except psutil.Error:
            return False
        return abs(started - float(data.get("started", 0))) < 5.0

    def __enter__(self) -> "PipelineLock":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and self._owner_alive(data):
            if data["pid"] in _ancestor_pids():
                return self  # we are running inside the owner's run
            raise Busy(f"another {data.get('label') or 'pipeline'} run is active (pid {data['pid']})")
        me = psutil.Process()
        self.path.write_text(json.dumps({"pid": me.pid, "started": me.create_time(), "label": self.label}),
                             encoding="utf-8")
        self._owned = True
        return self

    def __exit__(self, *exc) -> None:
        if not self._owned:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                self.path.unlink()
        except (OSError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# Step journal + rollback
# --------------------------------------------------------------------------- #
STEPS = ("bumped", "committed", "gated", "built_desktop", "smoke", "built_android",
         "tagged", "pushed", "tag_pushed", "released", "verified")
RELEASE_FILES = ("desktop-app/src-tauri/Cargo.toml", "desktop-app/src-tauri/Cargo.lock",
                 "desktop-app/src-tauri/tauri.conf.json", "android-app/app/build.gradle.kts")


class Journal:
    def __init__(self, path: Path, version: str, base_head: str):
        self.path = Path(path)
        self.version = version
        self.base_head = base_head
        self.steps: list[str] = []
        self.data: dict = {}

    @classmethod
    def load(cls, path: Path) -> Optional["Journal"]:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            j = cls(path, raw["version"], raw["base_head"])
            j.steps = [s for s in raw.get("steps", []) if s in STEPS]
            j.data = dict(raw.get("data", {}))
            return j
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self) -> None:
        self.path.write_text(json.dumps({"version": self.version, "base_head": self.base_head,
                                         "steps": self.steps, "data": self.data}, indent=2), encoding="utf-8")

    def done(self, step: str, **data) -> None:
        if step not in STEPS:
            raise ValueError(step)
        if step not in self.steps:
            self.steps.append(step)
        self.data.update(data)
        self.save()

    def is_done(self, step: str) -> bool:
        return step in self.steps

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def rollback(journal: Journal, root: Path = ROOT) -> list[str]:
    """Undo release steps that never left this machine. Pushed work is never
    rewritten; the caller is told to --resume instead."""
    actions: list[str] = []
    tag = f"v{journal.version}"
    if journal.is_done("bumped") and not journal.is_done("committed"):
        files = [f for f in RELEASE_FILES if (root / f).exists()]
        r = git("checkout", "HEAD", "--", *files, root=root)
        actions.append("restored the uncommitted version bumps" if r.ok
                       else f"COULD NOT restore the version bumps: {r.output.strip()}")
    if journal.is_done("tagged") and not journal.is_done("tag_pushed"):
        r = git("tag", "-d", tag, root=root)
        actions.append(f"deleted local tag {tag}" if r.ok else f"COULD NOT delete local tag {tag}: {r.output.strip()}")
    if journal.is_done("committed") and not journal.is_done("pushed"):
        head = git("rev-parse", "HEAD", root=root).output.strip()
        commit = journal.data.get("release_commit")
        changed = set(git("diff", "--name-only", journal.base_head, head, root=root).output.split())
        if head != commit:
            actions.append("NOT rolled back: HEAD is no longer the release commit (someone committed on top)")
        elif not changed <= set(RELEASE_FILES):
            actions.append("NOT rolled back: the release commit contains more than version bumps: "
                           + ", ".join(sorted(changed - set(RELEASE_FILES))))
        else:
            # Soft-reset then restore ONLY the release files: a hard reset would
            # also throw away any unrelated edit made while the release ran.
            r = git("reset", "--soft", journal.base_head, root=root)
            if r.ok:
                files = [f for f in RELEASE_FILES if (root / f).exists()]
                r = git("restore", f"--source={journal.base_head}", "--staged", "--worktree", "--", *files, root=root)
            actions.append("dropped the unpushed release commit" if r.ok
                           else f"COULD NOT drop the release commit: {r.output.strip()}")
    if journal.is_done("pushed") and not journal.is_done("released"):
        actions.append(f"the branch is already pushed — finish with: publish_release.py {journal.version} ... --resume")
    return actions


# --------------------------------------------------------------------------- #
# Running commands with recovery
# --------------------------------------------------------------------------- #
def classify_failure(output: str) -> str:
    low = output.lower()
    if any(m in low for m in LOCK_MARKERS):
        return "lock"
    if any(m in low for m in NETWORK_MARKERS):
        return "network"
    return "fatal"


def run_cmd(cmd: list[str], *, cwd: Path = ROOT, timeout: Optional[float] = None,
            echo: bool = True, env: Optional[dict] = None) -> CmdResult:
    """Streams output live and keeps it (the tail is what classification and
    error reports use). A hung command is killed, tree and all."""
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", env=env)
    except FileNotFoundError:
        return CmdResult(127, f"{cmd[0]}: not found")
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            for c in psutil.Process(proc.pid).children(recursive=True):
                c.kill()
            proc.kill()
        except psutil.Error:
            pass

    timer = threading.Timer(timeout, _kill) if timeout else None
    if timer:
        timer.start()
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if echo:
                sys.stdout.write(line)
            lines.append(line)
            if len(lines) > 4000:
                del lines[:2000]
        proc.wait()
    finally:
        if timer:
            timer.cancel()
    out = "".join(lines)
    if timed_out.is_set():
        return CmdResult(124, out + f"\n{' '.join(cmd)}: killed after {timeout}s")
    return CmdResult(proc.returncode, out)


def run_with_retry(cmd: "list[str] | Callable[[], list[str]]", *, cwd: Path = ROOT, attempts: int = 3, base_delay: float = 3.0,
                   timeout: Optional[float] = None, heal: Optional[Callable[[], object]] = None,
                   sleep: Callable[[float], None] = time.sleep,
                   runner: Callable[..., CmdResult] = run_cmd,
                   log: Callable[[str], None] = print) -> CmdResult:
    """Retries only failures that are worth retrying: a held file (heal, then
    retry) or a flaky network. A compile/test failure returns immediately.
    `cmd` may be a callable, re-evaluated on every attempt (e.g. "create the
    release, or upload to it if a previous attempt already created it")."""
    res = CmdResult(1, "")
    for attempt in range(1, attempts + 1):
        res = runner(cmd() if callable(cmd) else cmd, cwd=cwd, timeout=timeout)
        if res.ok:
            return res
        kind = classify_failure(res.output)
        if kind == "fatal" or attempt == attempts:
            return res
        delay = base_delay * (2 ** (attempt - 1))
        log(f"  attempt {attempt}/{attempts} failed ({kind}); "
            f"{'releasing held files, ' if kind == 'lock' and heal else ''}retrying in {delay:g}s")
        if kind == "lock" and heal:
            heal()
        sleep(delay)
    return res


# --------------------------------------------------------------------------- #
# Bundle smoke test
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def smoke_test_bundle(stage_dir: Path, *, timeout: float = 90.0, python: Optional[Path] = None,
                      log: Callable[[str], None] = print) -> tuple[bool, str]:
    """Boot the staged installer bundle (its own venv + bot/ copy) against a
    throwaway state dir and a random port, and require /healthz to answer.
    Catches a bundle that ships a missing module or won't start, BEFORE it is
    tagged. Uses a temp ABP_HOME, so it never touches real data or config."""
    stage_dir = Path(stage_dir)
    py = Path(python) if python else stage_dir / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    if not py.is_file() or not (stage_dir / "bot" / "main.py").is_file():
        return False, f"staged bundle is incomplete ({py} / bot/main.py missing)"
    port = _free_port()
    home = Path(tempfile.mkdtemp(prefix="abp-smoke-"))
    env = {k: v for k, v in os.environ.items() if k not in ("DASHBOARD_TOKEN", "PYTHONPATH", "VIRTUAL_ENV")}
    env.update(ABP_HOME=str(home), DASHBOARD_PORT=str(port), DASHBOARD_HOST="127.0.0.1",
               ABP_DISABLE_MDNS="1", PYTHONUTF8="1")
    proc = subprocess.Popen([str(py), "-m", "bot.main"], cwd=stage_dir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    tail: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line)
            del tail[:-60]

    threading.Thread(target=_drain, daemon=True).start()
    ok, detail = False, "timed out waiting for /healthz"
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                detail = f"the bundled app exited with code {proc.returncode}"
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                    body = json.loads(r.read().decode("utf-8"))
                if body.get("status") == "ok":
                    ok, detail = True, "healthz ok"
                    break
            except Exception:  # noqa: BLE001 - not up yet
                pass
            time.sleep(1.0)
    finally:
        try:
            stop_processes([psutil.Process(proc.pid)], grace=5)
        except psutil.Error:
            pass
        shutil.rmtree(home, ignore_errors=True)
    if not ok:
        detail += "\n" + "".join(tail[-25:])
    return ok, detail


# --------------------------------------------------------------------------- #
# Pre-push handshake
# --------------------------------------------------------------------------- #
def head_sha(root: Path = ROOT) -> str:
    return git("rev-parse", "HEAD", root=root).output.strip()


def pipeline_already_passed(root: Path = ROOT, environ: Optional[dict] = None) -> Optional[str]:
    """If the release gate already ran the full pipeline on exactly this
    commit, the pre-push hook has nothing left to verify (and must NOT rebuild
    the app over the installer that is about to be signed and uploaded)."""
    env = os.environ if environ is None else environ
    sha = env.get(PIPELINE_PASSED_ENV, "").strip()
    return sha if sha and sha == head_sha(root) else None
