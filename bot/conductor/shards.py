"""Running the suite politely: a machine-wide lock, a worker count sized from what is free, and test files split into shards.

    budget = Budget.measure(max_workers=8)             # cpu / memory free right now -> pytest -n
    with machine_lock(wait_s=1800): ...                 # one heavy run at a time on this machine (integrator or pipeline)
    cmds = shard_commands(suite, files, shards=3, workers=budget.workers)

The lock is `scripts/release_guard.PipelineLock` (a live owner blocks, a dead or recycled one is stale), kept in a folder every
checkout and every user session on the machine shares, so two integrator runs - or an integrator run and another pipeline that
takes the same lock - never run their suites at once.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence

import psutil

GB = 1024 ** 3
PER_WORKER_GB = 1.5          # what one pytest-xdist worker is budgeted in memory


class MachineBusy(RuntimeError):
    pass


@dataclass
class Budget:
    workers: int
    cpus: int
    free_cpus: float
    free_ram_gb: float
    limited_by: str

    def to_dict(self) -> dict:
        return {"workers": self.workers, "cpus": self.cpus, "free_cpus": round(self.free_cpus, 1),
                "free_ram_gb": round(self.free_ram_gb, 1), "limited_by": self.limited_by}

    @classmethod
    def measure(cls, max_workers: int = 8, per_worker_gb: float = PER_WORKER_GB, *, cpus: Optional[int] = None,
                busy_pct: Optional[float] = None, free_ram: Optional[int] = None) -> "Budget":
        """pytest -n for right now: the idle CPUs and the free RAM (per_worker_gb each), at least 1, at most max_workers."""
        cpus = cpus if cpus is not None else (psutil.cpu_count(logical=True) or 1)
        busy = busy_pct if busy_pct is not None else psutil.cpu_percent(interval=0.3)
        free_ram = free_ram if free_ram is not None else psutil.virtual_memory().available
        free_cpus = max(0.0, cpus * (1 - min(max(busy, 0.0), 100.0) / 100.0))
        by_cpu, by_ram = int(free_cpus), int((free_ram / GB) // per_worker_gb)
        n = max(1, min(int(max_workers), by_cpu, by_ram))
        limit = "max_workers" if n == int(max_workers) and n <= min(by_cpu, by_ram) else ("cpu" if by_cpu <= by_ram else "memory")
        return cls(n, cpus, free_cpus, free_ram / GB, limit)


def lock_path() -> Path:
    """One lock file for the whole machine: ABP_SUITE_LOCK, else a shared folder (ProgramData on Windows, /tmp elsewhere)."""
    env = os.environ.get("ABP_SUITE_LOCK")
    if env:
        return Path(env)
    base = os.environ.get("PROGRAMDATA") if os.name == "nt" else "/tmp"
    folder = Path(base or tempfile.gettempdir()) / "abp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path(tempfile.gettempdir())
    return folder / "suite.lock"


def _release_guard():
    path = Path(__file__).resolve().parents[2] / "scripts" / "release_guard.py"
    if not path.is_file():
        raise RuntimeError("scripts/release_guard.py not found (the machine lock lives there)")
    name = "conductor_release_guard"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)          # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                         # type: ignore[union-attr]
    return mod


@contextmanager
def machine_lock(wait_s: float = 1800, path: Optional[Path] = None, label: str = "conductor suite", poll_s: float = 2.0) -> Iterator[None]:
    """Hold the machine-wide suite lock; wait up to `wait_s` for a live holder, then raise MachineBusy."""
    rg = _release_guard()
    deadline = time.monotonic() + max(0.0, wait_s)
    lock = rg.PipelineLock(path or lock_path(), label)
    while True:
        try:
            lock.__enter__()
            break
        except rg.Busy as exc:
            if time.monotonic() >= deadline:
                raise MachineBusy(f"{exc}; waited {wait_s:.0f}s") from exc
            time.sleep(poll_s)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def split_files(files: Sequence[tuple[str, int]], shards: int) -> list[list[str]]:
    """Balance (path, size) pairs over `shards` groups: biggest first onto the lightest group. Empty groups are dropped."""
    shards = max(1, int(shards))
    groups: list[list[str]] = [[] for _ in range(shards)]
    load = [0] * shards
    for path, size in sorted(files, key=lambda f: (-f[1], f[0])):
        i = load.index(min(load))
        groups[i].append(path)
        load[i] += max(size, 1)
    return [sorted(g) for g in groups if g]


def find_test_files(root: Path, folder: str = "tests") -> list[tuple[str, int]]:
    base = Path(root) / folder
    return [(p.relative_to(root).as_posix(), p.stat().st_size) for p in sorted(base.rglob("test_*.py"))] if base.is_dir() else []


def with_workers(cmd: Sequence[str], workers: int) -> list[str]:
    """`cmd` with its pytest `-n N` set to `workers`; a command without -n is left alone."""
    out = list(cmd)
    for i, a in enumerate(out):
        if a == "-n" and i + 1 < len(out):
            out[i + 1] = str(workers)
        elif a.startswith("-n") and a[2:].isdigit():
            out[i] = f"-n{workers}"
    return out


def shard_commands(suite: Sequence[str], groups: list[list[str]], target: str = "tests") -> list[list[str]]:
    """One command per group: the suite with its `tests` argument replaced by the group's files (appended if it has none)."""
    cmds = []
    for g in groups:
        if target in suite:
            i = list(suite).index(target)
            cmds.append([*suite[:i], *g, *suite[i + 1:]])
        else:
            cmds.append([*suite, *g])
    return cmds
