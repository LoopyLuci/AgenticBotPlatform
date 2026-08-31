#!/usr/bin/env python
"""BotServer's local CI/CD pipeline — 100% on this machine, no cloud runner.

Replaces the GitHub Actions workflow this project used to push every
commit's checks to GitHub's own servers for. This runs the same checks
(Python tests/audit, Rust fmt/clippy/build, Android unit tests, Docker
image build) directly on your machine, then — only if every check that
actually ran passed —
rebuilds the production app and restarts whichever local OS service is
registered to run it, so a green pipeline means the change is both
verified and already live on this install.

Invoked automatically by the pre-push git hook (scripts/git-hooks/pre-push,
installed via scripts/install_git_hooks.sh / .ps1) so `git push` itself is
the same trigger point GitHub Actions used to fire on. Can also be run
directly any time:

    python scripts/local_pipeline.py [--no-deploy]

Each check is best-effort about tools that aren't installed: Rust/Docker
checks are skipped (not failed) if cargo/docker aren't on PATH or the
Docker daemon isn't running — this mirrors the project's own "Docker is
optional, never required" stance (see the README's "Headless server
deployment" section) rather than forcing every contributor to have every
toolchain installed just to push a Python-only change.

It's also change-aware (see changed_files()): a push is diffed against
origin/main, and the Rust check, Docker build, and the whole stop/rebuild/
restart cycle are each skipped when the push doesn't touch anything that
step cares about — a docs-only or tests-only push shouldn't pay a multi-
minute Rust+Docker rebuild, or briefly kill the locally running instance,
for a change that instance has nothing new to pick up anyway. When the
changed scope can't be determined, everything runs — unknown always means
"assume the worst," never "assume nothing changed."

There's deliberately no attempt here to replicate the old "bare metal"
GitHub Actions job (spin up a second bot process and hit /healthz) —
doing that against this actual repo would either fight over the real
Telegram bot token with whatever instance is already running, or (worse)
touch the real data/bot.db, since bot/envfile.py's canonical-root pinning
means every local invocation resolves to the one real install on this
machine, not an isolated copy the way an ephemeral CI runner or a Docker
container is. The pytest suite's own FastAPI TestClient coverage (temp_db-
backed, see tests/conftest.py) is what stands in for that check locally.
"""

from __future__ import annotations

import argparse
import datetime
import io
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


class Step:
    @staticmethod
    def head(text: str) -> None:
        print(f"\n=== {text} ===")

    @staticmethod
    def ok(text: str) -> None:
        print(f"  [ok]   {text}")

    @staticmethod
    def skip(text: str) -> None:
        print(f"  [--]   {text}")

    @staticmethod
    def doing(text: str) -> None:
        print(f"  ->     {text}")

    @staticmethod
    def warn(text: str) -> None:
        print(f"  [!]    {text}")

    @staticmethod
    def err(text: str) -> None:
        print(f"  [ERR]  {text}", file=sys.stderr)


def _venv_python() -> str:
    py = ROOT / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    return str(py) if py.exists() else sys.executable


def _run(cmd: list[str], cwd: Optional[Path] = None, retries: int = 0) -> tuple[bool, str]:
    """Runs `cmd`, returning (ok, combined output). Retries on failure — a
    transient file lock (e.g. an antivirus scan mid-build) shouldn't fail
    the whole pipeline the way a real compile error should."""
    attempt = 0
    result = None
    while True:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if result.returncode == 0 or attempt >= retries:
            break
        attempt += 1
        time.sleep(3)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output


# Paths whose change actually requires each expensive step. Anything not
# under one of these (README/CHANGELOG/docs/tests-only edits, for example)
# doesn't need that step re-run — see changed_files() below.
RUST_PREFIXES = ("desktop-app/src-tauri/",)
ANDROID_PREFIXES = ("android-app/",)
DOCKER_PREFIXES = ("bot/", "requirements.txt", "Dockerfile", "docker-compose.yml",
                   ".dockerignore", "scripts/docker-entrypoint.sh")
# The running instance's bot code, config, and app binary all come from a
# copy baked into desktop-app/src-tauri/target/release/ at build time (see
# find_running_instance()'s docstring) — a push that doesn't touch any of
# these has nothing new for a stop/rebuild/restart cycle to actually pick
# up, so it's pure overhead to run one.
DEPLOY_PREFIXES = ("bot/", "config/", "desktop-app/", "requirements.txt")


def _matches(changed: set[str], prefixes: tuple[str, ...]) -> bool:
    return any(f.startswith(prefixes) for f in changed)


