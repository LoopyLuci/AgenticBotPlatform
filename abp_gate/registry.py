"""What the gate knows about every ABP instance it manages.

One JSON file, read-modify-write under a lock, written atomically. It is the
gate's only durable state and it is deliberately trivial:

    {
      "active": "prod",
      "instances": {
        "prod": {"name": "prod", "code_root": "...", "data_root": "...",
                 "port": 8791, "pid": 1234, "role": "active",
                 "health": "healthy", "started": 1700000000.0,
                 "sandbox": false, "localai_port": 11437, "error": null}
      },
      "previous": "prod-1712345678"
    }

`role` is active | standby | sandbox - the three roles bot/main.py understands.
`previous` is the instance a `rollback` would go back to, which is the whole
reason a rollback can be automatic: the outgoing instance is named in the
registry before it is asked to leave, not remembered in somebody's head.

`restarts` is the watcher's memory: when the watcher last tried to restart each
instance, so a crash loop can be told apart from one unlucky crash even across a
restart of the gate itself.

Role transitions are the manager's business; this module only stores and
sanitises them, and every field is optional on read so a registry written by an
older gate is still usable.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from abp_gate import paths

ROLE_ACTIVE = "active"
ROLE_STANDBY = "standby"
ROLE_SANDBOX = "sandbox"
ROLES = (ROLE_ACTIVE, ROLE_STANDBY, ROLE_SANDBOX)

HEALTH_STARTING = "starting"
HEALTH_HEALTHY = "healthy"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_STOPPED = "stopped"
HEALTH_FAILED = "failed"

#: How far back the restart history is kept. Ten minutes is long enough that a
#: crash loop has to be a crash loop (not a single unlucky restart) before the
#: watcher's budget is spent, and short enough that an instance which stays
#: broken is not left marked failed forever. limits.restart_window_s() is the
#: overridable version of this.
DEFAULT_RESTART_WINDOW_S = 600.0

# One registry writer per process. uvicorn runs the control app in a single
# event loop, so this is belt-and-braces rather than a real requirement - but
# a swap is a multi-step read-modify-write and two of them interleaving would
# be exactly the kind of bug that only shows up during an incident.
_lock = threading.RLock()


@dataclass
class Instance:
    name: str
    code_root: str = ""
    data_root: str = ""
    port: int = 0
    pid: Optional[int] = None
    role: str = ROLE_STANDBY
    health: str = HEALTH_STARTING
    started: float = 0.0
    sandbox: bool = False
    standby: bool = False
    localai_port: int = 0
    error: str = ""
    log: str = ""
    source: str = ""  # the git ref/branch it was started from, when known
    #: Has it ever answered /healthz? The watcher refuses to restart an
    #: instance that never did: a process that cannot start is not a process
    #: that crashed, and retrying it on a timer is how one bad checkout becomes
    #: thousands of processes.
    ever_healthy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Instance":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (raw or {}).items() if k in known}
        clean.setdefault("name", "")
        inst = cls(**clean)
        if inst.role not in ROLES:
            inst.role = ROLE_STANDBY
        return inst

    def public(self) -> dict[str, Any]:
        """What GET /api/instance and `abp_cli instance list` print. No paths
        that are already printed elsewhere, no pid tree, no secrets - the
        control API's output is logged by the CLI in --json mode."""
        return self.to_dict()


def free_port(preferred: int = 0) -> int:
    """A port nothing is listening on right now. `preferred` is tried first so a
    caller that has an opinion (a test, a re-start of the same instance) gets a
    stable one; the OS picks otherwise, which is the only race-free option."""
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", preferred))
            except OSError:
                return _any_free_port()
        return preferred
    return _any_free_port()


