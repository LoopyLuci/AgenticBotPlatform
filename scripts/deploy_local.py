#!/usr/bin/env python
"""Deploy the checkout you are standing in onto the app you actually run.

Why this exists: `cargo tauri build` writes its output where CARGO_TARGET_DIR
says, not necessarily where the app is launched from. With
CARGO_TARGET_DIR=X:/cargo-target, Tauri copies every bundle.resources entry
into X:/cargo-target/release/, while the exe the person runs lives in
<checkout>/desktop-app/src-tauri/target/release/. A release build runs the
Python BUNDLED next to its own exe (desktop-app/src-tauri/src/lib.rs's
resolve_paths), so that folder's copy of bot/ is the code that actually
executes. Restarting it therefore reloads whatever was mirrored into it
whenever that was — which is how a running app kept serving the Python
from days earlier while every restart "succeeded". This script is the
automatic, safe version of doing that by hand: build, stop, install every
bundled resource AND the exe into the folder the app is launched from,
restart it the way the user's own session would, and then prove the app
that came back is the app that was just built.

    python scripts/deploy_local.py              # build (or reuse), install, restart, verify
    python scripts/deploy_local.py --dry-run    # print exactly what it would do, touch nothing
    python scripts/deploy_local.py --no-build   # install an existing build as-is

The resource list is read from tauri.conf.json's bundle.resources, never
written here, so adding a resource to the app can't leave the deploy
behind the build.

Two rules it will not break:

* The install folder's OWN state is never touched. `.env`, `data/` and
  `logs/` belong to the install, not to the build. `config/` is subtler:
  when the bundle sits inside a checkout, bot/envfile.py's
  _checkout_behind_build_output() pins the state root to that checkout, so
  the running app reads the CHECKOUT's live config/backends.yaml and the
  copy bundled next to the exe is dead weight — overwriting it would throw
  away the person's own settings for nothing. So state-owned destinations
  are skipped, loudly, whenever the state root is somewhere else; they are
  only installed into a folder that IS the install's own state root.

* A bundle deployed outside a checkout has its own .env/config/data, and
  replacing its files is replacing a real install's state, so a target
  folder outside every checkout is refused unless it was asked for by name
  (--install-dir plus --outside-checkout).

If verification fails, the files that were just installed are put back from
the one previous copy this keeps next to the install folder, and the app is
started again on those. scripts/local_pipeline.py's deploy step is this
same code, so `git push` deploys through exactly this path.

Only the standard library and psutil (already a project requirement).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import release_guard  # noqa: E402  (shared tree-killing helpers: run_cmd / stop_processes)

# This script is not always started from a terminal: the pre-push hook
# (scripts/git-hooks/pre-push) is exec'd by git.exe, and anything that starts
# git.exe without a console leaves git.exe, the hook's bash and therefore
# THIS process with no console at all. Every console program started after
# that allocates and *shows* a console of its own - a blank window on the
# person's desktop, from cargo or git of all things. bot/sandbox_ns/guard.py
# fixes that once, process-wide, by rewriting subprocess.Popen's creation
# flags; a no-op off Windows. See docs/sandbox-nervous-system.md.
from bot.sandbox_ns import guard as _sandbox_guard  # noqa: E402

_sandbox_guard.install()

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_REL = Path("desktop-app") / "src-tauri"
# <checkout>/desktop-app/src-tauri/target/<profile> - the shape bot/envfile.py's
# _checkout_behind_build_output() recognises as "this bundle has a checkout
# behind it", and therefore the shape whose state root is NOT the bundle.
TAURI_TARGET_TAIL = ("desktop-app", "src-tauri", "target")
IS_WINDOWS = sys.platform.startswith("win")

EXE_NAME = "agentic-bot-platform.exe" if IS_WINDOWS else "agentic-bot-platform"
BUILD_PROFILE = "release"
# A bundle carries the state root's own files; a build never does.
STATE_ENTRIES = (".env", "data", "logs", "config")
# The one previous copy lives beside the install folder, so it can never be
# mistaken for one of the app's own directories, and a rollback can put a
# whole resource (a directory tree, not just a file) back.
PREVIOUS_SUFFIX = ".previous"
PREVIOUS_MANIFEST = ".abp_deploy_previous.json"
# The bundle's own build stamp (scripts/stage_bundle.py writes it; it ships
# as a bundle.resources entry), which is how the running app can say what
# commit it was built from.
BUILD_STAMP = ".abp_build.json"

INSTALL_DIR_ENV = "ABP_INSTALL_DIR"
OUTSIDE_CHECKOUT_ENV = "ABP_DEPLOY_OUTSIDE_CHECKOUT"
DEFAULT_PORT = 8787
BUILD_TIMEOUT = 45 * 60
VERIFY_TIMEOUT = 300.0


class DeployError(RuntimeError):
    """Anything that makes the deploy unsafe or impossible. Reported, never
    a traceback: this is a command a person runs, not a library call."""


def venv_python(bundle_dir: Path) -> Path:
    return bundle_dir / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")



# --------------------------------------------------------------------------- #
# Where everything is
# --------------------------------------------------------------------------- #
def build_dir(root: Path = ROOT) -> Path:
    """Where cargo/tauri put their output: CARGO_TARGET_DIR when set, else
    the in-checkout target/ it defaults to. This is the entire bug this
    script exists for, so it is read from the environment rather than
    assumed."""
    override = (os.environ.get("CARGO_TARGET_DIR") or "").strip()
    return Path(override).expanduser() if override else root / DESKTOP_REL / "target"


def build_profile_dir(root: Path = ROOT) -> Path:
    return build_dir(root) / BUILD_PROFILE


def default_install_dir(root: Path = ROOT) -> Path:
    """The folder inside the checkout the app is launched from — where the
    exe and its bundled resources have always lived, and where it must keep
    living for `resolve_paths` to find the Python next to the exe."""
    return root / DESKTOP_REL / "target" / BUILD_PROFILE


def checkout_of(path: Path) -> Optional[Path]:

    """The ABP checkout `path` is inside, or None. Recognised by the things
    that make a directory a checkout and not a coincidence of names."""
    for candidate in [Path(path).resolve(), *Path(path).resolve().parents]:
        if (candidate / "bot" / "main.py").is_file() and (candidate / DESKTOP_REL / "tauri.conf.json").is_file():
            return candidate
    return None


def state_root(install_dir: Path, environ: Optional[dict] = None) -> Path:
    """Where the installed bundle will read and write its own state —
    bot/envfile.py's resolve_roots() rule, reimplemented here because this
    script must be able to ask the question about a folder before anything
    is copied into it (and without importing the app). A bundle running from
    <checkout>/desktop-app/src-tauri/target/<profile> resolves to that
    checkout; anything else resolves to itself, unless ABP_HOME says
    otherwise."""
    environ = os.environ if environ is None else environ
    resolved = Path(install_dir).resolve()
    code_root = resolved
    if len(resolved.parts) >= 5 and resolved.parts[-4:-1] == TAURI_TARGET_TAIL:
        checkout = resolved.parents[3]
        if (checkout / "bot" / "main.py").is_file():
            code_root = checkout
    home = (environ.get("ABP_HOME") or "").strip()
    if not home:
        return code_root
    return Path(os.path.expandvars(home)).expanduser().resolve()


def state_owned(rel: str) -> bool:
    """Whether this destination belongs to an install's own state rather
    than to a build. tauri.conf.json bundles exactly one of them today
    (config/backends.yaml, the committed routing defaults); this is the
    check that keeps a deploy from putting a build's copy over a live one."""
    return rel.replace("\\", "/").split("/", 1)[0] in STATE_ENTRIES


