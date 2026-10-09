"""The transaction engine (docs/runtime.md, "The transaction engine").

Every change is "go from the live manifest to a target manifest": plan, stage in parallel, verify, carry state,
commit atomically, drain, keep standby. Anything that fails before the commit aborts with a report and the live
system is untouched. Only one transaction runs at a time (a lock is a FIFO queue); the commit itself is one
synchronous block with no `await`, so the dispatcher, tool, panel and overlay tables change together and the
generation is bumped once.

Notes on choices the spec leaves open:
* A replaced instance's tasks are cancelled when it starts draining; the new version's tasks start at commit.
* A shadow preview does not register tools or panels and does not run tasks; its state is copy-on-write. Any commit
  to live ends all previews.
* Standby builds are reused by a rollback (their `start` runs again); they expire after `standby_window` seconds.
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import FastAPI

from bot.runtime.dispatcher import Dispatcher, build_table
from bot.runtime.manifest import LIVE, PREVIOUS, BranchStore, InstanceSpec, Manifest, valid_branch_name
from bot.runtime.registries import Registries, Snapshot, deep_merge
from bot.runtime.state import StateStore
from bot.runtime.tasks import TaskSupervisor
from bot.runtime.unit import (ACTIVE, DRAINING, STANDBY, STOPPED, Health, Instance, UnitContext, make_ref,
                              split_ref)

logger = logging.getLogger("bot.runtime.engine")

DRAIN_GROUP = "runtime:drain"


class UnitFactories:
    """Maps unit id + version to a callable that builds the Unit object (tests register toy units here)."""

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], Callable[[InstanceSpec], Any]] = {}

    def register(self, unit_id: str, factory: Callable[[InstanceSpec], Any], version: str = "*") -> None:
        self._factories[(unit_id, version)] = factory

    def unregister(self, unit_id: str, version: str = "*") -> None:
        self._factories.pop((unit_id, version), None)

    def build(self, spec: InstanceSpec) -> Any:
        factory = self._factories.get((spec.unit, spec.version)) or self._factories.get((spec.unit, "*"))
        if factory is None:
            raise LookupError(f"no factory for unit {spec.unit!r} version {spec.version!r}")
        return factory(spec)


@dataclass
class Op:
    action: str                    # load | unload | swap | reconfigure
    ref: str
    from_version: Optional[str] = None
    to_version: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "ref": self.ref, "from": self.from_version, "to": self.to_version}


@dataclass
class Plan:
    ops: list = field(default_factory=list)
    config_changed: bool = False

    @property
    def empty(self) -> bool:
        return not self.ops and not self.config_changed

    def to_dict(self) -> dict[str, Any]:
        return {"ops": [o.to_dict() for o in self.ops], "config_changed": self.config_changed}


@dataclass
class Report:
    label: str
    ok: bool = True
    generation: int = 0
    manifest_id: str = ""
    plan: Optional[Plan] = None
    error: str = ""
    failed: dict = field(default_factory=dict)       # ref -> error
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "ok": self.ok, "generation": self.generation, "manifest_id": self.manifest_id,
                "plan": self.plan.to_dict() if self.plan else None, "error": self.error, "failed": self.failed,
                "duration": round(self.duration, 4)}


class StageError(Exception):
    def __init__(self, failed: dict[str, str]) -> None:
        super().__init__("; ".join(f"{r}: {e}" for r, e in failed.items()))
        self.failed = failed


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class RuntimeEngine:
    def __init__(self, *, factories: UnitFactories, store: StateStore, branches: BranchStore,
                 dispatcher: Optional[Dispatcher] = None, registries: Optional[Registries] = None,
                 supervisor: Optional[TaskSupervisor] = None, on_commit: Optional[Callable[[dict], Any]] = None,
                 drain_deadline: float = 30.0, standby_window: float = 600.0, health_timeout: float = 10.0,
                 stage_timeout: float = 30.0) -> None:
        self.factories = factories
        self.store = store
        self.branches = branches
        self.dispatcher = dispatcher or Dispatcher()
        self.registries = registries or Registries()
        self.supervisor = supervisor or TaskSupervisor()
        self.on_commit = on_commit
        self.drain_deadline = drain_deadline
        self.standby_window = standby_window
        self.health_timeout = health_timeout
        self.stage_timeout = stage_timeout
        self.generation = 0
        self.last_transaction: Optional[Report] = None
        self.boot_error = ""                  # set by the application when its boot failed (shown in status)
        self._manifest = Manifest()
        self._active: dict[str, Instance] = {}
        self._standby: dict[str, tuple[Instance, float]] = {}
        self._previews: dict[str, dict[str, Instance]] = {}
        self._failures: dict[str, str] = {}
        self._events: list[dict] = []
        self._lock = asyncio.Lock()
        self._serial = itertools.count(1)

    # ---------------------------------------------------------------- reading
    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def effective_config(self) -> dict:
        """Unit overlays as defaults, the manifest's own config on top."""
        return deep_merge(dict(self.registries.snapshot.overlay), self._manifest.config)

    def plan(self, target: Manifest) -> Plan:
        live = self._manifest.instances
        ops: list[Op] = []
        for ref in sorted(set(live) | set(target.instances)):
            old, new = live.get(ref), target.instances.get(ref)
            if old is None:
                ops.append(Op("load", ref, None, new.version))
            elif new is None:
                ops.append(Op("unload", ref, old.version, None))
            elif old.version != new.version:
                ops.append(Op("swap", ref, old.version, new.version))
            elif old.settings != new.settings:
                ops.append(Op("reconfigure", ref, old.version, new.version))
        return Plan(ops, target.config != self._manifest.config)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "generation": self.generation, "manifest_id": self._manifest.id,
            "units": [i.describe() for _r, i in sorted(self._active.items())],
            "standby": [i.describe() for r, (i, exp) in sorted(self._standby.items()) if exp > now],
            "failed": dict(self._failures), "boot_error": self.boot_error, "previews": sorted(self._previews),
            "tasks": self.supervisor.total(),
            "last_transaction": self.last_transaction.to_dict() if self.last_transaction else None,
        }

    def ui_manifest(self) -> dict[str, Any]:
        return {"generation": self.generation, "panels": [dict(p) for p in self.registries.snapshot.panels]}

    def events_since(self, generation: int) -> list[dict]:
        keys = ("generation", "added", "removed", "changed")
        return [{k: e[k] for k in keys} for e in self._events if e["generation"] > generation]

    async def settle(self) -> None:
        """Wait for drains started by earlier commits (tests; graceful shutdown)."""
        await self.supervisor.wait_group(DRAIN_GROUP)

    # ---------------------------------------------------------------- transactions
    async def apply(self, target: Manifest, *, label: str = "apply") -> Report:
        async with self._lock:
            return await self._transact(target, label)

    async def _transact(self, target: Manifest, label: str) -> Report:
        started = time.monotonic()
        plan = self.plan(target)
        report = Report(label, generation=self.generation, manifest_id=self._manifest.id, plan=plan)
        if plan.empty:
            return self._finish(report, started)
        staged: dict[str, Instance] = {}
        try:
            self._set_states({o.ref: "loading" for o in plan.ops if o.action == "load"})
            staged = await self._stage(plan, target)
            self.dispatcher.set_staged(build_table(staged.values()))
            await self._verify(staged)
            await self._carry_state(plan, staged)
            new_active = {r: i for r, i in self._active.items()
                          if r in target.instances and r not in staged}
            new_active.update(staged)
            snapshot = self.registries.prepare(i for r, i in new_active.items())
            self._persist(target)
        except asyncio.CancelledError:
            await asyncio.shield(self._abort(staged))
            raise
        except Exception as exc:  # noqa: BLE001 - any failure before the commit aborts with a report
            await self._abort(staged)
            report.ok, report.error = False, str(exc)
            report.failed = dict(exc.failed) if isinstance(exc, StageError) else {}
            self._failures.update(report.failed)
            self._set_states({})
            logger.warning("transaction %s aborted: %s", label, exc)
            return self._finish(report, started)
        finally:
            self.dispatcher.set_staged(None)
        self._commit(plan, target, staged, new_active, snapshot, report)
        return self._finish(report, started)

    def _finish(self, report: Report, started: float) -> Report:
        report.duration = time.monotonic() - started
        self.last_transaction = report
        return report

    # -- 2. stage
    async def _stage(self, plan: Plan, target: Manifest) -> dict[str, Instance]:
        refs = [o.ref for o in plan.ops if o.action != "unload"]
        results = await asyncio.gather(*(self._build(r, target.instances[r]) for r in refs), return_exceptions=True)
        built = {r: res for r, res in zip(refs, results) if isinstance(res, Instance)}
        failed = {r: f"{type(res).__name__}: {res}" for r, res in zip(refs, results) if not isinstance(res, Instance)}
        if failed:
            await self._abort(built)
            raise StageError(failed)
        return built

    async def _build(self, ref: str, spec: InstanceSpec, *, state: Any = None, reuse_ok: bool = True) -> Instance:
        entry = self._standby.pop(ref, None) if reuse_ok else None
        unit = None
        if entry is not None and entry[1] > time.monotonic():
            if entry[0].spec == spec:
                unit = entry[0].unit
            else:
                self._standby[ref] = entry           # a different build: leave it for a later rollback
        from_standby = unit is not None
        if unit is None:
            unit = self.factories.build(spec)
        unit_id, instance_id = split_ref(ref)
        group = f"{ref}#{next(self._serial)}"
        ctx = UnitContext(unit_id, instance_id, spec.version, dict(spec.settings),
                          state if state is not None else self.store.namespace(ref),
                          lambda fn, name=None, g=group: self.supervisor.spawn(g, fn, name))
        inst = Instance(ref, spec.version, dict(spec.settings), unit, ctx, group)
        inst.spec = spec
        inst.from_standby = from_standby
        try:
            inst.contributions = unit.contributions(ctx)
            inst.app = self._asgi(inst.contributions.routes)
            await asyncio.wait_for(unit.start(ctx), self.stage_timeout)
            inst.started = True
        except BaseException:
            await self._dispose(inst)
            raise
        return inst

    @staticmethod
    def _asgi(router: Any) -> Any:
        if router is None:
            return None
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(router)
        return app

    async def _dispose(self, inst: Instance) -> None:
        """Stop a never-activated instance; a reused standby build goes back to standby."""
        await self.supervisor.cancel_group(inst.group, 1.0)
        if inst.started:
            try:
                await asyncio.wait_for(inst.unit.stop(inst.ctx, deadline=time.monotonic() + 5), 5.0)
            except Exception:  # noqa: BLE001
                logger.exception("stopping %s failed", inst.ref)
        inst.slot = STOPPED
        inst.started = False
        if inst.from_standby:
            inst.slot = STANDBY
            self._standby[inst.ref] = (inst, time.monotonic() + self.standby_window)

    async def _abort(self, staged: dict[str, Instance]) -> None:
        for inst in staged.values():
            if inst.slot != STOPPED and inst.slot != STANDBY:
                await self._dispose(inst)

    # -- 3. verify
    async def _verify(self, staged: dict[str, Instance]) -> None:
        async def one(inst: Instance) -> Health:
            try:
                health = await asyncio.wait_for(inst.unit.health(inst.ctx), self.health_timeout)
            except asyncio.TimeoutError:
                health = Health("failed", f"health check timed out after {self.health_timeout}s")
            except Exception as exc:  # noqa: BLE001
                health = Health("failed", f"{type(exc).__name__}: {exc}")
            inst.health = health
            return health

        results = await asyncio.gather(*(one(i) for i in staged.values()))
        failed = {i.ref: h.detail or "health check failed" for i, h in zip(staged.values(), results) if not h.passes}
        if failed:
            raise StageError(failed)

    # -- 4. carry state
    async def _carry_state(self, plan: Plan, staged: dict[str, Instance]) -> None:
        for op in plan.ops:
            old = self._active.get(op.ref)
            if op.action not in ("swap", "reconfigure") or old is None:
                continue
            new = staged[op.ref]
            export = getattr(old.unit, "export_state", None)
            importer = getattr(new.unit, "import_state", None)
            if export is None or importer is None:
                continue
            try:
                state = await _maybe_await(export(old.ctx))
                await _maybe_await(importer(new.ctx, state or {}))
            except Exception as exc:  # noqa: BLE001
                raise StageError({op.ref: f"state carry failed: {type(exc).__name__}: {exc}"}) from exc

    def _persist(self, target: Manifest) -> None:
        self.branches.save(PREVIOUS, self._manifest)
        self.branches.save(LIVE, target)

    # -- 5. commit (synchronous: no await, so every table changes together)
    def _commit(self, plan: Plan, target: Manifest, staged: dict[str, Instance], new_active: dict[str, Instance],
                snapshot: Snapshot, report: Report) -> None:
        replaced = [self._active[o.ref] for o in plan.ops if o.ref in self._active and o.action != "load"]
        self._active = new_active
        self._manifest = target
        for ref in staged:
            self._failures.pop(ref, None)
        self.dispatcher.publish(build_table(new_active.values(), self._states()))
        self.registries.publish(snapshot)
        self.generation += 1
        for inst in staged.values():
            inst.slot = ACTIVE
            for fn in inst.contributions.tasks:
                self.supervisor.spawn(inst.group, fn)
        previews = list(self._previews)
        for name in previews:
            self.dispatcher.set_branch(name, None)
        stale = [i for name in previews for i in self._previews.pop(name).values()]
        keep = {o.ref for o in plan.ops if o.action in ("swap", "reconfigure")}
        for inst in replaced:
            inst.slot = DRAINING
            self.supervisor.spawn(DRAIN_GROUP, lambda i=inst: self._drain(i, i.ref in keep), "drain")
        for inst in stale:
            self.supervisor.spawn(DRAIN_GROUP, lambda i=inst: self._drain(i, False), "drain")
        report.generation, report.manifest_id = self.generation, target.id
        event = {"type": "runtime.changed", "generation": self.generation, "ts": time.time(),
                 "added": [o.ref for o in plan.ops if o.action == "load"],
                 "removed": [o.ref for o in plan.ops if o.action == "unload"],
                 "changed": [o.ref for o in plan.ops if o.action in ("swap", "reconfigure")] + (["config"] if plan.config_changed else [])}
        self._events = (self._events + [event])[-500:]
        if self.on_commit is not None:
            try:
                out = self.on_commit(event)
                if inspect.isawaitable(out):
                    self.supervisor.spawn(DRAIN_GROUP, lambda o=out: o, "on_commit")
            except Exception:  # noqa: BLE001 - a broken listener must not undo a commit
                logger.exception("on_commit callback failed")

    def _states(self) -> dict[str, str]:
        return {r: "failed" for r in self._failures if r not in self._active}

    def _set_states(self, loading: dict[str, str]) -> None:
        states = {**self._states(), **loading}
        self.dispatcher.publish(build_table(self._active.values(), states))

    # -- 6/7. drain, standby
    async def _drain(self, inst: Instance, keep_standby: bool) -> None:
        inst.slot = DRAINING
        deadline = time.monotonic() + self.drain_deadline
        await self.supervisor.cancel_group(inst.group, min(self.drain_deadline, 5.0))
        if not await inst.wait_idle(self.drain_deadline):
            logger.warning("%s still has work in flight after the drain deadline; stopping it", inst.ref)
        try:
            await asyncio.wait_for(inst.unit.stop(inst.ctx, deadline=deadline), max(deadline - time.monotonic(), 1.0))
        except Exception:  # noqa: BLE001
            logger.exception("stopping %s failed", inst.ref)
        inst.started = False
        inst.slot = STOPPED
        if keep_standby:
            inst.slot = STANDBY
            self._standby[inst.ref] = (inst, time.monotonic() + self.standby_window)
        now = time.monotonic()
        self._standby = {r: e for r, e in self._standby.items() if e[1] > now}

    # ---------------------------------------------------------------- convenience transactions
    def _edited(self, branch: Optional[str], fn: Callable[[Manifest], Manifest]) -> Optional[Manifest]:
        """Apply `fn` to the live manifest (returns the target), or to a stored branch (saves it, returns None)."""
        if branch in (None, LIVE):
            return fn(self._manifest)
        base = self.branches.get(branch)
        if base is None:
            raise KeyError(f"no branch {branch!r}")
        self.branches.save(branch, fn(base))
        return None

    async def _edit(self, label: str, branch: Optional[str], fn: Callable[[Manifest], Manifest]) -> Report:
        target = self._edited(branch, fn)
        if target is None:
            return Report(label, generation=self.generation, manifest_id=self._manifest.id)
        return await self.apply(target, label=label)

    async def load(self, unit: str, *, instance: str = "default", version: str = "1", settings: Optional[dict] = None,
                   branch: Optional[str] = None) -> Report:
        spec = InstanceSpec(unit, version, dict(settings or {}))
        return await self._edit(f"load {make_ref(unit, instance)}", branch, lambda m: m.with_instance(unit, instance, spec))

    async def unload(self, unit: str, *, instance: str = "default", branch: Optional[str] = None) -> Report:
        ref = make_ref(unit, instance)
        return await self._edit(f"unload {ref}", branch, lambda m: m.without(ref))

    async def swap(self, unit: str, version: str, *, instance: str = "default", settings: Optional[dict] = None,
                   branch: Optional[str] = None) -> Report:
        ref = make_ref(unit, instance)

        def edit(m: Manifest) -> Manifest:
            old = m.instances.get(ref)
            return m.with_instance(unit, instance, InstanceSpec(
                unit, version, dict(settings) if settings is not None else (old.settings if old else {})))

        return await self._edit(f"swap {ref}", branch, edit)

    async def set_config(self, config: dict, *, branch: Optional[str] = None) -> Report:
        return await self._edit("config", branch, lambda m: m.with_config(config))

    async def boot(self, manifest: Optional[Manifest] = None) -> Report:
        """Start from `manifest` (default: the stored live branch). A unit that fails is reported and left out;
        it never stops the rest."""
        target = manifest or self.branches.get(LIVE) or Manifest()
        while True:
            report = await self.apply(target, label="boot")
            bad = [r for r in report.failed if r in target.instances]
            if report.ok or not bad:
                return report
            for ref in bad:
                target = target.without(ref)

    # ---------------------------------------------------------------- branches
    def list_branches(self) -> list[dict]:
        names = sorted(set(self.branches.names()) | {LIVE})
        return [{"name": n, "manifest_id": (self._manifest if n == LIVE else self.branches.get(n)).id,
                 "preview": n in self._previews} for n in names]

    def get_branch(self, name: str) -> Optional[Manifest]:
        return self._manifest if name == LIVE else self.branches.get(name)

    def fork(self, name: str, from_: str = LIVE) -> Manifest:
        if not valid_branch_name(name) or name in (LIVE, PREVIOUS):
            raise ValueError(f"cannot create branch {name!r}")
        base = self.get_branch(from_)
        if base is None:
            raise KeyError(f"no branch {from_!r}")
        self.branches.save(name, base)
        return base

    async def switch(self, name: str) -> Report:
        target = self.get_branch(name)
        if target is None:
            raise KeyError(f"no branch {name!r}")
        return await self.apply(target, label=f"switch {name}")

    async def rollback(self) -> Report:
        target = self.branches.get(PREVIOUS)
        if target is None:
            raise KeyError("nothing to roll back to")
        return await self.apply(target, label="rollback")

    # -- shadow preview
    async def preview(self, name: str) -> Report:
        """Start the differing instances of a branch beside live, with copy-on-write state, under /api/_branch/<name>/."""
        async with self._lock:
            started = time.monotonic()
            target = self.get_branch(name)
            if target is None:
                raise KeyError(f"no branch {name!r}")
            report = Report(f"preview {name}", generation=self.generation, manifest_id=target.id, plan=self.plan(target))
            await self._close_preview(name)
            shadows: dict[str, Instance] = {}
            try:
                for ref, spec in target.instances.items():
                    if self._manifest.instances.get(ref) == spec:
                        continue
                    ns = self.store.namespace(f"{ref}#{name}", base=self.store.namespace(ref))
                    shadows[ref] = await self._build(ref, spec, state=ns, reuse_ok=False)
                await self._verify(shadows)
            except Exception as exc:  # noqa: BLE001
                for inst in shadows.values():
                    await self._dispose(inst)
                report.ok, report.error = False, str(exc)
                report.failed = dict(exc.failed) if isinstance(exc, StageError) else {}
                return self._finish(report, started)
            members = [shadows.get(r) or self._active[r] for r in target.instances if r in shadows or r in self._active]
            for inst in shadows.values():
                inst.slot = ACTIVE
            self._previews[name] = shadows
            self.dispatcher.set_branch(name, build_table(members))
            return self._finish(report, started)

    async def _close_preview(self, name: str) -> None:
        self.dispatcher.set_branch(name, None)
        for inst in self._previews.pop(name, {}).values():
            await self._dispose(inst)
            self.store.drop_owner(inst.ctx.state.owner)

    async def end_preview(self, name: str) -> None:
        async with self._lock:
            await self._close_preview(name)

    async def shutdown(self) -> None:
        """Unload everything (no units left running) and stop all tasks."""
        await self.apply(Manifest(self._manifest.config), label="shutdown")
        await self.settle()
        await self.supervisor.cancel_all(2.0)
