"""Where the gate keeps its own bookkeeping, and which ports it owns.

Deliberately the smallest module in abp_gate: it answers "where do files go"
and nothing else, so a path convention can change without anything else in the
package having to know.

Layout, all under ABP_INSTANCES_DIR (default `<state root>/instances`):

    instances/gate/registry.json       the instance registry (names, roles, ports, pids)
    instances/gate/gate.log            the gate daemon's own log
    instances/gate/gate.json           the gate's own pid + the ports it owns
    instances/<name>/                  a sandbox instance's COPIED state
    instances/<name>.log               that instance's stdout/stderr

The state root is ABP_HOME when set and the code root otherwise - exactly
bot/envfile.py's rule, reimplemented over the environment rather than by
importing bot (so the gate can be reasoned about, and tested, without booting
ABP).
"""

from __future__ import annotations

import os
from pathlib import Path

INSTANCES_ENV = "ABP_INSTANCES_DIR"

#: The gate's own control API port - deliberately NOT 8787. The control API can
#: start and stop instances; the public port is what every other program talks
#: to and must stay up even while the gate is restarting an instance.
CONTROL_PORT = 8788

#: The dashboard/API port ABP has always used, and the one the desktop app,
#: abp_cli and every Android/iOS client already point at. Owning it is the
#: whole point: from the outside nothing changes when the code underneath is
#: swapped.
DASHBOARD_PORT = 8787

#: ABP's local model server (bot/localai). Optional, off by default, because
#: the server binds a port from its own settings and an instance behind the
#: gate has to be told to bind a private one instead - see ABP_LOCALAI_PORT and
#: manager.py's _localai_port(). Listed as a channel constant so adding it is
#: one env var rather than a redesign.
LOCALAI_PORT = 11436

CHANNELS = ("dashboard", "localai")


def code_root() -> Path:
    """The ABP checkout the gate is running out of (abp_gate's own parent)."""
    env = os.environ.get("ABP_GATE_CODE_ROOT", "").strip()
    if env:
        return Path(os.path.expandvars(env)).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def state_root() -> Path:
    """The real ABP state: ABP_HOME if set, else the code root itself."""
    home = os.environ.get("ABP_HOME", "").strip()
    if home:
        return Path(os.path.expandvars(home)).expanduser().resolve()
    return code_root()


def instances_dir() -> Path:
    """Where sandboxed instances and the gate's own state live."""
    raw = os.environ.get(INSTANCES_ENV, "").strip()
    if raw:
        return Path(os.path.expandvars(raw)).expanduser().resolve()
    return state_root() / "instances"


def gate_dir() -> Path:
    path = instances_dir() / "gate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def registry_path() -> Path:
    return gate_dir() / "registry.json"


def gate_log_path() -> Path:
    return gate_dir() / "gate.log"


def gate_meta_path() -> Path:
    return gate_dir() / "gate.json"


def instance_state_dir(name: str) -> Path:
    """A sandbox instance's private copy of ABP's state."""
    return instances_dir() / name


def instance_log_path(name: str) -> Path:
    return instances_dir() / f"{name}.log"


def public_ports() -> list[int]:
    """The loopback ports this gate binds and reverse-proxies, from
    ABP_GATE_PUBLIC_PORTS (comma separated). 8787 alone by default; add 11436
    once ABP_LOCALAI_PORT is honoured by the instance (it is)."""
    raw = os.environ.get("ABP_GATE_PUBLIC_PORTS", "").strip()
    if not raw:
        return [DASHBOARD_PORT]
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            port = int(chunk)
        except ValueError as exc:
            raise ValueError(f"ABP_GATE_PUBLIC_PORTS: {chunk!r} is not a port number") from exc
        if port not in out:
            out.append(port)
    if not out:
        raise ValueError("ABP_GATE_PUBLIC_PORTS is set but names no ports")
    return out


def channel_of_port(port: int) -> str:
    """Which backend a public port fronts. Unknown ports are treated as
    dashboard traffic rather than refused - a new public port should work
    before anybody has taught the gate what it is for."""
    if port == LOCALAI_PORT:
        return "localai"
    return "dashboard"


def control_port() -> int:
    raw = os.environ.get("ABP_GATE_CONTROL_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"ABP_GATE_CONTROL_PORT: {raw!r} is not a port number") from exc
    return CONTROL_PORT


def private_localai_port() -> int:
    """The port a gate-managed instance is told to bind its model server on.
    Only ever used when the gate owns the public 11436."""
    raw = os.environ.get("ABP_GATE_LOCALAI_PORT", "").strip()
    return int(raw) if raw else LOCALAI_PORT + 1