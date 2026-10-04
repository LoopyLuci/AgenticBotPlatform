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


def write(data: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or paths.registry_path()
    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, target)  # atomic: a crash mid-write never truncates the live registry


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


def to_jsonable(instances: list[Instance], active: Optional[str], previous: Optional[str]) -> dict[str, Any]:
    return {
        "active": active,
        "previous": previous,
        "instances": {i.name: i.public() for i in instances},
    }