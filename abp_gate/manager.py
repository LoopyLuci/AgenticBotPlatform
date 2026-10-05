"""What the gate actually does: start, swap, roll back, sandbox, stop.

    start    the production instance - code root = the ABP checkout, data root =
             the real one - on a private port; routing only starts once it is
             healthy.
    swap     start a standby instance from new code on the SAME data, wait for
             it to be healthy, tell the outgoing one to release the leader
             lease, let the new one take it, wait for it to answer again, flip
             routing, drain the old one and stop it. If any step fails, the old
             one re-takes the lease, keeps serving, and the failure is reported
             with the reason.
    rollback flip routing back to the instance the last swap replaced, which is
             still alive and still has the data warm.
    sandbox a copy of the real state on its own port with ABP_SANDBOX_INSTANCE=1,
             from any code root, reachable directly and never through the public
             port.
    stop     kill one instance (or all of them) politely, optionally keeping a
             sandbox's state for next time.

Every step here is idempotent-ish and reports what it actually did, because
every one of these commands is run by a person or an agent who needs to know
whether it worked.

Two rules exist purely because the first version of this file did not have
    them, and the machine found out the hard way (thousands of orphaned
    python.exe processes in ten minutes):

  * nothing is ever replaced without its whole process TREE going first.
    `_terminate()` kills the instance's job object - the launcher, the real
    interpreter it started, and everything either of them started - then walks
    the pid tree, then kills whatever of ours is still holding its port. An
    instance is two processes, not one, on Windows.
  * there is a ceiling on how much of this can go wrong at once: a live
    instance cap and a bounded restart budget (both in limits.py), and
    `_spawn()` refusing to start anything when the cap is reached.

And one that exists because the public port and this file share an event loop:

  * everything here that waits on something - a process, a port, a probe, a
    database copy - waits in a thread (`asyncio.to_thread`). Done inline it is
    time the front door does not answer, and the front door is the one thing
    this whole package is for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import httpx

from abp_gate import limits, paths, procs, registry
from abp_gate.proxy import Router

logger = logging.getLogger("abp_gate.manager")

#: How long a new instance has to answer /healthz before a swap gives up and
#: rolls back. Generous: a cold start has to migrate the database, and an
#: OpenAPI schema build on a slow disk is not instant.
DEFAULT_HEALTH_TIMEOUT_S = 90.0
#: How long the incoming instance has to take the leader lease after the
#: outgoing one gives it up.
DEFAULT_LEASE_TIMEOUT_S = 45.0
DEFAULT_DRAIN_S = 15.0
#: How long a swap waits for the incoming instance to answer /healthz again,
#: after it has taken the lease, before routing is handed over. It is a
#: wait-for-a-condition with a deadline, not a sleep: the normal case returns as
#: soon as the instance answers (a few seconds), and the deadline only matters if
#: it never does, in which case the swap goes ahead anyway and says so.
DEFAULT_SERVE_TIMEOUT_S = 30.0

#: What happens to the instances when the gate process itself dies.
#:
#:   LIFETIME_GATE      every instance is in a kill-on-close job owned by the
#:                      gate, so a gate that is killed - even a taskkill /F,
#:                      even just its launcher - leaves nothing behind.
#: LIFETIME_DETACHED   the ACTIVE instance is deliberately started outside any
#:                      job and survives the gate, so a gate that comes straight
#:                      back can re-adopt it from the registry and ABP is up
#:                      again in seconds. Everything else (standbys, sandboxes)
#:                      still dies with the gate.
LIFETIME_GATE = "gate"
LIFETIME_DETACHED = "detached"
LIFETIMES = (LIFETIME_GATE, LIFETIME_DETACHED)


class SwapError(RuntimeError):
    """A swap that failed and was (or could not be) rolled back."""


class Manager:
    def __init__(self, router: Optional[Router] = None, *,
                 instance_lifetime: str = LIFETIME_GATE,
                 max_instances: Optional[int] = None):
        self.router = router or Router()
        self._ops = asyncio.Lock()  # one start/swap/rollback/stop at a time
        #: One job object per instance, keyed by instance name. A handle cannot
        #: be written to the registry, so a gate that is restarted comes back
        #: with none of them - see LIFETIME_DETACHED for why that is survivable.
        self._jobs: dict[str, procs.Job] = {}
        self.instance_lifetime = instance_lifetime if instance_lifetime in LIFETIMES else LIFETIME_GATE
        self._max_instances = max_instances

    # ------------------------------------------------------------- read side
    def max_instances(self) -> int:
        return self._max_instances or limits.max_instances()

    def alive_instances(self) -> list[registry.Instance]:
        return [i for i in registry.all_instances() if procs.alive(i.pid)]

    async def list(self) -> dict[str, Any]:
        data = registry.read()
        instances = [registry.Instance.from_dict(v) for v in data["instances"].values()]
        for inst in instances:
            await self._refresh_health(inst)
            if inst.name == data.get("active"):
                inst.role = registry.ROLE_ACTIVE
            registry.put(inst.name, inst)
        data = registry.read()
        alive = self.alive_instances()
        out = registry.to_jsonable(
            [registry.Instance.from_dict(v) for v in data["instances"].values()],
            data["active"], data["previous"],
        )
        out["limits"] = {
            "max_instances": self.max_instances(),
            "alive": len(alive),
            "instance_lifetime": self.instance_lifetime,
        }
        out["restarts"] = self.restart_report()
        return out

    def restart_report(self, window_s: float = registry.DEFAULT_RESTART_WINDOW_S) -> dict[str, Any]:
        """What the watcher's restart budget looks like right now, per instance.

        In the control API's output because the whole point of a circuit breaker
        is that stopping is visible: `abp_cli gate status` has to be able to say
        "it gave up after 3 restarts", not just show an instance as unhealthy."""
        limit = limits.restart_limit()
        out: dict[str, Any] = {}
        for inst in registry.all_instances():
            recent = registry.restarts_in_window(inst.name, window_s)
            out[inst.name] = {
                "restarts_last_window": len(recent),
                "budget": limit,
                "circuit_open": len(recent) >= limit,
                "ever_healthy": inst.ever_healthy,
            }
        return {"window_s": window_s, "limit": limit, "instances": out}

    def get(self, name: str) -> Optional[registry.Instance]:
        return registry.get(name)

    async def _refresh_health(self, inst: registry.Instance) -> None:
        """Truth from the OS, not from what we hope is true: is the pid alive,
        and is the port actually answering? A record saying "healthy" for a
        process that died ten minutes ago is worse than no record at all.

        The probe is a blocking HTTP call with a three second timeout, in a
        thread: it runs on the same event loop as the public port, and every
        control request asks for this, so doing it inline would make a slow
        instance a slow public port."""
        if not procs.alive(inst.pid):
            # Nothing is running, so the job has nothing left to hold: closing it
            # here is what keeps a gate that has been up for a month from
            # carrying a handle per instance it has ever lost.
            self._kill_job(inst.name)
            if inst.health not in (registry.HEALTH_STOPPED, registry.HEALTH_FAILED):
                inst.health = registry.HEALTH_STOPPED
            if inst.port and not procs.is_free(inst.port):
                # The launcher is gone but its interpreter is still serving:
                # that is the shape of "killed the wrong pid", so the port is
                # the thing to trust here.
                procs.kill_stragglers(inst.port)
            return
        if not inst.port:
            return
        probe = await asyncio.to_thread(health, inst.port)
        if probe.get("healthy"):
            inst.health = registry.HEALTH_HEALTHY
            inst.error = ""
        else:
            inst.health = registry.HEALTH_UNHEALTHY
        if probe.get("error"):
            inst.error = probe["error"]

    def logs(self, name: str, lines: int = 120) -> str:
        inst = registry.get(name)
        path = Path(inst.log) if inst and inst.log else paths.instance_log_path(name)
        return procs.tail(path, lines)

    # ------------------------------------------------------------ start side
    async def start_production(self, *, code_root: Optional[Path] = None,
                               data_root: Optional[Path] = None,
                               health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S) -> dict[str, Any]:
        """The instance everything else is measured against: this checkout, this
        machine's real state, behind the public port."""
        async with self._ops:
            code = Path(code_root or paths.code_root()).resolve()
            data = Path(data_root).resolve() if data_root else paths.state_root()
            existing = await self._find_active()
            if existing is not None and procs.alive(existing.pid):
                await self._route_to(existing)
                return {"ok": True, "instance": existing.name, "already": True,
                        "detail": f"{existing.name} is already active on port {existing.port}"}
            name = registry.unique_name("prod")
            inst = await self._spawn(name, code, data, role=registry.ROLE_ACTIVE, sandbox=False)
            await self._wait_healthy(inst, health_timeout_s)
            registry.set_active(name)
            await self._route_to(inst)
            logger.info("gate started instance %r (code=%s data=%s port=%s)", name, code, data, inst.port)
            return {"ok": True, "instance": name, "url": self.base_url(), **registry.get(name).public()}

    def base_url(self) -> str:
        ports = paths.public_ports()
        return f"http://127.0.0.1:{ports[0]}"

    # ------------------------------------------------------------------ swap
    async def swap(self, code_root: Path, *, name: Optional[str] = None,
                   data_root: Optional[Path] = None,
                   health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
                   lease_timeout_s: float = DEFAULT_LEASE_TIMEOUT_S,
                   drain_s: float = DEFAULT_DRAIN_S,
                   serve_timeout_s: float = DEFAULT_SERVE_TIMEOUT_S) -> dict[str, Any]:
        """New code in, with the public port never going quiet.

        The order matters and is the whole design:

          1. the new instance starts on the SAME data and answers its own API
             before it owns anything (it starts as a standby, so it is holding
             zero singletons);
          2. only once it is healthy does the outgoing one give up the lease,
             which stops its pollers/scheduler/etc.;
          3. the incoming one takes the lease and starts those services - and
             then has to answer /healthz again before it is given the front
             door, because those services are what it is busy starting;
          4. routing flips - a single pointer, so the next request goes to the
             new instance and nothing is closed underneath it;
          5. the outgoing one is drained and stopped.

        A failure at 1, 3 or 4 leaves the outgoing instance serving, untouched.
        A failure at 3 is the interesting one: it means the new code is healthy
        but cannot lead, so we roll back by telling the outgoing one to take the
        lease again, which costs it a few seconds of services and no downtime.
        The reason is reported either way.
        """
        code = Path(code_root).resolve()
        data = Path(data_root).resolve() if data_root else paths.state_root()
        async with self._ops:
            outgoing = await self._find_active()
            new_name = name or registry.unique_name("swap")

            steps: list[str] = []
            inst = None
            try:
                inst = await self._spawn(new_name, code, data, role=registry.ROLE_STANDBY, sandbox=False, standby=True)
                await self._wait_healthy(inst, health_timeout_s)
                steps.append(f"{new_name} healthy on port {inst.port}")
            except Exception as exc:
                await self._abandon(inst, steps, f"the new instance never became healthy: {exc}")
                raise SwapError(f"swap to {code} failed at step 1 ({exc}); {outgoing.name if outgoing else 'nothing'} "
                                f"is still serving") from exc

            if outgoing is not None:
                await self._lease(outgoing, "release")
                steps.append(f"{outgoing.name} released the leader lease")
            try:
                await self._lease(inst, "take", lease_timeout_s)
                steps.append(f"{new_name} took the leader lease")
            except Exception as exc:
                if outgoing is not None:
                    await self._lease(outgoing, "take", lease_timeout_s)
                    steps.append(f"rolled back: {outgoing.name} took the leader lease again")
                await self._abandon(inst, steps, f"the new instance would not take the leader lease: {exc}")
                raise SwapError(f"swap to {code} rolled back at step 3 ({exc}); "
                                f"{outgoing.name if outgoing else 'nothing'} is still serving") from exc

            if not await self._wait_serving(inst, serve_timeout_s):
                steps.append(f"{new_name} did not answer /healthz again; routing anyway, "
                             f"and {outgoing.name if outgoing else 'the front door'} was serving until now")
            registry.set_active(new_name, previous=outgoing.name if outgoing else None)
            await self._route_to(inst)
            steps.append(f"routing now points at {new_name}")

            if outgoing is not None:
                left = await self.router.drain("dashboard", drain_s)
                steps.append(f"drained {outgoing.name} ({left} request(s) still in flight)" if left
                             else f"drained {outgoing.name}")
                procs.set_priority(outgoing.pid, below_normal=True)
                logger.info("stopping outgoing instance %r (pid=%s, port=%s)", outgoing.name, outgoing.pid, outgoing.port)
                await self._terminate(outgoing)
                current = registry.get(outgoing.name)
                if current is not None:
                    current.health = registry.HEALTH_STOPPED
                    current.role = registry.ROLE_STANDBY
                    registry.put(outgoing.name, current)
                steps.append(f"stopped {outgoing.name}")
            logger.info("swapped to %r from %s", new_name, code)
            return {
                "ok": True,
                "instance": new_name,
                "previous": outgoing.name if outgoing else None,
                "code_root": str(code),
                "url": self.base_url(),
                "steps": steps,
            }

    async def rollback(self, *, lease_timeout_s: float = DEFAULT_LEASE_TIMEOUT_S,
                       drain_s: float = DEFAULT_DRAIN_S) -> dict[str, Any]:
        """Point routing back at the instance the last swap replaced.

        Only works while that instance is still alive. If it is not, there is
        nothing to roll back TO, and saying so plainly beats starting the newest
        code and calling it a rollback."""
        async with self._ops:
            data = registry.read()
            target_name = data.get("previous")
            if not target_name:
                raise SwapError("nothing to roll back to: no swap has been recorded yet")
            target = registry.get(target_name)
            if target is None:
                raise SwapError(f"the instance {target_name!r} is no longer in the registry")
            if not procs.alive(target.pid):
                raise SwapError(
                    f"{target_name} is no longer running, so there is nothing to roll back to. "
                    f"Swap to the code you want instead: abp_cli instance swap <code-root>"
                )
            outgoing = await self._find_active()
            steps: list[str] = []
            if outgoing is not None and outgoing.name != target_name:
                await self._lease(outgoing, "release")
                steps.append(f"{outgoing.name} released the leader lease")
            try:
                await self._lease(target, "take", lease_timeout_s)
            except Exception as exc:
                if outgoing is not None and outgoing.name != target_name:
                    await self._lease(outgoing, "take", lease_timeout_s)
                    steps.append(f"rolled back the rollback: {outgoing.name} leads again")
                raise SwapError(f"{target_name} could not take the leader lease ({exc})") from exc
            steps.append(f"{target_name} took the leader lease")
            registry.set_active(target_name, previous=outgoing.name if outgoing and outgoing.name != target_name else None)
            await self._route_to(target)
            steps.append(f"routing now points at {target_name}")
            if outgoing is not None and outgoing.name != target_name:
                left = await self.router.drain("dashboard", drain_s)
                steps.append(f"drained {outgoing.name} ({left} still in flight)" if left else f"drained {outgoing.name}")
                await self._terminate(outgoing)
                cur = registry.get(outgoing.name)
                if cur is not None:
                    cur.health = registry.HEALTH_STOPPED
                    registry.put(outgoing.name, cur)
                steps.append(f"stopped {outgoing.name}")
            return {"ok": True, "instance": target_name, "previous": None, "steps": steps}

    # --------------------------------------------------------------- sandbox
    async def sandbox(self, code_root: Path, *, name: Optional[str] = None,
                  keep_state: bool = False) -> dict[str, Any]:
        """An agent's own ABP: new code, its own copy of the state, its own port,
        ABP_SANDBOX_INSTANCE=1. Reachable directly - never routed to from the
        public port, because nothing but the agent should be able to end up
        talking to a dev instance by accident."""
        code = Path(code_root).resolve()
        sandbox_name = name or code.name
        async with self._ops:
            # Stop any existing sandbox with the same name to avoid file locks
            existing = registry.get(sandbox_name)
            if existing is not None and procs.alive(existing.pid):
                await self.stop(sandbox_name, keep_state=keep_state)
            data_root = paths.instance_state_dir(sandbox_name)
            if not keep_state or not (data_root / "data" / "bot.db").is_file():
                # In a thread: this reads the live database through SQLite's
                # backup API, which is as slow as the database is big, and the
                # public port is waiting on the same loop.
                await asyncio.to_thread(seed_state, paths.state_root(), data_root)
            inst = await self._spawn(sandbox_name, code, data_root, role=registry.ROLE_SANDBOX, sandbox=True)
            await self._wait_healthy(inst, timeout_s=DEFAULT_HEALTH_TIMEOUT_S)
            url = f"http://127.0.0.1:{inst.port}"
            return {
                "ok": True,
                "instance": sandbox_name,
                "url": url,
                "code_root": str(code),
                "data_root": str(data_root),
                "token_var": "DASHBOARD_TOKEN",
                "note": "sandboxed: no outward connectors, no leader lease; ABP_SANDBOX_INSTANCE=1",
                **registry.get(sandbox_name).public(),
            }

    # ------------------------------------------------------------------ stop
    async def stop(self, name: str, *, keep_state: bool = True) -> dict[str, Any]:
        async with self._ops:
            inst = registry.get(name)
            if inst is None:
                raise SwapError(f"no instance named {name!r}")
            was_active = registry.active_name() == name
            await self._terminate(inst)
            if inst.sandbox and not keep_state:
                shutil.rmtree(paths.instance_state_dir(name), ignore_errors=True)
            registry.drop(name)
            if was_active:
                self.router.set("dashboard", None)
                if inst.localai_port:
                    self.router.set("localai", None)
            return {"ok": True, "stopped": name, "was_active": was_active, "state_kept": bool(keep_state or not inst.sandbox)}

    async def stop_all(self) -> dict[str, Any]:
        stopped = []
        for inst in registry.all_instances():
            try:
                stopped.append((await self.stop(inst.name, keep_state=True))["stopped"])
            except SwapError:
                continue
        return {"ok": True, "stopped": stopped}

    async def restart_active(self, *, attempts: int = 3) -> dict[str, Any]:
        """The crashed-active-instance case. Same data, same code, same public
        port - so from outside, nothing changed; there is just a second or two
        of 503 while the new process boots. A restart that keeps failing is left
        failed rather than retried forever, because a loop that can't bind its
        port or can't open its database will never start working on its own.

        The old instance's entire tree is dead BEFORE the replacement starts.
        Not "shortly after": an instance is a launcher plus an interpreter, and
        a replacement that starts while the old interpreter is still holding the
        port is a second writer of the same database. Each attempt cleans up its
        own failure too, so a failed restart leaves one stopped instance rather
        than a new pile of them. How OFTEN this may be called is the watcher's
        business (abp_gate/__main__.py), not this method's."""
        async with self._ops:
            inst = await self._find_active()
            if inst is None:
                raise SwapError("no active instance to restart")
            code = Path(inst.code_root)
            data = Path(inst.data_root) if inst.data_root else None
            errors: list[str] = []
            for attempt in range(1, attempts + 1):
                fresh: Optional[registry.Instance] = None
                try:
                    await self._terminate(inst)
                    fresh = await self._spawn(registry.unique_name(inst.name), code, data,
                                              role=registry.ROLE_ACTIVE, sandbox=False)
                    await self._wait_healthy(fresh, DEFAULT_HEALTH_TIMEOUT_S)
                    registry.set_active(fresh.name, previous=inst.name)
                    await self._route_to(fresh)
                    procs.set_priority(fresh.pid, below_normal=False)
                    logger.warning("restarted crashed active instance %r as %r (attempt %s)",
                                   inst.name, fresh.name, attempt)
                    return {"ok": True, "instance": fresh.name, "attempt": attempt}
                except Exception as exc:  # noqa: BLE001 - every failure is retried
                    errors.append(f"attempt {attempt}: {exc}")
                    logger.exception("restart of %r failed on attempt %s", inst.name, attempt)
                    if fresh is not None:
                        # Otherwise attempt 2 starts on top of attempt 1's corpse.
                        await self._abandon(fresh, [], f"restart attempt {attempt} failed")
            inst.health = registry.HEALTH_FAILED
            inst.error = "; ".join(errors)[-800:]
            registry.put(inst.name, inst)
            self.router.set("dashboard", None)
            raise SwapError(f"could not restart {inst.name}: {'; '.join(errors)}")

    # ------------------------------------------------------------- internals
    async def _find_active(self) -> Optional[registry.Instance]:
        name = registry.active_name()
        if not name:
            return None
        inst = registry.get(name)
        if inst is None:
            return None
        await self._refresh_health(inst)
        registry.put(inst.name, inst)
        return inst

    async def _spawn(self, name: str, code_root: Path, data_root: Path, *, role: str,
                     sandbox: bool, standby: bool = False) -> registry.Instance:
        if not (code_root / "bot" / "main.py").is_file():
            raise SwapError(f"{code_root} is not an ABP checkout (no bot/main.py)")
        self._check_capacity(name)
        port = registry.free_port()
        localai_port = 0
        extra: dict[str, str] = {}
        if paths.LOCALAI_PORT in paths.public_ports():
            # The gate owns the public 11436, so the instance must bind a
            # private one instead - otherwise the two would fight for it.
            localai_port = paths.private_localai_port()
        if sandbox:
            extra["ABP_SANDBOX_INSTANCE"] = "1"
            extra["ABP_STANDBY"] = "1"
        log_path = paths.instance_log_path(name)
        python = procs.python_for(code_root)
        argv = procs.instance_argv(python, standby=standby)
        env = procs.instance_env(code_root=code_root, data_root=data_root, port=port,
                                 localai_port=localai_port, extra=extra)
        inst = registry.Instance(
            name=name,
            code_root=str(code_root),
            data_root=str(data_root),
            port=port,
            role=role,
            health=registry.HEALTH_STARTING,
            started=registry.stamp(),
            sandbox=sandbox,
            standby=standby,
            localai_port=localai_port,
            log=str(log_path),
        )
        registry.put(name, inst)
        # The active instance in `detached` mode is deliberately started OUTSIDE
        # a job: it has to outlive the gate (see LIFETIME_DETACHED), and a
        # kill-on-close job would take it with the gate's handle. Everything
        # else gets one, which is what makes a hard gate death take the rest.
        detached = role == registry.ROLE_ACTIVE and self.instance_lifetime == LIFETIME_DETACHED
        job = None if detached else procs.Job(f"instance:{name}")
        # Below-normal for everything except the active instance: a standby
        # waiting to be swapped in, or an agent's sandbox, must never make the
        # machine feel slow for whoever is actually using ABP.
        #
        # In a thread, because it blocks: procs.spawn waits a moment to see
        # whether the process dies on the spot, and absorbs the launcher's tree
        # into the job, and both of those are seconds of wall clock. This runs on
        # the event loop that also serves the public port, and a swap that makes
        # the front door go quiet while it starts something is a swap that has
        # broken the one promise the gate makes.
        try:
            pid = await asyncio.to_thread(
                procs.spawn, argv, cwd=code_root, env=env, log_path=log_path,
                below_normal=(role != registry.ROLE_ACTIVE), job=job,
            )
        except Exception:
            # Never leave a record or a job behind for something that is not
            # running: an entry with no pid and no job is a mystery later.
            registry.drop(name)
            if job is not None:
                job.kill()
            raise
        if job is not None:
            self._jobs[name] = job
        inst.pid = pid
        registry.put(name, inst)
        return inst

    def _check_capacity(self, name: str) -> None:
        """Refuse to become one process too many.

        The cap exists because the alternative was measured: a gate that starts
        instances without ever stopping them reaches six thousand of them on a
        machine with 32 GB, and the failure mode is the whole machine, not the
        gate."""
        limit = self.max_instances()
        alive = [i for i in self.alive_instances() if i.name != name]
        if len(alive) >= limit:
            raise SwapError(
                f"the gate already has {len(alive)} instance(s) alive and the cap is {limit}; "
                f"stop one first (abp_cli instance list, then abp_cli instance stop <name>)"
            )

    def _kill_job(self, name: str) -> None:
        job = self._jobs.pop(name, None)
        if job is not None:
            job.kill()

    async def _terminate(self, inst: Optional[registry.Instance]) -> list[int]:
        """Stop one instance, completely, and say what was left over.

        Job first (that is the whole tree in one call), then the pid tree for
        platforms without jobs and for anything the job missed, then anything of
        ours still holding the port - because an interpreter whose launcher is
        gone is exactly the process a pid-only kill leaves behind.

        The stopping itself runs in a thread: `procs.stop` waits on a tree,
        `kill_stragglers` runs netstat, and `wait_port_closed` waits on the OS,
        and on a loaded machine that is tens of seconds in the worst case. All of
        it on the event loop is all of it time the public port does not answer."""
        if inst is None:
            return []
        self._kill_job(inst.name)
        leftover = await asyncio.to_thread(_stop_processes, inst)
        if leftover:
            logger.warning("instance %r: %s process(es) outlived the stop: %s", inst.name, len(leftover), leftover)
        current = registry.get(inst.name)
        if current is not None and current.health != registry.HEALTH_FAILED:
            current.health = registry.HEALTH_STOPPED
            registry.put(inst.name, current)
        return leftover

    async def _wait_healthy(self, inst: registry.Instance, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last = "no answer yet"
        while time.monotonic() < deadline:
            if inst.pid and not procs.alive(inst.pid):
                raise SwapError(f"{inst.name} exited while starting:\n{procs.tail(Path(inst.log), 25)}")
            probe = await asyncio.to_thread(health, inst.port)
            if probe.get("healthy"):
                inst.health = registry.HEALTH_HEALTHY
                inst.error = ""
                inst.ever_healthy = True
                registry.put(inst.name, inst)
                # Healthy again: whatever restart budget this instance had was
                # for a problem it no longer has.
                registry.clear_restarts(inst.name)
                procs.set_priority(inst.pid, below_normal=(inst.role != registry.ROLE_ACTIVE))
                return probe
            last = probe.get("error") or last
            await asyncio.sleep(0.25)
        raise SwapError(f"{inst.name} did not answer /healthz within {timeout_s:.0f}s ({last})")

    async def _wait_serving(self, inst: registry.Instance,
                            timeout_s: float = DEFAULT_SERVE_TIMEOUT_S) -> bool:
        """Wait until `inst` answers /healthz. False if it never does.

        The health check that passed a moment ago said this instance's port was
        answering - but that was BEFORE it took the leader lease, and taking the
        lease is precisely what starts the lease-gated services: a long sequence
        of blocking work on that instance's own event loop, measured at 2.5-3.1s
        (and much longer on a loaded machine). Flipping routing into an instance
        that cannot answer for the next few seconds is the outage a swap exists
        to prevent, and the instance being replaced is still serving and keeps
        serving until the pointer moves - so waiting here costs nothing and
        removing the stall costs every client of the public port.

        Best effort on purpose: a slow answer is not on its own a reason to undo
        a handover that has already happened (that costs more downtime than it
        saves), so this reports the wait rather than raising. The caller puts it
        in the swap's own step list, where a person can see it."""
        deadline = time.monotonic() + timeout_s
        while True:
            if procs.alive(inst.pid) and (await asyncio.to_thread(health, inst.port)).get("healthy"):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)

    async def _route_to(self, inst: registry.Instance) -> None:
        self.router.set("dashboard", str(inst.port))
        self.router.set("localai", str(inst.localai_port) if inst.localai_port else None)

    async def _abandon(self, inst: Optional[registry.Instance], steps: list[str], why: str) -> None:
        if inst is not None:
            await self._terminate(inst)
            registry.drop(inst.name)
            logger.warning("swap abandoned %r: %s", inst.name, why)
            steps.append(f"discarded {inst.name}: {why}")
        else:
            logger.warning("swap abandoned before instance creation: %s", why)
            steps.append(f"discarded (instance not created): {why}")

    async def _lease(self, inst: registry.Instance, action: str, timeout_s: float = DEFAULT_LEASE_TIMEOUT_S) -> dict:
        """Ask an instance's own /api/lease endpoint to give up or take the lease.

        Deliberately over the instance's own API rather than by touching its
        lease file: the instance has to stop its own pollers, and only it knows
        what it started."""
        if not inst.port:
            raise SwapError(f"{inst.name} has no port")
        url = f"http://127.0.0.1:{inst.port}/api/lease/{action}"
        try:
            async with httpx.AsyncClient(timeout=timeout_s + 10.0) as client:
                resp = await client.post(url, params={"timeout": timeout_s} if action == "take" else None,
                                        headers=await dashboard_headers())
        except httpx.HTTPError as exc:
            raise SwapError(f"{inst.name} did not answer /api/lease/{action}: {exc}") from exc
        if resp.status_code >= 400:
            raise SwapError(f"{inst.name} refused /api/lease/{action}: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        if action == "take" and not body.get("singletons_running"):
            raise SwapError(f"{inst.name} would not take the leader lease: {body.get('error') or 'the lease stayed held'}")
        return body


async def dashboard_headers() -> dict[str, str]:
    """The dashboard token, for talking to an instance's own API.

    Read the same way the CLI and the desktop app read it (bot.envfile's
    resolver), never logged, never put in a command line."""
    token = dashboard_token()
    return {"X-Dashboard-Token": token} if token else {}


def dashboard_token() -> str:
    try:
        from bot import envfile

        return envfile.get_var("DASHBOARD_TOKEN") or ""
    except Exception:  # noqa: BLE001 - the gate must work without bot.* importable
        return _token_from_env_file(paths.state_root() / ".env")


def _token_from_env_file(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("DASHBOARD_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def health(port: int, timeout: float = 3.0) -> dict[str, Any]:
    """Is this instance really serving? /healthz also reports whether its own
    database opens, which is the difference between "the process is up" and
    "ABP works"."""
    out: dict[str, Any] = {"port": port, "healthy": False, "error": ""}
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=timeout)
    except httpx.HTTPError as exc:
        out["error"] = str(exc)
        return out
    if resp.status_code >= 400:
        out["error"] = f"/healthz returned {resp.status_code}"
        return out
    try:
        body = resp.json()
    except ValueError:
        out["error"] = "/healthz did not return JSON"
        return out
    out["healthy"] = bool(body.get("db_ok", body.get("status") == "ok"))
    out["status"] = body.get("status")
    out["db_ok"] = body.get("db_ok")
    if not out["healthy"]:
        out["error"] = f"/healthz says {body.get('status')!r} (db_ok={body.get('db_ok')})"
    return out


def _stop_processes(inst: registry.Instance) -> list[int]:
    """The blocking half of stopping one instance, and the pids that outlived it.

    Everything here waits on something the operating system does at its own pace:
    a tree being signalled, a job's members being reaped, `netstat` answering, a
    socket leaving TIME_WAIT. It is a module function rather than a method so
    Manager._terminate can hand the whole of it to a thread - see its docstring
    for why that matters (the same loop serves the public port)."""
    leftover: list[int] = []
    if inst.pid:
        leftover = procs.tree_pids(inst.pid)
    procs.stop(inst.pid)
    if inst.port:
        procs.kill_stragglers(inst.port)
        procs.wait_port_closed(inst.port)
        leftover = [pid for pid in leftover if procs.alive(pid)]
    return leftover


# --------------------------------------------------------------- state copy
#: Copied verbatim from the real state. Everything here is either small and
#: read-mostly (config, secrets the sandbox needs to decrypt its own vault) or
#: the database itself - which goes through SQLite's online backup API so a
#: write in flight is captured consistently instead of producing a torn file.
COPY_FILES = (".env",)
COPY_DIRS = ("config",)
SKIP_DIRS = ("logs", "backups", "snapshots", "attachments", "instances", "module-checkouts", "caches", "cache")


def seed_state(source: Path, dest: Path) -> dict[str, Any]:
    """Copy the real state into a sandbox's own directory.

    Caches, logs, backups and model/attachment blobs are skipped on purpose:
    they are large, they are rebuilt on demand, and copying them is how a
    sandbox ends up serving stale files that look authoritative."""
    # Start from nothing: a half-removed directory from a previous sandbox is
    # how a "fresh" sandbox ends up serving the last one's leftovers.
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "data").mkdir(parents=True, exist_ok=True)
    (dest / "logs").mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    skipped: list[str] = []

    for name in COPY_FILES:
        src = source / name
        if src.is_file():
            shutil.copy2(src, dest / name)
            copied.append(name)
        else:
            skipped.append(name)
    for name in COPY_DIRS:
        src = source / name
        if not src.is_dir():
            continue
        shutil.copytree(src, dest / name, ignore=shutil.ignore_patterns(*SKIP_DIRS, ".tmp"))
        copied.append(name)
    for db_name in ("bot.db", "provider_store.db"):
        src = source / "data" / db_name
        if not src.is_file():
            continue
        try:
            online_backup(src, dest / "data" / db_name)
        except OSError as exc:
            # Almost always "the sandbox's last process still has this file
            # open", which is worth saying out loud rather than surfacing as a
            # bare WinError from deep inside the copy.
            raise SwapError(
                f"could not seed {dest / 'data' / db_name} from the live state ({exc}); "
                f"stop the sandbox {dest.name!r} and try again"
            ) from exc
        copied.append(f"data/{db_name}")

    manifest = dest / "data" / "sandbox.json"
    manifest.write_text(
        '{"seeded_from": %s, "seeded_at": %s, "copied": %s, "skipped": %s}'
        % (_json_str(str(source)), float(time.time()), _json_str(copied), _json_str(skipped)),
        encoding="utf-8",
    )
    return {"data_root": str(dest), "copied": copied, "skipped": skipped}


def online_backup(src: Path, dest: Path) -> None:
    """SQLite's own online backup API, not a file copy: the real instance is
    running and writing while this happens, and a plain copy of a live WAL
    database gives you a file whose -wal and main pages disagree.

    Written to a temporary name and moved into place, so a failure part way
    through can never leave a half-copied database where the next start would
    happily open it. `closing()` matters on Windows: `with sqlite3.connect(...)`
    only ends the transaction, so the file handle would still be open when the
    rename happens, and the rename would fail with a sharing violation."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.{uuid4().hex}.tmp")
    try:
        with closing(sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=15)) as source_conn:
            with closing(sqlite3.connect(tmp)) as dest_conn:
                source_conn.backup(dest_conn)
                dest_conn.commit()
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _json_str(value: Any) -> str:
    return json.dumps(value)