def changed_files() -> Optional[set[str]]:
    """Files touched by this push, relative to ROOT with forward slashes,
    or None if that can't be determined — an unknown scope always means
    "run everything", never "assume nothing changed"."""
    range_spec = None
    if os.environ.get("BOTSERVER_LOCAL_PIPELINE_HOOK") == "1":
        # git feeds a pre-push hook "<local ref> <local sha> <remote ref>
        # <remote sha>" lines on stdin — only read it under this env var
        # (set solely by scripts/git-hooks/pre-push) so a manual run of
        # this script never risks blocking on an unrelated inherited stdin.
        try:
            stdin_data = sys.stdin.read()
        except Exception:
            stdin_data = ""
        for line in stdin_data.splitlines():
            parts = line.split()
            if len(parts) != 4:
                continue
            _local_ref, local_sha, _remote_ref, remote_sha = parts
            if set(remote_sha) == {"0"}:
                continue  # new remote ref -- nothing to diff against, fall through
            range_spec = f"{remote_sha}..{local_sha}"
    if range_spec is None:
        ok, base = _run(["git", "merge-base", "HEAD", "origin/main"], cwd=ROOT)
        if not ok:
            return None
        range_spec = f"{base.strip()}..HEAD"
    ok, out = _run(["git", "diff", "--name-only", range_spec], cwd=ROOT)
    if not ok:
        return None
    return {line.strip().replace(os.sep, "/") for line in out.splitlines() if line.strip()}


_EXE_NAME = "bot-server.exe" if IS_WINDOWS else "bot-server"
_EXE_PATH = ROOT / "desktop-app" / "src-tauri" / "target" / "release" / _EXE_NAME


def find_running_instance() -> Optional[int]:
    """PID of a currently-running bot-server(.exe), or None. Real, not
    theoretical: on Windows, Tauri's own build script re-copies
    tauri.conf.json's bundle.resources (the whole .venv) into
    target/release/ on every cargo check/clippy/build — see the existing
    comment on `terminate_child` above about the venv's python.exe
    spawning a *second* process that holds .venv files open — so as long
    as a previously-built instance is running, the copy step can never
    overwrite its own bundled venv's loaded extension modules and every
    Rust check fails identically and deterministically, not flakily."""
    if IS_WINDOWS:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"Name='{_EXE_NAME}'\").ProcessId"],
            capture_output=True, text=True,
        )
    else:
        out = subprocess.run(["pgrep", "-x", _EXE_NAME], capture_output=True, text=True)
    first_line = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    return int(first_line) if first_line.isdigit() else None


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in out.stdout
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