def _any_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def read(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or paths.registry_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": None, "previous": None, "instances": {}}
    if not isinstance(raw, dict):
        return {"active": None, "previous": None, "instances": {}}
    raw.setdefault("active", None)
    raw.setdefault("previous", None)
    raw.setdefault("instances", {})
    return raw


#: How long a rename waits for whoever is reading the registry.
#:
#: Windows opens a file for reading WITHOUT FILE_SHARE_DELETE, so any reader that
#: holds `registry.json` open makes a rename onto it fail with ACCESS DENIED -
#: and the gate's own watcher reads the registry on every cycle, so the gate can
#: lose that race against itself. Measured: the rename fails outright whenever a
#: plain reader has the file open, and a read of a file this small already takes
#: milliseconds on a busy disk, so "sometimes" here is "under load", which is
#: exactly when a lost write costs the most.
#:
#: Waiting is not papering over anything: the write itself is atomic and
#: correct, it just lost a race with a reader of the same file, and losing that
#: race must not fail the operation that lost it. A real permissions problem
#: still raises - it just takes a second to say so.
REPLACE_ATTEMPTS = 20
REPLACE_INTERVAL_S = 0.1


def _replace(tmp: Path, target: Path) -> None:
    """`os.replace(tmp, target)`, waiting out a reader holding the target open."""
    deadline = time.monotonic() + REPLACE_ATTEMPTS * REPLACE_INTERVAL_S
    while True:
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(REPLACE_INTERVAL_S)


def write(data: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or paths.registry_path()
    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A temporary name of its own, never a shared one: the lock above is per
        # process, so the gate and anything else writing this registry (an agent's
        # script, a test putting an instance in by hand) can be inside `write` at
        # the same time. With one shared name they wrote each other's file and then
        # renamed each other's file away - which is a FileNotFoundError for one of
        # them, or a rename of the WRONG content into the live registry.
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
            _replace(tmp, target)  # atomic: a crash mid-write never truncates the live registry
        finally:
            tmp.unlink(missing_ok=True)  # a no-op after a successful rename; no litter after a failure


def update(mutate, path: Optional[Path] = None) -> dict[str, Any]:
    """Read, apply `mutate(data)`, write, return. The one place the registry is
    ever changed, so a swap cannot interleave with itself."""
    target = path or paths.registry_path()
    with _lock:
        data = read(target)
        mutate(data)
        write(data, target)
        return data


def put(name: str, instance: Instance, path: Optional[Path] = None) -> dict[str, Any]:
    def _apply(data: dict) -> None:
        data["instances"][name] = instance.to_dict()

    return update(_apply, path)


def get(name: str, path: Optional[Path] = None) -> Optional[Instance]:
    raw = read(path)["instances"].get(name)
    return Instance.from_dict(raw) if raw else None


def all_instances(path: Optional[Path] = None) -> list[Instance]:
    data = read(path)
    return [Instance.from_dict(v) for v in data["instances"].values()]


def active_name(path: Optional[Path] = None) -> Optional[str]:
    return read(path)["active"]


def set_active(name: Optional[str], previous: Optional[str] = None, path: Optional[Path] = None) -> dict[str, Any]:
    """Point routing at `name`, remembering `previous` so rollback is a field
    read rather than a guess about which instance was there before."""

    def _apply(data: dict) -> None:
        if previous is not None:
            data["previous"] = previous
        data["active"] = name
        inst = data["instances"].get(name or "")
        if inst is not None:
            inst["role"] = ROLE_ACTIVE

    return update(_apply, path)


def previous_name(path: Optional[Path] = None) -> Optional[str]:
    return read(path)["previous"]


def drop(name: str, path: Optional[Path] = None) -> dict[str, Any]:
    def _apply(data: dict) -> None:
        data["instances"].pop(name, None)
        if data.get("active") == name:
            data["active"] = None
        if data.get("previous") == name:
            data["previous"] = None

    return update(_apply, path)


def unique_name(base: str, path: Optional[Path] = None) -> str:
    """`prod` for the first one, `prod-1712345678` for the next, so a swap never
    silently overwrites the record of the instance it replaced."""
    taken = read(path)["instances"]
    if base not in taken:
        return base
    return f"{base}-{int(time.time())}"


def stamp() -> float:
    return time.time()


# ------------------------------------------------------------ restart history


def record_restart(name: str, when: Optional[float] = None,
                   window_s: float = DEFAULT_RESTART_WINDOW_S) -> list[float]:
    """Note that the watcher restarted `name`; returns the starts still inside
    the window (oldest first).

    Persisted rather than kept in the gate's memory on purpose: the budget has
    to survive the gate itself being restarted, or "restart the gate" becomes a
    way to get a fresh allowance of runaway restarts."""
    at = time.time() if when is None else when

    def _apply(data: dict) -> None:
        history = [float(t) for t in (data.get("restarts") or {}).get(name, []) if isinstance(t, (int, float))]
        history.append(at)
        data.setdefault("restarts", {})[name] = [t for t in history if t > at - window_s]

    return update(_apply)["restarts"].get(name, [])


def restarts_in_window(name: str, window_s: float = DEFAULT_RESTART_WINDOW_S) -> list[float]:
    now = time.time()
    return [float(t) for t in (read().get("restarts") or {}).get(name, []) if now - float(t) <= window_s]


def clear_restarts(name: str) -> None:
    """The instance came back healthy, so whatever budget it had is spent -
    reset, or an instance that flapped once an hour would eventually be left
    marked failed while it serves perfectly well."""

    def _apply(data: dict) -> None:
        (data.get("restarts") or {}).pop(name, None)

    update(_apply)


def to_jsonable(instances: list[Instance], active: Optional[str], previous: Optional[str]) -> dict[str, Any]:
    return {
        "active": active,
        "previous": previous,
        "instances": {i.name: i.public() for i in instances},
    }