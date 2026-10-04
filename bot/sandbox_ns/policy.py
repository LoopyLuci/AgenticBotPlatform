"""What a sandbox is allowed to do: memory, CPU share, processor set, priority, lifetime.

One `Policy` says it all, so every caller says the same thing the same way and the answer is
inspectable (`policy.describe()`) instead of being scattered across creationflags in twenty
files. Named presets cover the kinds of process ABP actually starts; a caller that needs
something else does `dataclasses.replace(PRESETS["tool"], memory_mb=512)`.

**Defaults are ABP's own hardware rules, not arbitrary ones.** Everything but ABP's server
process itself runs `below_normal`: ABP starts compilers, package managers and inference
servers on the same machine a person is using, and it is not more important than they are.
The affinity default leaves out the logical processors in `sandbox_ns.avoid_cpus`
(config/backends.yaml; `[]` in code) and, on a machine where the Neural Lab's stability
policy has seen machine-check errors, the ones it detected itself - this machine's faulty
cores are kept out of every child ABP starts without anyone remembering to configure it.

**Environment and network are delegated, never reimplemented.** `Policy.environment()` calls
bot/agent_runtime/sandbox.py's `build_env` (the same scrubbing of secret-looking variables,
the same `minimal`/`inherit` modes) and `Policy.offline()` calls its network launcher. A
second implementation of either would be a second, worse set of rules.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import Optional

#: Job Object priority classes, as win_job.PRIORITY_CLASSES names them. ABP's server process
#: is never in a job, so it keeps the OS default; everything it starts is below it.
PRIORITIES = ("idle", "below_normal", "normal", "above_normal", "high", "realtime")


@dataclass(frozen=True)
class Policy:
    """The limits and the lifetime of one sandbox. 0 / None / "" means "no limit set",
    never "unlimited by accident": a limit that was asked for and could not be applied is
    reported in the cell's status rather than silently dropped."""
    name: str = "custom"
    memory_mb: int = 0                       # per process; 0 = no cap
    cpu_rate_percent: float = 0.0            # hard Job Object CPU cap (cycles per 10 000); 0 = none
    max_processes: int = 0                   # how many processes the whole cell may hold
    priority: str = "below_normal"           # "below_normal" for everything but the server itself
    affinity: Optional[tuple[int, ...]] = None   # logical processors the cell may use; None = all of them
    kill_on_close: bool = True               # close the cell and everything in it dies
    timeout_s: Optional[float] = None        # how long a process in this cell may live (spawn enforces it)
    persistent: bool = False                 # a daemon meant to outlive ABP: recorded, windowless, not killed
    env_mode: Optional[str] = None           # secrets | minimal | inherit; None = the caller's own environment
    env_allow: tuple[str, ...] = ()          # names kept in a scrubbed environment
    network: Optional[str] = None            # allow | none; None = whatever sandbox.network says

    def describe(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in dataclasses.asdict(self).items()}

    # ---- environment and network: delegated to bot/agent_runtime/sandbox.py ----
    def environment(self, environ: Optional[dict] = None) -> dict:
        """The environment a process in this cell runs with. With no env_mode this is the
        caller's own environment unchanged (ABP's own services need what they need); with
        one, sandbox.build_env scrubs it exactly like an agent shell command's."""
        if self.env_mode is None:
            return dict(os.environ if environ is None else environ)
        from bot.agent_runtime import sandbox

        return sandbox.build_env(environ, {"env": {"mode": self.env_mode, "allow": list(self.env_allow)}})

    def offline(self) -> bool:
        """True when this policy asks for the network to be cut. The launcher itself is
        sandbox.py's, not this package's - a cell only says what it wants, never how."""
        if self.network is None:
            try:
                from bot.agent_runtime import sandbox

                return sandbox.network() == "none"
            except Exception:  # noqa: BLE001
                return False
        return self.network.lower() == "none"


def _default_affinity() -> Optional[tuple[int, ...]]:
    """Every logical processor except the ones ABP must keep off. None means "all of them",
    which is also what an empty or impossible result means: a mask of nothing would leave the
    process unable to run at all, which is never what avoiding a couple of cores meant."""
    cpus = set(available_cpus())
    avoid = set(avoid_cpus())
    left = tuple(sorted(cpus - avoid))
    return left or None


def available_cpus() -> tuple[int, ...]:
    """The processors this process may actually use - which on a machine with more than 64
    of them, or with a restricted process affinity already, is not 0..cpu_count-1."""
    if os.name == "nt":
        try:
            import psutil

            return tuple(psutil.Process().cpu_affinity())
        except Exception:  # noqa: BLE001
            return tuple(range(os.cpu_count() or 1))
    try:
        return tuple(sorted(os.sched_getaffinity(0)))          # type: ignore[attr-defined]
    except AttributeError:
        return tuple(range(os.cpu_count() or 1))


_detected: dict = {"stamp": 0.0, "cpus": ()}


def _detected_avoid() -> tuple[int, ...]:
    """The processors the Neural Lab's stability policy has excluded on this machine (it
    reads real machine-check errors out of the event log), cached for five minutes like its
    own policy. Best effort: an ABP without numpy, or a machine with no history, contributes
    nothing and that is fine - the config key above is the deliberate way to say it."""
    import time

    now = time.time()
    if now - _detected["stamp"] < 300:
        return _detected["cpus"]
    cpus: tuple[int, ...] = ()
    try:
        from bot.neurallab import systune

        cpus = tuple(int(c) for c in systune.cpu_policy().get("avoid") or ())
    except Exception:  # noqa: BLE001
        cpus = ()
    _detected.update(stamp=now, cpus=cpus)
    return cpus