def as_dest(value: str) -> str:
    """A bundle.resources destination as a path relative to the install
    folder. Only "./" is stripped - str.lstrip("./") would eat the leading
    dot of ".venv", which is one of the resources."""
    rel = str(value).replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def check_install_dir(install_dir: Path, outside_ok: bool = False) -> Optional[str]:
    """Why this folder must not be deployed into, or None if it may be.
    A folder that is inside no checkout at all holds a real install's own
    state, so replacing its files needs the person to have said so."""
    if checkout_of(install_dir) is not None:
        return None
    if outside_ok or (os.environ.get(OUTSIDE_CHECKOUT_ENV) or "").strip() in ("1", "true", "yes"):
        return None
    return (f"{install_dir} is not inside a checkout. A bundle there is its own install, with its own "
            f".env/config/data; installing over it replaces a real install's state. Pass --install-dir "
            f"{install_dir} --outside-checkout if that is what you want.")


def resolve_install_dir(root: Path = ROOT, install_dir: Optional[str] = None) -> Path:
    chosen = install_dir or (os.environ.get(INSTALL_DIR_ENV) or "").strip()
    return Path(chosen).expanduser().resolve() if chosen else default_install_dir(root).resolve()


# --------------------------------------------------------------------------- #
# The plan: what tauri.conf.json says ships, and where it goes
# --------------------------------------------------------------------------- #
def read_resources(root: Path = ROOT) -> dict[str, str]:
    """tauri.conf.json's bundle.resources as {source: destination}. Both
    forms the schema allows are read: the mapping this project uses
    ({"stage/bot": "bot"}) and a plain list, where the destination is the
    source's own name. Read every time, never cached or hardcoded — a
    resource added to the app must never be missing from the deploy."""
    conf = root / DESKTOP_REL / "tauri.conf.json"
    try:
        raw = json.loads(conf.read_text(encoding="utf-8")).get("bundle", {}).get("resources", {})
    except (OSError, ValueError) as exc:
        raise DeployError(f"could not read {conf}: {exc}") from exc
    if isinstance(raw, dict):
        entries = {str(k): str(v) for k, v in raw.items()}
    elif isinstance(raw, list):
        entries = {str(k): str(k).replace("\\", "/").rstrip("/").split("/")[-1] for k in raw}
    else:
        raise DeployError(f"{conf}: bundle.resources must be an object or a list, not {type(raw).__name__}")
    if not entries:
        raise DeployError(f"{conf} declares no bundle.resources — there is nothing to deploy")
    return entries


