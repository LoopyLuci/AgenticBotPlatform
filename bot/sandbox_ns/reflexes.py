"""Three small rules the sampler runs, so the machine notices before the person has to.

Each rule is a pure decision function over a snapshot (`memory_decision`, `cpu_decision`,
`estop_decision`) plus one `apply()` that carries the decisions out. That split is the point:
what the machine does about a hot cell is worth testing directly, without a sampler thread, a
job object and a running build in the picture.

* **memory** - a cell past its own cap is killed (`limit_hit`). A cap that means nothing would
  be worse than no cap: the process is the problem, and it is stopped.
* **CPU** - when the *machine* stays above the threshold for several samples in a row, builds
  and workers are demoted to idle priority. Not killed: a slow build is still better than no
  build, and the person can raise the cap themselves. Demotion is one-way per cell (the priority
  is only lowered, never raised back automatically) so the machine cannot oscillate.
* **emergency stop** - when ABP's own estop is engaged (`bot/agent_runtime/estop.py`, the same
  sentinel every agent entry point already checks), every non-persistent cell is killed. Daemons
  are left alone: the estop stops *work*, and a daemon that was already running is somebody's
  service, not a turn in progress.

Thresholds are read from config (`sandbox_ns.cpu_percent`, `sandbox_ns.cpu_samples`,
`sandbox_ns.memory_percent` as a margin over the cap) so they can be tuned per machine, with
the code's defaults as the fallback.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import Optional

#: The machine's CPU has to be this busy, in this many consecutive samples, before builds and
#: workers are demoted. Three samples at the sampler's own 3 s is ~9 s of sustained load -
#: long enough to be a real build, short enough that nobody notices the pause.
CPU_PERCENT = 85.0
CPU_SAMPLES = 3
#: A cell is killed once it is this far over its own memory cap, so a process that is merely
#: *near* the line (a page cache filling up) is not killed for it.
MEMORY_MARGIN = 0.15
#: The presets demoted when the machine is hot. Never 'tool' or 'agent': a person's agent is
#: the thing they are waiting for, and 'engine' is already capped.
DEMOTABLE = ("build", "worker")
DEMOTED_PRIORITY = "idle"

_hot: dict = {"since": 0.0, "runs": 0, "last": 0.0}


@dataclass(frozen=True)
class Decision:
    """One rule firing: what to do, to which cell, and why (the `why` goes into the event log)."""
    kind: str                 # memory | cpu | estop
    cell: str = ""
    detail: str = ""

    def summary(self) -> dict:
        return {"kind": self.kind, "cell": self.cell, "detail": self.detail}


def memory_decision(cell, sample: dict) -> Optional[Decision]:
    """Kill a cell that is over its memory cap. `sample` is the registry's per-cell measurement
    (`rss_mb`, `cpu_percent`, `pid`, `at`); a cell with no processes in it never fires."""
    cap = int(getattr(cell.policy, "memory_mb", 0) or 0)
    used = float(sample.get("rss_mb") or 0.0)
    if cap <= 0 or used <= 0 or not sample.get("pid"):
        return None
    limit = cap * (1.0 + _margin())
    if used <= limit:
        return None
    return Decision("memory", cell.id,
                    f"{cell.name} is using {used:.0f} MB against its {cap} MB cap (>{limit:.0f} MB tolerated)")


def cpu_decision(cpu_percent: float, hot_runs: int, cells) -> list[Decision]:
    """Demote builds and workers when the machine itself has been hot for `hot_runs` samples."""
    if cpu_percent < _cpu_threshold() or hot_runs < _cpu_samples():
        return []
    out = []
    for cell in cells:
        if cell.policy.name in DEMOTABLE and cell.policy.priority not in ("idle",):
            out.append(Decision("cpu", cell.id,
                                f"the machine has been {cpu_percent:.0f}% busy for {hot_runs} samples; "
                                f"{cell.name} demoted to {DEMOTED_PRIORITY} priority"))
    return out


def estop_decision(engaged: bool, cells) -> list[Decision]:
    """ABP's emergency stop: stop the work, leave the services."""
    if not engaged:
        return []
    return [Decision("estop", c.id, f"the emergency stop is engaged; {c.name} stopped")
            for c in cells if not c.policy.persistent]