def _configured_avoid() -> tuple[int, ...]:
    """`sandbox_ns.avoid_cpus` from config/backends.yaml - the deliberate list (this machine's
    cores 20 and 21 have thrown machine-check errors). An empty or unreadable config contributes
    nothing; that is not a reason to refuse to start anything."""
    try:
        from bot.config import config

        raw = ((config.current.get("sandbox_ns") or {}).get("avoid_cpus")) or []
        return tuple(sorted({int(c) for c in raw}))
    except Exception:  # noqa: BLE001 - no config yet (a worker, a first import)
        return ()


def avoid_cpus() -> tuple[int, ...]:
    """Logical processors no ABP child may run on: `sandbox_ns.avoid_cpus` (deliberate) plus the
    ones the stability policy detected itself (machine-check errors on this machine)."""
    return tuple(sorted(set(_configured_avoid()) | set(_detected_avoid())))


def preset(name: str) -> Policy:
    """A named policy. Raises KeyError with the list of names, because a typo here would
    otherwise mean a process silently running with no limits at all."""
    try:
        return PRESETS[name]
    except KeyError:
        raise KeyError(f"unknown sandbox preset {name!r}; use one of {', '.join(sorted(PRESETS))}") from None


# The presets, one per kind of process ABP starts. The numbers are deliberately about runaway
# processes, not about making real work fail: a preset whose cap is too low for the work is
# worse than no cap, so the light ones (a tool call, a CLI agent) carry no memory cap at all -
# their limits are the operator's to set (native_agent.sandbox.windows_job, or
# `sandbox_ns.presets`), and the cell still gives them a guaranteed tree kill, the default
# processor set and below-normal priority.
PRESETS: dict[str, Policy] = {
    # A command an agent ran: short, untrusted, and the most likely to misbehave.
    "tool": Policy(name="tool", max_processes=256, env_mode="secrets"),
    # A CLI agent backend (claude, hermes, opencode): a long conversation, but still just a client.
    "agent": Policy(name="agent", max_processes=256),
    # Compilers and test suites: the heaviest CPU ABP causes, capped and kept off the bad cores.
    "build": Policy(name="build", memory_mb=6144, cpu_rate_percent=60.0, max_processes=512, timeout_s=7200),
    # A local inference server (llama-server and friends): big, long-lived, and it sizes its own
    # memory from the model - a hard cap here would break a big model on the CPU, so the CPU rate
    # is capped instead and the memory is left to it.
    "engine": Policy(name="engine", cpu_rate_percent=75.0, max_processes=64),
    # A service that is supposed to keep running when ABP does not.
    "daemon": Policy(name="daemon", persistent=True, max_processes=256),
    # A training or lab worker: heavy, background, and never more important than the desktop.
    "worker": Policy(name="worker", memory_mb=12288, cpu_rate_percent=50.0, max_processes=128),
}
# The affinity default is resolved once, lazily: it costs a config read and (on a machine with
# a stability history) a hardware-event-log look, neither of which belongs on an import path.
_affinity_default: dict = {"policy": None}


def with_defaults(policy: Policy) -> Policy:
    """The policy as it should actually be applied: `affinity=None` means "this preset asked
    for the default processor set", which is every processor ABP is allowed to use."""
    if policy.affinity is not None:
        return policy
    cached = _affinity_default["policy"]
    if cached is None:
        cached = _default_affinity()
        _affinity_default["policy"] = cached
    return dataclasses.replace(policy, affinity=cached)


def nice_value(priority: str) -> int:
    """The POSIX nice increment for a policy's priority. Windows' below_normal has no POSIX
    equivalent; 10 is the conventional "a bit less important" and it is reversible."""
    if priority in ("idle", "below_normal"):
        return 10
    return 0          # normal, and above normal too: a child of ABP never gets more CPU than ABP asks for


def apply_overrides(policy: Policy, values: Optional[dict]) -> Policy:
    """`sandbox_ns.presets.<preset>` out of config/backends.yaml: field name -> value.
    Unknown field names are ignored rather than crashing a boot over a typo in a YAML file."""
    if not values:
        return policy
    names = {f.name for f in dataclasses.fields(Policy)}
    changes = {k: v for k, v in values.items() if k in names}
    if not changes:
        return policy
    if "affinity" in changes and isinstance(changes["affinity"], list):
        changes["affinity"] = tuple(int(c) for c in changes["affinity"]) or None
    return dataclasses.replace(policy, **changes)


def _preset_overrides() -> dict:
    """The configured per-preset field overrides, read live (config is hot-reloadable)."""
    try:
        from bot.config import config

        return dict(((config.current.get("sandbox_ns") or {}).get("presets")) or {})
    except Exception:  # noqa: BLE001 - a worker, or a boot before config exists
        return {}


def policy_for(name: str, overrides: Optional[dict] = None) -> Policy:
    """The effective policy for a preset name: the preset, its configured overrides, and the
    default processor set folded in."""
    return with_defaults(apply_overrides(preset(name), overrides if overrides is not None else _preset_overrides()))