@dataclass(frozen=True)
class Resource:
    """One thing the build produced and the install folder must hold."""

    rel: str                       # destination, relative to the install folder, forward slashes
    source: Path                   # as cargo/tauri left it in the build's profile directory
    dest: Path                     # as the app will find it in the install folder


@dataclass
class Plan:
    install: list[Resource] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (destination, why)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def plan_install(root: Path, build_profile: Path, install_dir: Path, environ: Optional[dict] = None,
                 allow_missing: bool = False) -> Plan:
    """Every bundle.resources destination, plus the app binary, resolved
    against the build that was just produced and the folder the app runs
    from. Exe last: it is the one file that is always replaced, and a
    failure to copy the Python code must not leave a new exe pointing at
    the old code.

    `allow_missing` plans a destination the build has not produced yet,
    which only a --dry run may do: it wants to print the whole plan on a
    machine where nothing has been built, not to call that a failure."""
    state = state_root(install_dir, environ)
    plan = Plan()
    for _source, dest in read_resources(root).items():
        rel = as_dest(dest)
        if not rel or Path(rel).is_absolute() or ".." in rel.split("/"):
            raise DeployError(f"tauri.conf.json bundles {dest!r} outside the install folder — refusing to deploy it")
        if state_owned(rel):
            if state != Path(install_dir).resolve():
                plan.skipped.append((rel, f"the app reads the live copy at {state / rel}"))
                continue
            if rel.split("/", 1)[0] in (".env", "data", "logs"):
                plan.skipped.append((rel, "an install's own .env/data/logs is never replaced by a deploy"))
                continue
        source = build_profile / rel
        if not source.exists():
            plan.missing.append(rel)
            if not allow_missing:
                continue
        plan.install.append(Resource(rel=rel, source=source, dest=Path(install_dir) / rel))
    exe = Resource(rel=EXE_NAME, source=build_profile / EXE_NAME, dest=Path(install_dir) / EXE_NAME)
    if exe.source.is_file():
        plan.install.append(exe)
    elif allow_missing:
        plan.install.append(exe)
    else:
        plan.missing.append(EXE_NAME)
    return plan



# --------------------------------------------------------------------------- #
# Install and roll back
# --------------------------------------------------------------------------- #
def previous_dir(install_dir: Path) -> Path:
    return Path(install_dir).with_name(Path(install_dir).name + PREVIOUS_SUFFIX)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def install_resources(plan: Plan, install_dir: Path, dry_run: bool = False, log: Callable[[str], None] = print) -> Path:
    """Copies every planned resource into the install folder, first moving
    whatever is there now into a single previous copy kept beside it. The
    manifest records which destinations had something before, so a rollback
    can tell "restore this" from "delete what this deploy created"."""
    prev = previous_dir(install_dir)
    if dry_run:
        log(f"  would keep one previous copy at {prev}")
        for r in plan.install:
            log(f"  would install {r.dest} (from {r.source}, "
                f"{'replacing what is there' if r.dest.exists() else 'new'})")
        return prev

    if prev.exists():
        shutil.rmtree(prev)
    install_dir.mkdir(parents=True, exist_ok=True)
    had_previous: list[str] = []
    deployed: list[str] = []

    def _record() -> None:
        prev.mkdir(parents=True, exist_ok=True)
        (prev / PREVIOUS_MANIFEST).write_text(
            json.dumps({"deployed": deployed, "had_previous": had_previous}, indent=2), encoding="utf-8")

    _record()
    for r in plan.install:
        if r.dest.exists():
            backup = prev / r.rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(r.dest), str(backup))
            had_previous.append(r.rel)
        # Recorded BEFORE the copy, so a copy that dies half way through is
        # still something a rollback can put back: it removes whatever
        # landed there and restores the previous file.
        deployed.append(r.rel)
        _record()
        r.dest.parent.mkdir(parents=True, exist_ok=True)
        if r.source.is_dir():
            shutil.copytree(r.source, r.dest)
        else:
            shutil.copy2(r.source, r.dest)
        log(f"  installed {r.rel}")
    _record()
    return prev