def stop_instance(pid: int) -> None:
    """Graceful close first — on Windows this sends a WM_CLOSE the app's
    own on_window_event(CloseRequested) handler catches, running its real
    shutdown path (bot/main.py's stop_event, task cancellation, backend
    shutdown, an audit log entry) rather than skipping straight to a hard
    kill. Falls back to the same tree-kill (`/T /F`) lib.rs's own
    terminate_child() uses for its spawned Python child, applied here to
    the whole app, only if it's still alive after a few seconds."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
    else:
        subprocess.run(["kill", str(pid)], capture_output=True)
    for _ in range(10):
        if not _pid_alive(pid):
            return
        time.sleep(1)
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True)
    time.sleep(1)


def find_mcp_server_pids() -> list[int]:
    """PIDs of any `python -m bot.mcp_server` process — a real,
    previously-undiagnosed second cause of the exact same "can't
    overwrite the bundled venv" build failure find_running_instance()
    already documents above. An MCP client (e.g. an editor/agent
    integration) can keep this running from the same
    target/release/.venv a cargo build needs to overwrite, holding its
    compiled extension modules (cryptography's _rust.pyd, etc.) mapped
    into memory — Tauri's build script then fails with a locked-file
    error that looks identical to, but has a different root cause than,
    bot-server.exe still running."""
    if IS_WINDOWS:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*bot.mcp_server*' }).ProcessId"],
            capture_output=True, text=True,
        )
    else:
        out = subprocess.run(["pgrep", "-f", "bot.mcp_server"], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split() if p.strip().isdigit()]


def stop_mcp_servers(pids: list[int]) -> None:
    """Tree-kills each one found by find_mcp_server_pids(). No graceful
    shutdown attempt and no restore-on-failure the way stop_instance()
    has for the user-facing app: an MCP client reconnects and respawns
    this on its own next tool call, so there's no real downtime to
    protect here."""
    for pid in pids:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)


def relaunch_bare() -> None:
    if not _EXE_PATH.exists():
        return
    if IS_WINDOWS:
        subprocess.Popen(
            [str(_EXE_PATH)],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        subprocess.Popen([str(_EXE_PATH)], start_new_session=True)


def restore_prior_state(was_running: bool) -> None:
    """Called whenever the pipeline stops short of deploying a rebuilt
    instance (checks failed, or --no-deploy) — leaves the machine exactly
    as it found it rather than as a side effect of just having run
    checks."""
    if not was_running:
        return
    cmd = _service_restart_command()
    if cmd is not None:
        _run(cmd)
    else:
        relaunch_bare()


def check_python() -> bool:
    Step.head("Python — compile, tests, audit")
    py = _venv_python()

    ok, out = _run([py, "-m", "compileall", "-q", "bot", "scripts"])
    if not ok:
        Step.err("byte-compile failed:\n" + out)
        return False
    Step.ok("every .py file compiles")

    ok, out = _run([py, "-m", "pytest", "-q"], cwd=ROOT)
    if not ok:
        Step.err("pytest failed:\n" + out[-4000:])
        return False
    Step.ok(out.strip().splitlines()[-1] if out.strip() else "tests passed")

    ok, out = _run([py, "-m", "pip_audit", "-r", str(ROOT / "requirements.txt"), "--strict"])
    if not ok:
        Step.err("pip-audit found a vulnerability:\n" + out)
        return False
    Step.ok("no known vulnerabilities")
    return True


def check_rust() -> Optional[bool]:
    Step.head("Rust — format, clippy, build check")
    if not shutil.which("cargo"):
        Step.skip("cargo not on PATH — skipping (install Rust to enable this check)")
        return None
    src_tauri = ROOT / "desktop-app" / "src-tauri"

    ok, out = _run(["cargo", "fmt", "--check"], cwd=src_tauri)
    if not ok:
        Step.err("cargo fmt --check failed — run `cargo fmt` in desktop-app/src-tauri:\n" + out)
        return False
    Step.ok("formatting clean")

    ok, out = _run(["cargo", "clippy", "--all-targets", "--", "-D", "warnings"], cwd=src_tauri)
    if not ok:
        Step.err("clippy found warnings:\n" + out[-4000:])
        return False
    Step.ok("clippy clean")

    # tauri_build validates every tauri.conf.json bundle.resources path
    # exists even for a plain check — this repo's own .venv already
    # satisfies that here (unlike a fresh CI checkout), so no placeholder
    # directory is needed the way the old GitHub Actions job needed one.
    ok, out = _run(["cargo", "check", "--release"], cwd=src_tauri, retries=1)
    if not ok:
        Step.err("cargo check --release failed:\n" + out[-4000:])
        return False
    Step.ok("release build check passed")
    return True


def check_android() -> Optional[bool]:
    Step.head("Android — unit tests")
    android_dir = ROOT / "android-app"
    gradlew = android_dir / ("gradlew.bat" if IS_WINDOWS else "gradlew")
    if not gradlew.exists():
        Step.skip("android-app/gradlew not found — skipping")
        return None
    if not (shutil.which("java") or os.environ.get("JAVA_HOME")):
        Step.skip("no JDK on PATH/JAVA_HOME — skipping (install a JDK to enable this check)")
        return None
    ok, out = _run([str(gradlew), "testDebugUnitTest", "--console=plain"], cwd=android_dir)
    if not ok:
        Step.err("./gradlew testDebugUnitTest failed:\n" + out[-4000:])
        return False
    Step.ok("unit tests passed")
    return True


def check_docker() -> Optional[bool]:
    Step.head("Docker — image builds")
    if not shutil.which("docker"):
        Step.skip("docker not on PATH — skipping (Docker is optional, see README)")
        return None
    info_ok, _ = _run(["docker", "info"])
    if not info_ok:
        Step.skip("Docker daemon not running — skipping (Docker is optional, see README)")
        return None
    ok, out = _run(["docker", "build", "-t", "botserver:local-ci", str(ROOT)])
    if not ok:
        Step.err("docker build failed:\n" + out[-4000:])
        return False
    Step.ok("image builds")
    return True


def _service_restart_command() -> Optional[list[str]]:
    """The command to restart whichever OS service scripts/install_*
    registered on this machine, or None if none is registered — deploying
    means restarting whatever's actually configured to run this app, not
    assuming one specific mechanism exists."""
    if IS_WINDOWS:
        check = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "if (Get-ScheduledTask -TaskName BotServer -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"],
            capture_output=True,
        )
        if check.returncode != 0:
            return None
        return ["powershell.exe", "-NoProfile", "-Command",
                "Stop-ScheduledTask -TaskName BotServer -ErrorAction SilentlyContinue; "
                "Start-ScheduledTask -TaskName BotServer"]
    if IS_MACOS:
        plist = Path.home() / "Library" / "LaunchAgents" / "com.botserver.app.plist"
        if not plist.exists():
            return None
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.botserver.app"]
    if IS_LINUX:
        unit = Path.home() / ".config" / "systemd" / "user" / "bot-server.service"
        if not unit.exists():
            return None
        return ["systemctl", "--user", "restart", "bot-server.service"]
    return None


def deploy(was_running: bool) -> bool:
    Step.head("Deploy — rebuild and restart the local install")
    if shutil.which("cargo") and (ROOT / "desktop-app" / "src-tauri").exists():
        Step.doing("cargo tauri build")
        ok, out = _run(["cargo", "tauri", "build"], cwd=ROOT / "desktop-app" / "src-tauri", retries=1)
        if not ok:
            Step.err("production build failed:\n" + out[-4000:])
            return False
        Step.ok("production build complete")
    else:
        Step.skip("cargo not available — nothing to rebuild")

    cmd = _service_restart_command()
    if cmd is not None:
        Step.doing("restarting the registered service")
        ok, out = _run(cmd)
        if not ok:
            Step.err("service restart failed:\n" + out)
            return False
        Step.ok("service restarted — this build is now live")
        return True

    if was_running:
        Step.doing("relaunching bot-server (it was running before this pipeline started)")
        relaunch_bare()
        Step.ok("relaunched — this build is now live")
    else:
        Step.skip("no registered OS service, and nothing was running before this "
                  "pipeline started — run scripts/install_service.sh (Linux), "
                  "install_service_macos.sh (macOS), or install_task.ps1 (Windows) "
                  "once to enable automatic restart on future pushes")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-deploy", action="store_true", help="run checks only, skip the rebuild/restart step")
    args = parser.parse_args()

    log_dir = ROOT / "logs" / "local_pipeline"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.log"

    buf = io.StringIO()

    class _Tee:
        def write(self, s):
            sys.__stdout__.write(s)
            buf.write(s)
            return len(s)

        def flush(self):
            sys.__stdout__.flush()

    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = _Tee()
    try:
        print("BotServer local CI/CD pipeline")

        Step.head("Change detection")
        changed = changed_files()
        if changed is None:
            Step.warn("couldn't determine which files changed — running the full pipeline")
            rust_needed = docker_needed = android_needed = deploy_needed = True
        else:
            rust_needed = _matches(changed, RUST_PREFIXES)
            docker_needed = _matches(changed, DOCKER_PREFIXES)
            android_needed = _matches(changed, ANDROID_PREFIXES)
            deploy_needed = _matches(changed, DEPLOY_PREFIXES)
            Step.ok(f"{len(changed)} file(s) changed since the last push")
            if not rust_needed:
                Step.skip("no desktop-app/src-tauri changes — skipping Rust fmt/clippy/check")
            if not docker_needed:
                Step.skip("no Docker-relevant changes — skipping the Docker image build")
            if not android_needed:
                Step.skip("no android-app changes — skipping Android unit tests")
            if not deploy_needed:
                Step.skip("no bot/config/desktop-app changes — nothing new for a "
                          "stop/rebuild/restart cycle to pick up, skipping it entirely")

        running_pid = find_running_instance() if deploy_needed else None
        was_running = running_pid is not None
        if was_running:
            Step.head("Stopping the running instance")
            Step.doing(f"a build check can't succeed while its own bundled venv is in "
                       f"use — stopping bot-server (pid {running_pid})")
            stop_instance(running_pid)
            Step.ok("stopped")

        if rust_needed or deploy_needed:
            mcp_pids = find_mcp_server_pids()
            if mcp_pids:
                if not was_running:
                    Step.head("Stopping processes holding the bundled venv open")
                Step.doing(f"an MCP client is holding the same bundled venv open "
                           f"(bot.mcp_server, pid {', '.join(map(str, mcp_pids))}) — stopping it too; "
                           f"it respawns on its own next tool call")
                stop_mcp_servers(mcp_pids)
                Step.ok("stopped")

        results: dict[str, Optional[bool]] = {"python": check_python()}
        results["rust"] = check_rust() if rust_needed else None
        results["android"] = check_android() if android_needed else None
        results["docker"] = check_docker() if docker_needed else None

        failed = [name for name, ok in results.items() if ok is False]
        skipped = [name for name, ok in results.items() if ok is None]

        Step.head("Summary")
        for name, ok in results.items():
            (Step.ok if ok else Step.skip if ok is None else Step.err)(name)

        if failed:
            Step.err(f"pipeline failed: {', '.join(failed)}")
            restore_prior_state(was_running)
            return 1

        if skipped:
            Step.warn(f"ran with some checks skipped (not applicable to this change, "
                      f"or tooling not installed): {', '.join(skipped)}")

        if args.no_deploy:
            Step.skip("--no-deploy passed — not rebuilding/restarting")
            restore_prior_state(was_running)
            return 0

        if not deploy_needed:
            Step.skip("nothing deploy-relevant changed — leaving the running instance untouched")
            return 0

        if not deploy(was_running):
            restore_prior_state(was_running)
            return 1

        Step.ok("pipeline green, deployed")
        return 0
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        log_path.write_text(buf.getvalue(), encoding="utf-8")
        print(f"\n(full log: {log_path.relative_to(ROOT)})")


if __name__ == "__main__":
    sys.exit(main())
