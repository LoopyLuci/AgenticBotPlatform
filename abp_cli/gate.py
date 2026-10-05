"""`abp gate ...`, `abp instance ...`, `abp dev ...`: the always-on front door and the
instances behind it (abp_gate/).

  abp gate start [--code-root P] [--state-root P] [--instances-dir P] [--no-start]
  abp gate stop                                        stop every instance, then the gate
  abp gate status                                      what is running, and where traffic goes
  abp gate autostart on|off|status                     start the gate at every logon, hidden

  abp instance list                                    every instance: role, port, pid, health
  abp instance swap <code-root> [--name N]             new code in, zero downtime, auto-rollback
  abp instance rollback                                back to the instance the last swap replaced
  abp instance sandbox <code-root> [--name N]          an agent's own ABP: own state, own port
  abp instance stop <name> [--forget-state]            stop one instance (--forget-state deletes its copy)
  abp instance logs <name> [--lines N] [--follow]

  abp dev up [--worktree P] [--branch B] [--name N]     a git worktree + a sandbox on it
  abp dev down [--name N | --all]                      stop that sandbox (its worktree stays)
  abp dev status                                       the sandboxes and how to point a client at them

These talk to the GATE's control API (abp_gate/, a separate localhost port,
authenticated with the same DASHBOARD_TOKEN as the dashboard) rather than to the
dashboard itself - that is the whole point: the command that swaps the running
code must not go through the code being swapped. Nothing here prints the token,
only the name of the variable that holds it.

Every command takes --json like the rest of abp_cli, and all of them honour
--state-root / --instances-dir, which is how you point them at a gate that was
started with a different ABP_HOME than the shell you are standing in.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from bot.dashboard_client import ApiError, DashboardClient

from abp_gate import control, paths, procs
from abp_gate import manager as gate_manager

#: A swap waits for health (90s), a lease handover (45s) and a drain (15s), so the
#: control call that triggers one has to be allowed to sit there. `gate status` and
#: the rest stay snappy.
LONG_TIMEOUT_S = 240.0
SHORT_TIMEOUT_S = 20.0
#: How long `gate start` waits for the daemon's control API to answer.
GATE_START_TIMEOUT_S = 60.0

WORKTREES_ENV = "ABP_DEV_WORKTREES_DIR"


# ----------------------------------------------------------------- plumbing


def _print(args, data: Any, *, table: Optional[list[str]] = None) -> None:
    if args.json:
        print(json.dumps(data, indent=1, default=str))
        return
    if table and isinstance(data, list):
        rows = [[str(row.get(col, "")) for col in table] for row in data]
        widths = [max(len(col), *(len(r[i]) for r in rows)) if rows else len(col) for i, col in enumerate(table)]
        print("  ".join(col.ljust(w) for col, w in zip(table, widths)))
        for r in rows:
            print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
        return
    print(json.dumps(data, indent=1, default=str) if isinstance(data, (dict, list)) else data)


def _roots(args) -> None:
    """Honour --state-root/--code-root/--instances-dir BEFORE anything resolves a
    path, because abp_gate.paths reads them from the environment on every call.
    Doing it here rather than per command is what makes `--state-root` work
    identically for a read (`gate status`) and for a write (`instance swap`)."""
    if getattr(args, "state_root", None):
        os.environ["ABP_HOME"] = args.state_root
    if getattr(args, "code_root", None):
        os.environ["ABP_GATE_CODE_ROOT"] = args.code_root
    if getattr(args, "instances_dir", None):
        os.environ[paths.INSTANCES_ENV] = args.instances_dir


def _token(args) -> str:
    if getattr(args, "token", None):
        return args.token
    return gate_manager.dashboard_token()


def _control_url(args) -> str:
    """Where the gate's control API is. The running gate's own gate.json is
    authoritative (it knows the port it actually bound); --host overrides it, and
    the default port is only a guess used when no gate has ever been started."""
    if getattr(args, "host", None):
        host = args.host
        if not str(host).startswith("http"):
            host = f"http://{host}"
        return str(host).rstrip("/")
    meta = control.read_gate_meta()
    if meta.get("control_url"):
        return str(meta["control_url"]).rstrip("/")
    return f"http://127.0.0.1:{paths.control_port()}"


async def _call(args, method: str, path: str, *, long: bool = False, **kwargs) -> Any:
    client = DashboardClient(_control_url(args), _token(args),
                             timeout=LONG_TIMEOUT_S if long else SHORT_TIMEOUT_S)
    try:
        return await client._request(method, path, **kwargs)
    finally:
        await client.aclose()


def _require_gate(args) -> str:
    if not control.gate_running():
        raise ApiError(0, f"no gate is running for {paths.instances_dir()} "
                          f"(start one with: abp_cli gate start)")
    return _control_url(args)


def _not_running(exc: ApiError) -> bool:
    return exc.status_code == 0 or "connect" in str(exc).lower() or "refused" in str(exc).lower()


def _drop_none(params: dict) -> dict:
    """httpx encodes an absent optional argument as an empty query value, which
    the gate would then read as "set it to the empty string" - so the unset ones
    are removed rather than sent."""
    return {k: v for k, v in params.items() if v is not None}


# --------------------------------------------------------------------- gate


def add_parser(sub) -> None:
    g = sub.add_parser("gate", help="ABP's always-on front door: one process owning the public port, "
                                   "instances behind it")
    gsub = g.add_subparsers(dest="gate_cmd", required=True)
    p = gsub.add_parser("start", help="start the gate (windowless, detached - it outlives this terminal)")
    _add_roots(p)
    p.add_argument("--no-start", action="store_true", dest="no_start",
                   help="bind the ports but do not start the production instance yet")
    p.add_argument("--foreground", action="store_true",
                   help="run it in this terminal instead of detaching (for watching it)")
    p.add_argument("--instance-lifetime", choices=gate_manager.LIFETIMES, default=gate_manager.LIFETIME_DETACHED,
                   dest="instance_lifetime",
                   help="what happens to the instances when the gate itself dies. 'detached' (default): the "
                        "ACTIVE instance survives and the next gate re-adopts it, which is what keeps ABP up "
                        "across a gate crash. 'gate': every instance dies with the gate - nothing can outlive it, "
                        "and ABP stays down until it is started again")
    gsub.add_parser("stop", help="stop every instance, then the gate itself")
    gsub.add_parser("status", help="is the gate up, what is active, where traffic goes")
    for verb in ("stop", "status"):
        _add_roots(gsub.choices[verb])
    autostart = gsub.add_parser("autostart", help="have the gate come up at every logon, with no console window")
    asub = autostart.add_subparsers(dest="autostart_cmd", required=True)
    for verb, what in (("on", "write the logon entry"),
                       ("off", "delete the logon entry"),
                       ("status", "is there a logon entry, and what does it start")):
        p = asub.add_parser(verb, help=f"{what}")
        p.add_argument("--startup-dir", default=None, dest="startup_dir",
                       help="the Startup folder to use instead of this user's own (Windows only)")
        _add_roots(p)

    inst = sub.add_parser("instance", help="the ABP instances the gate manages: swap, sandbox, stop")
    isub = inst.add_subparsers(dest="instance_cmd", required=True)
    isub.add_parser("list", help="every instance with its role, port, pid and health")
    p = isub.add_parser("swap", help="start a standby instance from new code on the same data and switch to it "
                                     "(rolled back automatically if it is unhealthy)")
    p.add_argument("code_root", help="the ABP checkout to switch to (a worktree, or any clone)")
    p.add_argument("--name", default=None, help="the instance name (default: swap-<timestamp>)")
    p.add_argument("--data-root", default=None, dest="data_root",
                   help="the state to run on (default: the real one - a swap keeps the SAME data)")
    _add_roots(p)
    p = isub.add_parser("rollback", help="switch back to the instance the last swap replaced")
    _add_roots(p)
    p = isub.add_parser("sandbox", help="an agent's own ABP: its own copied state, its own port, "
                                        "no outward connectors")
    p.add_argument("code_root", help="the ABP checkout to run (usually your worktree)")
    p.add_argument("--name", default=None, help="the instance name (default: the folder name)")
    p.add_argument("--keep-state", action="store_true", dest="keep_state",
                   help="reuse this sandbox's existing copied state instead of re-copying it")
    _add_roots(p)
    p = isub.add_parser("stop", help="stop one instance")
    p.add_argument("name")
    p.add_argument("--forget-state", action="store_true", dest="forget_state",
                   help="also delete its copied state (a sandbox only; the default keeps it for next time)")
    _add_roots(p)
    p = isub.add_parser("logs", help="an instance's stdout/stderr")
    p.add_argument("name")
    p.add_argument("--lines", type=int, default=80)
    p.add_argument("--follow", action="store_true", help="stream new lines as they arrive")
    _add_roots(p)
    _add_roots(isub.choices["list"])

    dev = sub.add_parser("dev", help="work on ABP itself: a git worktree with its own sandboxed ABP on it")
    dsub = dev.add_subparsers(dest="dev_cmd", required=True)
    p = dsub.add_parser("up", help="make (or reuse) a worktree and run a sandbox instance on it")
    p.add_argument("--worktree", default=None, help="an existing worktree/clone to use instead of making one")
    p.add_argument("--branch", default=None, help="the branch to create in the new worktree (default: a dev/<host> one)")
    p.add_argument("--name", default=None, help="the sandbox instance name (default: the worktree folder name)")
    p.add_argument("--ref", default=None, help="what to check out in a new worktree (default: HEAD)")
    _add_roots(p)
    p = dsub.add_parser("down", help="stop a sandbox (its worktree is left alone - it is your work)")
    p.add_argument("--name", default=None, help="the sandbox to stop (default: every sandbox)")
    p.add_argument("--all", action="store_true", help="stop every sandbox")
    _add_roots(p)
    dsub.add_parser("status", help="the sandboxes, their ports, and what to export to reach them")
    _add_roots(dsub.choices["status"])


def _add_roots(p) -> None:
    """The three roots every gate command shares. Absent from --help noise on the
    subcommands that only read, because a reader of `instance list --help` does
    not need them - but always accepted, since a shell with the wrong ABP_HOME is
    the single most common way to get confusing answers here."""
    p.add_argument("--state-root", default=None, dest="state_root", help=argparse.SUPPRESS)
    p.add_argument("--instances-dir", default=None, dest="instances_dir", help=argparse.SUPPRESS)
    p.add_argument("--code-root", default=None, dest="code_root", help=argparse.SUPPRESS)


# ------------------------------------------------------------------ gate impl


async def _gate(args) -> int:
    if args.gate_cmd == "status":
        return await _gate_status(args)
    if args.gate_cmd == "stop":
        return await _gate_stop(args)
    if args.gate_cmd == "start":
        return await _gate_start(args)
    if args.gate_cmd == "autostart":
        return _gate_autostart(args)
    print(f"unknown gate subcommand {args.gate_cmd!r}", file=sys.stderr)
    return 2


async def _gate_status(args) -> int:
    if not control.gate_running():
        _print(args, {
            "running": False,
            "instances_dir": str(paths.instances_dir()),
            "public_url": f"http://127.0.0.1:{paths.public_ports()[0]}",
            "hint": "start it with: abp_cli gate start",
        })
        return 0
    data = await _call(args, "GET", "/api/gate")
    _print(args, data)
    if not args.json:
        _print_watch(data)
    return 0


def _print_watch(data: dict) -> None:
    """The lines that answer "is the gate coping, and what has it given up on?".

    A circuit breaker nobody can see is just an outage that stops being fixed,
    so the budget and any instance the watcher has marked failed are printed in
    words rather than left in a JSON field."""
    gate = data.get("gate") or {}
    limits = gate.get("limits") or {}
    restarts = gate.get("restarts") or {}
    print(f"\ninstances {limits.get('alive')}/{limits.get('max_instances')} alive, "
          f"lifetime {limits.get('instance_lifetime') or gate.get('instance_lifetime')}")
    window_s = float(restarts.get("window_s") or 0)
    for name, info in (restarts.get("instances") or {}).items():
        spent = f"{info.get('restarts_last_window')}/{info.get('budget')} restarts in the last {window_s / 60:.0f}m"
        print(f"  {name}: {spent}" + (" - RESTARTING STOPPED" if info.get("circuit_open") else ""))
    for inst in (data.get("instances") or {}).values():
        if inst.get("error"):
            print(f"  {inst.get('name')} [{inst.get('health')}]: {inst['error']}")


async def _gate_stop(args) -> int:
    if not control.gate_running():
        print(f"no gate is running for {paths.instances_dir()} - nothing to stop")
        return 0
    result = await _call(args, "POST", "/api/gate/stop", long=True)
    _print(args, result)
    # The daemon exits after this response, so give it a moment rather than
    # reporting "stopped" for something still winding down.
    for _ in range(50):
        if not control.gate_running():
            break
        await asyncio.sleep(0.1)
    return 0


async def _gate_start(args) -> int:
    if control.gate_running():
        print(f"the gate is already running (pid {control.read_gate_meta().get('pid')}) on "
              f"{control.read_gate_meta().get('control_url')}")
        return await _gate_status(args)
    if args.foreground:
        # Same process, same behaviour, just attached to this terminal.
        from abp_gate.__main__ import main as gate_main
        argv = ["--instance-lifetime", args.instance_lifetime]
        if args.no_start:
            argv.append("--no-start")
        return gate_main(argv)
    code_root = Path(args.code_root or paths.code_root()).resolve()
    python = procs.python_for(code_root)
    argv = [str(python), "-m", "abp_gate", "--instance-lifetime", args.instance_lifetime]
    if args.no_start:
        argv.append("--no-start")
    for flag, value in (("--code-root", args.code_root), ("--state-root", args.state_root),
                        ("--instances-dir", args.instances_dir)):
        if value:
            argv += [flag, str(value)]
    env = _gate_env(code_root)
    pid = procs.spawn(argv, cwd=code_root, env=env, log_path=paths.gate_log_path(),
                      below_normal=False, wait_s=0.2)
    control_url = _control_url(args)
    try:
        await _wait_for_gate(control_url, GATE_START_TIMEOUT_S)
    except TimeoutError:
        print(f"the gate did not answer on {control_url} within {GATE_START_TIMEOUT_S:.0f}s.\n"
              f"It was started (pid {pid}); its log is {paths.gate_log_path()}:", file=sys.stderr)
        print(procs.tail(paths.gate_log_path(), 40), file=sys.stderr)
        return 1
    print(f"abp_gate is up (pid {pid}) - control {control_url}, public "
          f"http://127.0.0.1:{paths.public_ports()[0]}")
    print(f"instances are {args.instance_lifetime}: "
          + ("the active one outlives this gate and the next gate re-adopts it" if args.instance_lifetime
             == gate_manager.LIFETIME_DETACHED else "everything dies with the gate"))
    return await _gate_status(args)


def _gate_env(code_root: Path) -> dict[str, str]:
    """The environment the gate daemon is started with.

    Its own ports come from --public-port/--control-port or the ABP_GATE_* env
    vars, NOT from DASHBOARD_PORT: the gate is what owns 8787, and an inherited
    DASHBOARD_PORT would only be confusing (procs.instance_env() is for the
    instances, which is the other direction of the same relationship)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(code_root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["ABP_GATE"] = "1"
    return env


async def _wait_for_gate(control_url: str, timeout_s: float) -> None:
    """The gate's own /healthz, which is unauthenticated exactly so this wait
    works without holding a token, and so a supervisor can probe it."""
    import httpx

    deadline = time.monotonic() + timeout_s
    last = "no answer yet"
    async with httpx.AsyncClient(timeout=3.0) as client:
        while time.monotonic() < deadline:
            if not control.gate_running():
                last = "the process exited"
                await asyncio.sleep(0.3)
                if not control.gate_running():
                    raise TimeoutError(last)
                continue
            try:
                resp = await client.get(f"{control_url}/healthz")
            except httpx.HTTPError as exc:
                last = str(exc)
            else:
                if resp.status_code in (200, 503):
                    return   # 503 = up, but no instance yet: exactly `gate start --no-start`
                last = f"/healthz returned {resp.status_code}"
            await asyncio.sleep(0.25)
    raise TimeoutError(last)


# ------------------------------------------------------------------ autostart

#: The logon entry's name inside the Startup folder. `abp_` prefixed so it is
#: obvious in a folder full of other people's shortcuts, and .vbs because that
#: is the one thing in a Startup folder that can start a program with NO console
#: window at all - a shortcut would flash one, and a scheduled task or a service
#: registration is a machine-wide change this does not get to make.
AUTOSTART_NAME = "abp_gate_autostart.vbs"
#: Where a test (or somebody with an unusual profile layout) says the Startup
#: folder is. Never the default, and never consulted silently by the gate itself.
STARTUP_DIR_ENV = "ABP_GATE_STARTUP_DIR"


def startup_dir(chosen: Optional[str] = None) -> Path:
    """This user's Startup folder - the one Windows runs at logon, per user and
    not per machine, so it needs no administrator and no uninstall step.

    `chosen` (or ABP_GATE_STARTUP_DIR) is how a test points this at a throwaway
    folder: the real one belongs to the person, and a test that wrote a logon
    entry into it would start a gate on their machine at their next logon."""
    raw = chosen or (os.environ.get(STARTUP_DIR_ENV) or "").strip()
    if raw:
        return Path(os.path.expandvars(raw)).expanduser()
    appdata = (os.environ.get("APPDATA") or "").strip()
    if not appdata:
        raise ApiError(0, "APPDATA is not set, so this shell cannot find the Startup folder; "
                          "pass --startup-dir with the path to it")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_path(chosen: Optional[str] = None) -> Path:
    return startup_dir(chosen) / AUTOSTART_NAME


def _vbs(value: str) -> str:
    """A VBScript string literal. Doubling the quote is the only escape there
    is; a backslash is an ordinary character, which is why these are Windows
    paths and not anything regex-shaped."""
    return '"' + str(value).replace('"', '""') + '"'


def autostart_command(*, code_root: Path, python: Path,
                      state_root: Optional[Path] = None,
                      instances_dir: Optional[Path] = None) -> str:
    """The exact command the logon entry runs.

    `abp_cli gate start`, and not `python -m abp_gate`: it is the documented way
    in, it detaches the daemon properly and it prints where the log is if the
    gate does not come up. The roots are passed explicitly rather than left to
    the environment, because a Startup entry has no environment to inherit -
    that is the whole reason a logon entry can silently come up against the
    wrong ABP_HOME."""
    argv = [str(python), "-m", "abp_cli", "gate", "start", "--code-root", str(code_root)]
    for flag, value in (("--state-root", state_root), ("--instances-dir", instances_dir)):
        if value:
            argv += [flag, str(value)]
    return " ".join(argv)


def autostart_script(*, command: str, code_root: Path, python: Path) -> str:
    """The .vbs itself, in the shape Hermes's own gateway entry uses: set the
    few variables the process needs on the PROCESS environment WScript.Shell
    hands it, then Run the command with window style 0 and do not wait.

    Window style 0 is the load-bearing part. It is what makes a logon start
    invisible, and it is why this is a .vbs and not a shortcut or a bare
    pythonw guess - a console that appears while somebody is logging in to
    their own machine is the one thing this must never do (see
    docs/sandbox-nervous-system.md for the same rule everywhere else in ABP)."""
    lines = [
        "' ABP gate - start it at every logon, with no console window.",
        "' Written by `abp_cli gate autostart on`; remove it with `abp_cli gate autostart off`.",
        f"' Starts: {command}",
        "Option Explicit",
        "Dim sh, env, pp",
        'Set sh = CreateObject("WScript.Shell")',
        'Set env = sh.Environment("PROCESS")',
        f'env.Item("PYTHONPATH") = {_vbs(code_root)}',
        'env.Item("PYTHONUNBUFFERED") = "1"',
        'env.Item("PYTHONUTF8") = "1"',
        'env.Item("ABP_GATE") = "1"',
    ]
    # VIRTUAL_ENV only when the interpreter really is in a venv: procs.python_for
    # falls back to the base interpreter when the checkout has none, and
    # pointing VIRTUAL_ENV at the base prefix would be a lie a child process
    # could act on.
    if python.parent.name in ("Scripts", "bin"):
        lines.append(f'env.Item("VIRTUAL_ENV") = {_vbs(python.parent.parent)}')
    lines += [
        'pp = env.Item("PYTHONPATH")',
        "If Len(pp) > 0 And Left(pp, Len(" + _vbs(code_root) + ")) <> " + _vbs(code_root) + " Then",
        "  env.Item(\"PYTHONPATH\") = " + _vbs(code_root) + " & \";\" & pp",
        "End If",
        f"sh.CurrentDirectory = {_vbs(code_root)}",
        f"sh.Run {_vbs(command)}, 0, False",
        "",
    ]
    return "\n".join(lines)


def autostart_state(chosen: Optional[str] = None, *, code_root: Optional[Path] = None,
                    python: Optional[Path] = None) -> dict[str, Any]:
    """What the logon entry is, whether it is there, and whether it is still
    pointing at this checkout.

    `matches` is the question that matters after a few months: an entry written
    by a build of ABP in a checkout that has since been moved or deleted starts
    a gate that cannot work, and the only symptom is a log nobody reads."""
    code_root = Path(code_root or paths.code_root()).resolve()
    python = Path(python or procs.python_for(code_root))
    path = autostart_path(chosen)
    want = autostart_command(code_root=code_root, python=python,
                             state_root=Path(os.environ["ABP_HOME"]) if os.environ.get("ABP_HOME") else None,
                             instances_dir=(Path(os.environ[paths.INSTANCES_ENV])
                                            if os.environ.get(paths.INSTANCES_ENV) else None))
    info: dict[str, Any] = {"installed": path.is_file(), "path": str(path), "startup_dir": str(path.parent),
                            "code_root": str(code_root), "python": str(python), "expected_command": want}
    if not info["installed"]:
        info["command"] = ""
        info["matches"] = False
        return info
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        info["command"] = f"(unreadable: {exc})"
        info["matches"] = False
        return info
    started = [line for line in text.splitlines() if line.strip().startswith("sh.Run ")]
    # The command is the quoted argument of sh.Run; a VBScript literal doubles
    # any quote inside it, which a command line never has.
    quoted = started[0].split('"', 2)[1] if started else ""
    info["command"] = quoted.replace('""', '"')
    info["matches"] = want in text
    return info


def _gate_autostart(args) -> int:
    """`gate autostart on|off|status`.

    Deliberately a separate verb rather than a flag on `gate start`: this writes
    something into the person's own Windows profile that runs at every logon,
    and that has to be a decision somebody makes out loud, twice - once to
    write it, once to remove it."""
    action = args.autostart_cmd
    if sys.platform != "win32":
        print("`gate autostart` is the Windows Startup folder. On this platform the gate is a detached "
              "process you start yourself (abp_cli gate start), or a unit your init system owns; the "
              "instance lifetime rules are the same either way.", file=sys.stderr)
        return 1
    if action == "on":
        before = autostart_state(args.startup_dir)
        path = Path(before["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(autostart_script(command=before["expected_command"],
                                          code_root=Path(before["code_root"]),
                                          python=Path(before["python"])), encoding="utf-8")
        if before["installed"] and not before["matches"]:
            print(f"replaced the logon entry that was pointing at {before['code_root']}")
        print(f"ABP will be up at every logon: {path}")
        print(f"  it runs, hidden: {before['expected_command']}")
        print("  instances default to `detached`, so the active ABP survives the gate being restarted")
        print("remove it again with: abp_cli gate autostart off")
        return 0
    if action == "off":
        path = autostart_path(args.startup_dir)
        if not path.is_file():
            print(f"there is no autostart entry at {path} - nothing to remove")
            return 0
        path.unlink()
        print(f"removed {path}: ABP will no longer start at logon "
              f"(a gate that is already running keeps running until it is stopped)")
        return 0
    info = autostart_state(args.startup_dir)
    if args.json:
        _print(args, info)
        return 0
    if not info["installed"]:
        print(f"not installed: there is no {AUTOSTART_NAME} in {info['startup_dir']}")
        print("install it with: abp_cli gate autostart on")
        return 0
    print(f"installed: {info['path']}")
    print(f"  starts: {info['command'] or '(nothing this script recognises)'}")
    if info["matches"]:
        print("  points at this checkout")
    else:
        print(f"  [!]  does NOT point at this checkout ({info['code_root']}) - rewrite it with "
              f"`abp_cli gate autostart on`, or ABP may come up from somewhere that no longer exists")
    return 0


# -------------------------------------------------------------- instance impl


async def _instance(args) -> int:
    if args.instance_cmd == "list":
        return await _instance_list(args)
    if args.instance_cmd == "swap":
        return await _instance_swap(args)
    if args.instance_cmd == "rollback":
        return await _instance_rollback(args)
    if args.instance_cmd == "sandbox":
        return await _instance_sandbox(args)
    if args.instance_cmd == "stop":
        return await _instance_stop(args)
    if args.instance_cmd == "logs":
        return await _instance_logs(args)
    print(f"unknown instance subcommand {args.instance_cmd!r}", file=sys.stderr)
    return 2


async def _instance_list(args) -> int:
    data = await _call(args, "GET", "/api/instance")
    rows = [
        {**row,
         "url": f"http://127.0.0.1:{row.get('port')}",
         "active": "yes" if row.get("name") == data.get("active") else ""}
        for row in (data.get("instances") or {}).values()
    ]
    if args.json:
        _print(args, data)
        return 0
    if not rows:
        print(f"no instances (registry: {paths.registry_path()})")
        return 0
    _print(args, rows, table=["name", "role", "health", "port", "pid", "sandbox", "code_root"])
    print(f"\nactive: {data.get('active')}   public: http://127.0.0.1:{paths.public_ports()[0]}   "
          f"rollback target: {data.get('previous') or '(none)'}")
    return 0


async def _instance_swap(args) -> int:
    result = await _call(args, "POST", "/api/instance/swap", long=True,
                         params=_drop_none({"code_root": str(Path(args.code_root).resolve()),
                                            "name": args.name, "data_root": args.data_root}))
    if args.json:
        _print(args, result)
        return 0
    for step in result.get("steps") or []:
        print(f"  - {step}")
    print(f"\n{result.get('instance')} is serving on {result.get('url')}")
    return 0


async def _instance_rollback(args) -> int:
    result = await _call(args, "POST", "/api/instance/rollback", long=True)
    if args.json:
        _print(args, result)
        return 0
    for step in result.get("steps") or []:
        print(f"  - {step}")
    print(f"\n{result.get('instance')} is serving again")
    return 0


async def _instance_sandbox(args) -> int:
    result = await _call(args, "POST", "/api/instance/sandbox", long=True,
                         params={"code_root": str(Path(args.code_root).resolve()),
                                 "keep_state": bool(args.keep_state),
                                 **_drop_none({"name": args.name})})
    _print_sandbox(args, result)
    return 0


def _print_sandbox(args, result: dict) -> None:
    if args.json:
        _print(args, result)
        return
    print(f"sandbox {result.get('instance')} is up on {result.get('url')}")
    for key in ("code_root", "data_root"):
        if result.get(key):
            print(f"  {key.replace('_', ' '):9} {result[key]}")
    print(f"  {'note':9} {result.get('note', '')}")
    print("\npoint a client or a test at it:")
    _print_env(args, result)


def _print_env(args, result: dict) -> None:
    """The two things needed to talk to a sandbox - its URL and the NAME of the
    variable holding the token. The token itself is a secret: a sandbox's .env is
    a copy of the real one, so printing it into a terminal, a log or an agent's
    transcript would spread the real install's credentials for no reason."""
    url = result.get("url") or ""
    host = url.replace("http://", "") if url else ""
    data_root = result.get("data_root") or ""
    print(f"export ABP_URL={url}")
    print(f"export ABP_HOST={host}")
    print(f"export {result.get('token_env_var', 'DASHBOARD_TOKEN')}=$(read from {Path(data_root) / '.env'})")
    print(f"\nabp_cli --host {host} bots list")


async def _instance_stop(args) -> int:
    result = await _call(args, "POST", "/api/instance/stop",
                         params={"name": args.name, "keep_state": not args.forget_state})
    _print(args, result)
    return 0


async def _instance_logs(args) -> int:
    data = await _call(args, "GET", f"/api/instance/{args.name}/logs", params={"lines": args.lines})
    if args.json:
        _print(args, data)
        return 0
    shown = data.get("log") or ""
    print(shown)
    if not args.follow:
        return 0
    # The log is an append-only file and the endpoint is a tail, so following it
    # means remembering how much of it has already been printed - re-printing the
    # window each second would be a stream of duplicates.
    seen = len(shown.splitlines())
    while True:
        await asyncio.sleep(1.0)
        try:
            again = await _call(args, "GET", f"/api/instance/{args.name}/logs", params={"lines": 400})
        except ApiError as exc:
            print(f"\nstopped following: {exc}", file=sys.stderr)
            return 0
        lines = (again.get("log") or "").splitlines()
        for line in lines[seen:]:
            print(line)
        seen = len(lines)


# -------------------------------------------------------------------- dev impl


async def _dev(args) -> int:
    # Every dev command ends up talking to the gate's control API - including
    # `dev up`, which otherwise made a worktree first and only then failed with
    # "all connection attempts failed". The gate is the thing that runs the
    # sandbox, so say which command to run before doing any of the work.
    _require_gate(args)
    if args.dev_cmd == "up":
        return await _dev_up(args)
    if args.dev_cmd == "down":
        return await _dev_down(args)
    if args.dev_cmd == "status":
        return await _dev_status(args)
    print(f"unknown dev subcommand {args.dev_cmd!r}", file=sys.stderr)
    return 2


def worktrees_dir() -> Path:
    """Where `dev up` makes a worktree.

    Next to the checkout rather than inside it: a worktree of the repo, created
    inside the repo's own working tree, is legal git but a nuisance (every
    `git status` in the checkout then lists it as untracked, and half the tools
    that walk the tree walk into it). ABP_DEV_WORKTREES_DIR moves it."""
    raw = os.environ.get(WORKTREES_ENV, "").strip()
    if raw:
        return Path(os.path.expandvars(raw)).expanduser().resolve()
    code_root = paths.code_root()
    return code_root.parent / f"{code_root.name}-worktrees"


def _git(code_root: Path, *argv: str) -> subprocess.CompletedProcess:
    """git with no window and no pager. A pager here would hang the CLI waiting
    for a keypress nobody is there to press."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(code_root), *argv], capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace", creationflags=procs.NO_WINDOW if sys.platform == "win32" else 0,
    )


def _worktree_branch(host: Optional[str] = None) -> str:
    import socket

    name = (host or socket.gethostname()).split(".")[0]
    return f"dev/{name}"


def ensure_worktree(code_root: Path, dest: Path, *, ref: str, branch: str) -> dict[str, Any]:
    """A git worktree at `dest`, made if it is not there and reused if it is.

    A worktree (not a clone) on purpose: it shares this checkout's .git, so
    `git commit` from the agent's editor lands on a real branch of the same
    repository, and `abp_cli instance swap` can later point the real ABP at it -
    which is the whole "work on ABP with ABP always up" loop."""
    dest = dest.resolve()
    if (dest / "bot" / "main.py").is_file():
        return {"path": str(dest), "created": False, "branch": _git(dest, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()}
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", "-b", branch, str(dest), ref]
    result = _git(code_root, *args)
    if result.returncode != 0:
        # The branch usually already exists from a previous `dev up`: reuse it.
        result = _git(code_root, "worktree", "add", str(dest), branch)
    if result.returncode != 0:
        raise ApiError(0, f"could not create a worktree at {dest}: {result.stderr.strip() or result.stdout.strip()}")
    return {"path": str(dest), "created": True, "branch": branch}


async def _dev_up(args) -> int:
    from abp_gate import registry

    code_root = Path(args.code_root or paths.code_root()).resolve()
    if args.worktree:
        worktree = Path(args.worktree).expanduser().resolve()
        if not (worktree / "bot" / "main.py").is_file():
            raise ApiError(0, f"{worktree} is not an ABP checkout (no bot/main.py)")
        info = {"path": str(worktree), "created": False, "branch": ""}
    else:
        branch = args.branch or _worktree_branch()
        info = ensure_worktree(code_root, worktrees_dir() / code_root.name,
                               ref=args.ref or "HEAD", branch=branch)
        worktree = Path(info["path"])
    name = args.name or worktree.name
    existing = registry.get(name)
    if existing is not None and procs.alive(existing.pid):
        # Already up: reuse it rather than starting a second sandbox on the same
        # copied state, which would be two writers of one database.
        result = {"instance": existing.name, "url": f"http://127.0.0.1:{existing.port}",
                  "code_root": existing.code_root, "data_root": existing.data_root,
                  "token_env_var": "DASHBOARD_TOKEN", "note": "already running", "reused": True}
    else:
        result = await _call(args, "POST", "/api/instance/sandbox", long=True,
                             params={"code_root": str(worktree), "name": name, "keep_state": False})
    if args.json:
        _print(args, {"worktree": info, **result})
        return 0
    print(f"{'made' if info.get('created') else 'using'} worktree {worktree}"
          + (f" (branch {info.get('branch')})" if info.get("branch") else ""))
    _print_sandbox(args, result)
    return 0


async def _dev_down(args) -> int:
    from abp_gate import registry

    if args.all or not args.name:
        names = [i.name for i in registry.all_instances() if i.sandbox]
    else:
        names = [args.name]
    if not names:
        print("no sandbox instances to stop")
        return 0
    stopped = []
    for name in names:
        stopped.append(await _call(args, "POST", "/api/instance/stop",
                                   params={"name": name, "keep_state": True}))
    if args.json:
        _print(args, stopped)
        return 0
    for name in names:
        print(f"stopped {name} (its copied state is kept; --forget-state deletes it)")
    return 0


async def _dev_status(args) -> int:
    data = await _call(args, "GET", "/api/instance")
    rows = []
    for row in (data.get("instances") or {}).values():
        if not row.get("sandbox"):
            continue
        rows.append({**row, "url": f"http://127.0.0.1:{row.get('port')}",
                     "env_file": str(Path(row.get("data_root") or "") / ".env")})
    if args.json:
        _print(args, {"instances_dir": str(paths.instances_dir()), "worktrees_dir": str(worktrees_dir()),
                      "sandboxes": rows})
        return 0
    if not rows:
        print(f"no sandboxes (instances dir: {paths.instances_dir()}, worktrees: {worktrees_dir()})")
        return 0
    _print(args, rows, table=["name", "health", "port", "pid", "code_root", "env_file"])
    print("\nstop one with: abp_cli dev down --name <name>")
    return 0


# ----------------------------------------------------------------------- run


async def run(args) -> int:
    _roots(args)
    try:
        if args.cmd == "gate":
            return await _gate(args)
        if args.cmd == "instance":
            _require_gate(args)
            return await _instance(args)
        return await _dev(args)
    except ApiError as exc:
        print(f"error: {exc.detail}" if exc.status_code == 0 else f"error: {exc}", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(f"error: timed out: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a network/connection failure, not an API error
        print(f"error: {exc}", file=sys.stderr)
        return 1