def rollback(install_dir: Path, log: Callable[[str], None] = print) -> list[str]:
    """Puts the previous copy back over everything this deploy installed:
    restored where there was something before, removed where the deploy
    created it. Returns what it restored (empty when there was nothing to
    roll back to, which is the case a rollback must still report honestly
    rather than claim success)."""
    prev = previous_dir(install_dir)
    try:
        manifest = json.loads((prev / PREVIOUS_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeployError(f"no usable previous copy to roll back to ({prev}): {exc}") from exc
    restored: list[str] = []
    for rel in manifest.get("deployed", []):
        dest = install_dir / rel
        _remove(dest)
        backup = prev / rel
        if rel in manifest.get("had_previous", []) and backup.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(dest))
            restored.append(rel)
            log(f"  rolled back {rel}")
        else:
            log(f"  removed {rel} (it did not exist before this deploy)")
    return restored


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
# Everything that goes into a bundle. Used to decide whether the build in
# the profile directory is already up to date, so a deploy can skip a
# multi-minute cargo run without ever skipping a change.
def _newest_mtime(paths: Iterable[Path]) -> float:
    newest = 0.0
    for base in paths:
        if not base.exists():
            continue
        if base.is_file():
            newest = max(newest, base.stat().st_mtime)
            continue
        for dirpath, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in names:
                if name.endswith((".pyc", ".pyo")):
                    continue
                try:
                    newest = max(newest, (Path(dirpath) / name).stat().st_mtime)
                except OSError:
                    continue
    return newest


def build_inputs(root: Path = ROOT) -> list[Path]:
    return [
        root / p for p in ("bot", "abp_cicd", "abp_run", "abp_acp", "abp_agenteval", "abp_toolkit", "abp_modkit",
                           "catalog", "vendor/ssh_toolkit", DESKTOP_REL / "src", DESKTOP_REL / "resources",
                           DESKTOP_REL / "build.rs")
    ] + [
        root / "config" / "backends.yaml",
        root / "requirements.txt",
        root / "requirements.lock",
        root / DESKTOP_REL / "tauri.conf.json",
        root / DESKTOP_REL / "Cargo.toml",
        root / DESKTOP_REL / "Cargo.lock",
    ]


def build_is_current(root: Path = ROOT, profile: Optional[Path] = None) -> bool:
    """Whether the build already in the profile directory is at least as
    new as every input that goes into it. Only true when the exe and every
    bundle resource are actually there — "the sources are older" is not
    enough if the build was interrupted."""
    profile = profile or build_profile_dir(root)
    exe = profile / EXE_NAME
    if not exe.is_file():
        return False
    resources = read_resources(root)
    for dest in resources.values():
        if not (profile / as_dest(dest)).exists():
            return False
    built_at = exe.stat().st_mtime
    return _newest_mtime(build_inputs(root)) <= built_at


def build(root: Path = ROOT, force: bool = False, dry_run: bool = False,
          log: Callable[[str], None] = print) -> bool:
    """`cargo tauri build`, or nothing at all when the profile directory
    already holds a build newer than every input. Returns whether a build
    ran."""
    if not shutil.which("cargo"):
        raise DeployError("cargo is not on PATH — install Rust (https://rustup.rs) or pass --no-build "
                          "to install an existing build as-is")
    if not force and build_is_current(root):
        log(f"  reusing the up-to-date build in {build_profile_dir(root)} (nothing is newer than it)")
        return False
    cmd = ["cargo", "tauri", "build"]
    if dry_run:
        log(f"  would run {' '.join(cmd)} in {root / DESKTOP_REL}")
        return True
    log(f"  {' '.join(cmd)} (this is the slow part)")
    # run_cmd() streams and keeps the tail, and kills the whole process tree
    # if it hangs — a cargo build that outlives its timeout must not leave
    # rustc behind holding the files the install needs.
    res = release_guard.run_cmd(cmd, cwd=root / DESKTOP_REL, timeout=BUILD_TIMEOUT, echo=True)
    if not res.ok:
        raise DeployError(f"{' '.join(cmd)} failed:\n{res.output[-4000:]}")
    return True


# --------------------------------------------------------------------------- #
# Stop, start
# --------------------------------------------------------------------------- #
def _ancestor_pids() -> set[int]:
    pids: set[int] = {os.getpid()}
    try:
        proc: Optional[psutil.Process] = psutil.Process()
        while proc is not None:
            pids.add(proc.pid)
            proc = proc.parent()
    except psutil.Error:
        pass
    return pids


def install_processes(install_dir: Path) -> list[psutil.Process]:
    """Every process running out of the install folder — the app, the
    backend it spawned, and an MCP server a client left running from the
    same bundled venv. Matched by the executable's own path, so an orphan
    whose parent exe is already gone is still found (that orphan is
    exactly what pins .pyd/.dll files and makes the copy fail)."""
    prefix = os.path.normcase(os.path.abspath(str(install_dir))) + os.sep
    protected = _ancestor_pids()
    found = []
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            exe = proc.info.get("exe")
            if not exe or proc.info["pid"] in protected:
                continue
            if os.path.normcase(os.path.abspath(exe)).startswith(prefix):
                found.append(proc)
        except psutil.Error:
            continue
    return found


def stop_app(install_dir: Path, dry_run: bool = False, log: Callable[[str], None] = print) -> list[str]:
    """Stops whatever is running out of the install folder, politely first
    (a Tauri app's WM_CLOSE handler runs its real shutdown: the backend's
    stop event, task cancellation, an audit entry) and then by tree."""
    running = install_processes(install_dir)
    if not running:
        log("  nothing is running out of the install folder")
        return []
    names = [f"{p.name()}({p.pid})" for p in running]
    if dry_run:
        log(f"  would stop {', '.join(names)}")
        return names
    log(f"  stopping {', '.join(names)}")
    return release_guard.stop_processes(running)


def locked_paths(plan: Plan, install_dir: Path) -> list[str]:
    """The planned destinations something still holds open. A loaded
    .pyd/.dll/.exe refuses a write-open on Windows, which is the probe
    release_guard.locked_files() already established as the reliable one.

    Only the bundled venv is walked, and only so far: it is the one resource
    that holds compiled extension modules, and it is the one that really
    does get held - by an MCP server a client left running out of the same
    install, or by an orphaned backend whose parent exe is already gone.
    Walking every resource's whole tree would cost seconds per poll for
    directories that cannot hold anything."""
    candidates = [r.dest for r in plan.install if r.source.is_file()]
    for r in plan.install:
        if r.dest.name == ".venv" and r.dest.is_dir():
            for dirpath, _dirs, names in os.walk(r.dest):
                candidates += [Path(dirpath) / n for n in names if n.lower().endswith((".pyd", ".dll", ".exe"))]
                if len(candidates) >= 4000:
                    break
    locked: list[str] = []
    for path in candidates:
        try:
            with open(path, "r+b"):
                pass
        except PermissionError:
            locked.append(str(path))
        except OSError:
            pass
    return locked


def _spawn_flags() -> int:
    """The creation flags every spawn from here uses: a hidden console it
    owns, so its own children stay invisible too.

    Never DETACHED_PROCESS, and that is not a style preference: a
    console-less child makes the first console program it starts allocate and
    SHOW a window of its own — measured on this machine, and the reason
    bot/sandbox_ns/guard.py exists at all."""
    from bot.sandbox_ns import guard

    return guard.CREATE_NO_WINDOW if IS_WINDOWS else 0


def _popen(cmd: list[str], **kwargs) -> subprocess.Popen:
    if IS_WINDOWS:
        kwargs.setdefault("creationflags", _spawn_flags())
    return subprocess.Popen(cmd, **kwargs)


def launch_command(exe: Path) -> list[str]:
    """How the app is started. On Windows that is explorer.exe, not the exe
    itself: the shell hands it the user's own interactive environment (their
    PATH, their mapped drives, the session's variables), exactly as a
    double-click would, and it is already windowless. A direct spawn from a
    terminal or an agent inherits THAT process's environment instead, which
    is how a deploy ends up launching an app that cannot find half its
    tools."""
    return ["explorer.exe", str(exe)] if IS_WINDOWS else [str(exe)]


def start_app(exe: Path, *, dry_run: bool = False, launcher: Optional[Callable[[Path], object]] = None,
              wait_s: float = 12.0, log: Callable[[str], None] = print) -> Optional[object]:
    """Starts the installed app and waits until its process exists."""
    if dry_run:
        # The command that WOULD run, even when the exe is not there yet:
        # a dry run on a machine that has never been built is still worth
        # reading all the way through.
        log(f"  would start {' '.join(launch_command(exe))}")
        return None

    if not exe.is_file():
        raise DeployError(f"there is no app to start at {exe}")
    if launcher is not None:
        return launcher(exe)
    cmd = launch_command(exe)
    log(f"  starting {' '.join(cmd)}")
    try:
        _popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise DeployError(f"could not start {exe}: {exc}") from exc
    # explorer.exe returns as soon as it has handed the request to the
    # shell, so the app appearing is what has to be waited for.
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _pid_running(exe):
            log(f"  {exe.name} is running again")
            return None
        time.sleep(0.5)
    log(f"  [!]  {exe.name} did not appear within {wait_s:.0f}s — continuing to verify anyway")
    return None


def _pid_running(exe: Path) -> bool:
    target = os.path.normcase(os.path.abspath(str(exe)))
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            if proc.info["pid"] in _ancestor_pids():
                continue
            if os.path.normcase(os.path.abspath(proc.info.get("exe") or "")).rstrip(os.sep) == target:
                return True
        except psutil.Error:
            continue
    return False


# --------------------------------------------------------------------------- #
# Verify: prove the app that came back is the app that was just built
# --------------------------------------------------------------------------- #
def _get(url: str, token: Optional[str] = None, timeout: float = 5.0) -> tuple[int, object]:
    request = urllib.request.Request(url, headers={"X-Dashboard-Token": token} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except Exception as exc:  # noqa: BLE001 - not up yet, or not ABP
        return 0, str(exc)
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def spec_paths(root: Path = ROOT) -> set[str]:
    """The API paths the committed spec declares. docs/api/openapi.json is
    generated by scripts/export_openapi.py and kept honest by
    tests/test_sdk.py, so it is the definition of what this version of ABP
    should be serving."""
    try:
        spec = json.loads((root / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeployError(f"could not read docs/api/openapi.json: {exc}") from exc
    return set(spec.get("paths", {}))


@dataclass
class VerifyResult:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.failures.append(message)


def dashboard_token(install_dir: Path, root: Path = ROOT) -> Optional[str]:
    """This install's own DASHBOARD_TOKEN, asked of the interpreter that
    will actually run — the bundled venv's, so the .env it resolves is the
    install's own (bot/envfile.py's state-root rule decides which). Never
    printed, only used as a request header, exactly as the desktop shell's
    own token fetch does."""
    python = venv_python(install_dir)
    if not python.is_file():
        python = Path(root / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python"))
        if not python.is_file():
            return None
    code = "import sys; from bot import envfile; sys.stdout.write(envfile.get_var('DASHBOARD_TOKEN') or '')"
    try:
        proc = _popen([str(python), "-c", code], cwd=str(install_dir), stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        token, _ = proc.communicate(timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (token or "").strip() or None


def verify(install_dir: Path, port: int = DEFAULT_PORT, *, root: Path = ROOT, token: Optional[str] = None,
           timeout_s: float = VERIFY_TIMEOUT, require_bots: bool = True,
           log: Callable[[str], None] = print) -> VerifyResult:
    """Health is up, the API is the one this commit documents, and every
    enabled bot instance is running again — all inside one deadline, so a
    deploy can never hang on a half-started app."""
    base = f"http://127.0.0.1:{port}"
    if token is None:
        token = dashboard_token(install_dir, root)
    expected = spec_paths(root)
    result = VerifyResult()
    deadline = time.monotonic() + timeout_s

    health: object = None
    while True:
        status, health = _get(f"{base}/healthz")
        if status == 200 and isinstance(health, dict) and health.get("status") == "ok":
            log(f"  health ok at {base}/healthz")
            break
        if time.monotonic() >= deadline:
            result.fail(f"the app never became healthy at {base}/healthz ({timeout_s:.0f}s): "
                        f"HTTP {status} {health if not isinstance(health, dict) else health.get('status')}")
            return result
        time.sleep(1.0)

    # What is running, and whether it is this commit's code: the bundle's
    # own build stamp against the checkout's HEAD (bot/diagnostics.py's
    # build_status()). Reported, not enforced — rolling back would restore
    # the same stale bundle, and a reused build (--no-build) can legitimately
    # be older than HEAD. But it must never be invisible.
    bundle = health.get("bundle", {}) if isinstance(health, dict) else {}
    if bundle.get("stale"):
        result.notes.append(f"STALE BUNDLE: the running app was built from {bundle.get('commit') or 'an unknown commit'} "
                            f"but the checkout is at {bundle.get('checkout_commit') or 'an unknown commit'} — the code "
                            f"you are looking at is not what is running (re-run without --no-build)")
    elif bundle.get("commit"):
        log(f"  running bundle {bundle['commit']}")

    status, served = _get(f"{base}/openapi.json")
    if status != 200 or not isinstance(served, dict) or "paths" not in served:
        result.fail(f"could not read {base}/openapi.json (HTTP {status})")
        return result
    live = set(served["paths"])
    if live != expected:
        missing = sorted(expected - live)
        extra = sorted(live - expected)
        detail = []
        if missing:
            detail.append(f"{len(missing)} path(s) missing from the running app, e.g. " + ", ".join(missing[:5]))
        if extra:
            detail.append(f"{len(extra)} path(s) the running app serves that this commit does not document, e.g. "
                          + ", ".join(extra[:5]))
        result.fail("the running app is not this commit's API: " + "; ".join(detail))
    else:
        log(f"  /openapi.json matches docs/api/openapi.json ({len(live)} paths)")

    if not require_bots:
        log("  not checking bot instances (--skip-bots)")
        return result

    status, bots = _get(f"{base}/api/bots", token=token)
    if status == 503:
        result.notes.append("the running app has no DASHBOARD_TOKEN, so it has no bot instances to bring back")
        return result
    if status in (401, 403):
        result.fail(f"could not authenticate against {base}/api/bots (HTTP {status}) — the install's DASHBOARD_TOKEN "
                    f"is not the one this deploy can resolve")
        return result
    if status != 200 or not isinstance(bots, list):
        result.fail(f"could not list bot instances (HTTP {status})")
        return result
    enabled = [b for b in bots if isinstance(b, dict) and b.get("enabled")]
    while True:
        running = {b.get("id") for b in bots if b.get("live_running")}
        missing = [b for b in enabled if b.get("id") not in running]
        if not missing:
            log(f"  {len(enabled)} enabled bot instance(s) are live again")
            return result
        if time.monotonic() >= deadline:
            names = ", ".join(str(b.get("name") or b.get("id")) for b in missing[:5])
            result.fail(f"{len(missing)} enabled bot instance(s) did not come back within the deadline: {names}")
            return result
        time.sleep(2.0)
        status, bots = _get(f"{base}/api/bots", token=token)
        if status != 200 or not isinstance(bots, list):
            continue


# --------------------------------------------------------------------------- #
# The deploy itself
# --------------------------------------------------------------------------- #
@dataclass
class DeployResult:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)      # what actually happened, in order
    installed: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.failures.append(message)


def deploy(root: Path = ROOT, *, install_dir: Optional[str] = None, outside_checkout: bool = False,
           dry_run: bool = False, do_build: bool = True, force_build: bool = False,
           skip_verify: bool = False, skip_bots: bool = False, port: int = DEFAULT_PORT,
           timeout_s: float = VERIFY_TIMEOUT, rollback_on_failure: bool = True, start: bool = True,
           restart: Optional[Callable[[], None]] = None,
           launcher: Optional[Callable[[Path], object]] = None,
           log: Callable[[str], None] = print) -> DeployResult:
    """build -> stop -> install -> restart -> verify, rolling back to the
    one previous copy if any of the last three fails.

    `restart` exists for the one legitimate alternative to launching the exe:
    an install registered as an OS service (scripts/install_task.ps1 and
    friends) is restarted through its service manager, not by starting a
    second copy of it. Verification is identical either way.

    `start=False` installs without starting anything, for the case where
    nothing was running from the install folder to begin with: the point is
    that the NEXT launch runs this commit, and starting a GUI app the person
    deliberately closed is not a deploy's decision to make.

    `launcher` is how the app is started, for the same reason build() takes
    nothing: the default is the OS's own way (explorer.exe on Windows). A
    test that cannot launch a GUI binary passes the command it wants here —
    everything else in this function stays the real thing."""

    result = DeployResult()
    target = resolve_install_dir(root, install_dir)
    profile = build_profile_dir(root)

    refusal = check_install_dir(target, outside_checkout)
    if refusal and not dry_run:
        raise DeployError(refusal)

    log(f"checkout:  {root}")
    log(f"build:     {profile}" + ("" if do_build else "  (--no-build)"))
    log(f"install:   {target}")
    log(f"state:     {state_root(target)}")
    if refusal:
        log(f"  [!]  {refusal} (this is a dry run; a real one would stop here)")

    if do_build:
        if build(root, force=force_build, dry_run=dry_run, log=log) and not dry_run:
            result.ran.append("cargo tauri build")

    plan = plan_install(root, profile, target)
    for rel, why in plan.skipped:
        log(f"  skipping {rel}: {why}")
    if plan.missing and dry_run:
        # Nothing is built yet on this machine; a dry run still has to be
        # able to print the whole plan it WOULD carry out.
        log(f"  {len(plan.missing)} of these do not exist yet and the build would produce them: "
            + ", ".join(plan.missing[:5]) + ("..." if len(plan.missing) > 5 else ""))
        plan = plan_install(root, profile, target, allow_missing=True)
    elif not plan.ok:
        result.fail(f"the build in {profile} is missing {len(plan.missing)} resource(s) it should have "
                    f"({', '.join(plan.missing[:5])}) — run without --no-build")
        return result

    log(f"  {len(plan.install)} file(s)/folder(s) to install"
        + (f", {len(plan.skipped)} skipped as the install's own state" if plan.skipped else ""))
    if dry_run:
        stop_app(target, dry_run=True, log=log)
        install_resources(plan, target, dry_run=True, log=log)
        if restart is not None:
            log("  would restart through the registered OS service instead")
        elif start:
            start_app(target / EXE_NAME, dry_run=True, log=log)
        else:
            log("  would not start it: nothing was running from the install folder before this deploy")
        log(f"  would verify: {base_url(port)}/healthz is ok, /openapi.json has the same paths as "
            f"docs/api/openapi.json, and every enabled bot instance is live again")
        log(f"  would roll back to {previous_dir(target)} if any of that fails")
        result.ran.append("dry run: nothing was built, stopped, installed, started or verified")
        return result
    was_installed = False
    try:
        stop_app(target, log=log)
        still_locked = _wait_unlocked(plan, target)
        if still_locked:
            result.fail(f"something still holds {len(still_locked)} of these open: " + ", ".join(still_locked[:3]))
            return result
        install_resources(plan, target, log=log)
        was_installed = True
        result.installed = [r.rel for r in plan.install]
        result.ran.append(f"installed {len(plan.install)} item(s)")

        if restart is not None:
            log("  restarting through the registered OS service")
            restart()
        elif not start:
            # The install folder gets this commit, and nothing starts an app
            # the person had deliberately closed. There is nothing running to
            # verify, which is said out loud rather than glossed over.
            log("  not starting it: nothing was running from the install folder before this deploy, "
                "so the next launch is what picks this up")
            result.notes.append("installed but not started (nothing was running before) — unverified by "
                                "necessity; start the app and check /healthz")
            return result
        else:
            start_app(target / EXE_NAME, launcher=launcher, log=log)
        result.ran.append("restarted")

        if skip_verify:
            log("  not verifying (--skip-verify) — the deploy is unverified, which is the failure mode "
                 "this script exists to remove")
            result.notes.append("deployed without verification (--skip-verify)")
            return result
        outcome = verify(target, port, root=root, timeout_s=timeout_s, require_bots=not skip_bots, log=log)
        result.notes += outcome.notes
        if not outcome.ok:
            for message in outcome.failures:
                result.fail(message)
        else:
            result.ran.append("verified")
    except DeployError as exc:
        result.fail(str(exc))
    except OSError as exc:
        # A held file, a full disk, a path that vanished mid-copy: reported
        # like any other failure, because this is a command a person runs.
        result.fail(f"installing failed: {exc}")

    if not result.ok and was_installed:
        if rollback_on_failure:
            log("  verification failed — rolling back to the previous copy")
            try:
                # The app that came back is the first thing standing in the way
                # of putting its own files back (loaded .pyd/.exe files refuse
                # to be replaced on Windows), so it goes first — which is also
                # the honest order: nothing else is using those files.
                stop_app(target, log=log)
                restored = rollback(target, log=log)
                result.notes.append(f"rolled back {len(restored)} item(s) from {previous_dir(target)}")
                result.ran.append("rolled back")
                _restart_again(target, restart, launcher, log)
            except (DeployError, OSError) as exc:
                result.fail(f"the rollback itself failed: {exc}")
        else:
            result.notes.append("left the failed deploy in place (--no-rollback)")
    for note in result.notes:
        log(f"  [!]  {note}")
    return result


def _restart_again(target: Path, restart: Optional[Callable[[], None]],
                   launcher: Optional[Callable[[Path], object]] = None,
                   log: Callable[[str], None] = print) -> None:
    """Brings the rolled-back app back up: leaving it stopped would be a
    worse outcome than the deploy that just failed."""
    try:
        if restart is not None:
            restart()
        else:
            start_app(target / EXE_NAME, launcher=launcher, log=log)
    except DeployError as exc:
        log(f"  [!]  could not start the rolled-back app: {exc}")



def _wait_unlocked(plan: Plan, install_dir: Path, wait_s: float = 20.0) -> list[str]:
    deadline = time.monotonic() + wait_s
    locked = locked_paths(plan, install_dir)
    while locked and time.monotonic() < deadline:
        time.sleep(1.0)
        locked = locked_paths(plan, install_dir)
    return locked


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print exactly what would happen and touch nothing")
    parser.add_argument("--install-dir", help=f"folder to install into (default: the checkout's own "
                                              f"desktop-app/src-tauri/target/{BUILD_PROFILE}; "
                                              f"env {INSTALL_DIR_ENV})")
    parser.add_argument("--outside-checkout", action="store_true",
                        help="allow a target folder that is inside no checkout (it has its own .env/config/data)")
    parser.add_argument("--no-build", action="store_true", help="install the build that is already there")
    parser.add_argument("--force-build", action="store_true", help="build even if the existing one is up to date")
    parser.add_argument("--skip-verify", action="store_true", help="do not verify the restarted app (not recommended)")
    parser.add_argument("--no-start", action="store_true",
                        help="install without starting the app (nothing is then verified — say so in your own log)")
    parser.add_argument("--skip-bots", action="store_true", help="verify health and the API, but not the bot instances")
    parser.add_argument("--no-rollback", action="store_true", help="leave a failed deploy in place instead of rolling back")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"dashboard port (default: {DEFAULT_PORT})")
    parser.add_argument("--timeout", type=float, default=VERIFY_TIMEOUT,
                        help=f"seconds to wait for the restarted app to come up (default: {VERIFY_TIMEOUT:.0f})")
    args = parser.parse_args(argv)

    try:
        result = deploy(ROOT, install_dir=args.install_dir, outside_checkout=args.outside_checkout,
                        dry_run=args.dry_run, do_build=not args.no_build, force_build=args.force_build,
                        skip_verify=args.skip_verify, start=not args.no_start, skip_bots=args.skip_bots,
                        port=args.port, timeout_s=args.timeout, rollback_on_failure=not args.no_rollback)

    except DeployError as exc:
        print(f"  [ERR]  {exc}", file=sys.stderr)
        return 2
    for message in result.failures:
        print(f"  [ERR]  {message}", file=sys.stderr)
    if result.ok:
        print("deploy ok: " + ", ".join(result.ran) if result.ran else "deploy ok")
        return 0
    print("deploy FAILED - the running app was not replaced by this build", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