def apply(registry, *, sample: bool = True) -> list[Decision]:
    """Run every rule against one snapshot and carry out what they decide. Called by the
    registry's sampler; `sample=False` runs the rules on the last measurement without taking a
    new one (tests do this)."""
    if sample:
        registry.sample(reflexes=False)
    cells = registry.cells()
    done: list[Decision] = []
    for cell in cells:
        decision = memory_decision(cell, registry.sample_for(cell.id))
        if decision is None:
            continue
        registry.event("limit_hit", cell=decision.cell, detail=decision.detail)
        cell.kill(reason=decision.detail)
        done.append(decision)
    live = [c for c in cells if not c.closed]
    cpu_percent = _system_cpu(registry)
    done += _demote(registry, cpu_decision(cpu_percent, _hot_runs(cpu_percent, registry), live), live)
    done += _estop(registry, estop_decision(_estop_engaged(), live), live)
    return done


# ---- the counters the cpu rule needs -----------------------------------------------------

def _hot_runs(cpu_percent: float, registry) -> int:
    """Consecutive samples the machine has been over the threshold. Reset by any sample below
    it, so a burst of load has to be sustained rather than momentary."""
    now = time.monotonic()
    if cpu_percent >= _cpu_threshold():
        _hot["runs"] = _hot["runs"] + 1 if now - _hot["last"] <= _interval(registry) * 2 else 1
        _hot["since"] = _hot["since"] or now
    else:
        _hot["runs"] = 0
        _hot["since"] = 0.0
    _hot["last"] = now
    return int(_hot["runs"])


def _interval(registry) -> float:
    from bot.sandbox_ns.registry import SAMPLE_INTERVAL_S

    return float(getattr(registry, "sample_interval_s", SAMPLE_INTERVAL_S) or SAMPLE_INTERVAL_S)


def _system_cpu(registry) -> float:
    try:
        return registry.system_cpu_percent()
    except Exception:  # noqa: BLE001 - psutil missing or a locked-down host: no verdict
        return 0.0


def _estop_engaged() -> bool:
    try:
        from bot.agent_runtime import estop

        return bool(estop.is_engaged())
    except Exception:  # noqa: BLE001 - no database yet (a worker, a first import): not engaged
        return False


def _log_limit(registry, decision: Decision) -> None:
    registry.event("limit_hit", cell=decision.cell, detail=decision.detail)


def _demote(registry, decisions: list[Decision], cells) -> list[Decision]:
    done = []
    for decision in decisions:
        cell = next((c for c in cells if c.id == decision.cell), None)
        if cell is None:
            continue
        if _set_idle(cell):
            registry.event("limit_hit", cell=decision.cell, detail=decision.detail)
            done.append(decision)
    return done


def _set_idle(cell) -> bool:
    """Put a live cell's processes at idle priority. The job's own limit is changed on Windows
    (one call covers every process in it); elsewhere each pid is niced. Never raises."""
    try:
        if cell.job_handle():
            from bot.agent_runtime import win_job

            win_job.set_priority(cell.job_handle(), DEMOTED_PRIORITY)
        else:
            import psutil

            for pid in cell.pids():
                psutil.Process(pid).nice(psutil.IDLE_PRIORITY_CLASS)
        cell.policy = dataclasses.replace(cell.policy, priority=DEMOTED_PRIORITY)
        return True
    except Exception:  # noqa: BLE001 - the process is gone, or the OS said no
        return False


def _estop(registry, decisions: list[Decision], cells) -> list[Decision]:
    done = []
    for decision in decisions:
        cell = next((c for c in cells if c.id == decision.cell), None)
        if cell is None:
            continue
        cell.kill(reason=decision.detail)
        done.append(decision)
    return done


# ---- the tunables --------------------------------------------------------------------------

def _setting(name: str, default):
    try:
        from bot.config import config

        raw = ((config.current.get("sandbox_ns") or {}).get(name))
    except Exception:  # noqa: BLE001
        return default
    return default if raw is None or raw == "" else raw


def _cpu_threshold() -> float:
    try:
        return float(_setting("cpu_percent", CPU_PERCENT))
    except (TypeError, ValueError):
        return CPU_PERCENT


def _cpu_samples() -> int:
    try:
        return max(1, int(_setting("cpu_samples", CPU_SAMPLES)))
    except (TypeError, ValueError):
        return CPU_SAMPLES


def _margin() -> float:
    try:
        return max(0.0, float(_setting("memory_margin", MEMORY_MARGIN)))
    except (TypeError, ValueError):
        return MEMORY_MARGIN
