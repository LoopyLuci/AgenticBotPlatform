"""Hot reload through the engine (docs/runtime.md, "Hot reload").

When a changed file belongs to a unit, reloading it is a *swap of that unit*, not an in-place module reload:

* feature area (bot/runtime/legacy.py AREAS): the new version id is a hash of the area's source files. The swap
  imports the area's code afresh into private module objects beside the running ones (the old version keeps serving),
  health-checks it, and installs the new modules, tools and tasks in the engine's single commit; the old version
  drains and stays as standby, so a rollback works. If the new source does not compile or import, or fails its health
  check, the old version keeps serving, the failure is reported (`GET /api/runtime` last transaction,
  `GET /api/hotreload/status` units) and the *next* edit reloads normally: a failed unit swap never degrades hot reload.
* extension / Python plugin: `pluggables.sync` swaps the unit whose files changed (version = hash of its files).
* an area that is not running: nothing is imported and no tool is registered; its cached modules are dropped so the
  next load imports the new code.

Anything else keeps the old module-reload behaviour in bot/hotreload.py (kernel and denylisted modules need a restart).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bot.runtime.manifest import InstanceSpec
from bot.runtime.unit import DEFAULT_INSTANCE, split_ref

logger = logging.getLogger("bot.runtime.reload")


def owned_by_units(paths: list) -> tuple:
    """(area -> its changed files, extension/plugin unit refs, files claimed by either). Cheap when nothing matches."""
    from bot.runtime import legacy, pluggables

    resolved = {Path(p).resolve(): Path(p) for p in paths}
    by_area: dict = {}
    claimed: set = set()
    for area in legacy.AREAS.values():
        mine = {f.resolve() for f in legacy.source_files(area)} & set(resolved)
        if mine:
            by_area[area.id] = sorted(resolved[m] for m in mine)
            claimed.update(resolved[m] for m in mine)
    refs = pluggables.refs_for_paths(list(resolved.values()))
    for files in refs.values():
        claimed.update(files)
    return by_area, refs, claimed


async def reload_units(paths: list) -> dict[str, Any]:
    """Swap every unit that owns one of `paths`. Returns {"handled": [paths], "units": [per-unit results]}; with no
    booted runtime, or no unit involved, nothing is handled."""
    out: dict[str, Any] = {"handled": [], "units": []}
    from bot.runtime import legacy, live

    if not live.booted or live._engine is None:
        return out
    by_area, refs, claimed = owned_by_units(paths)
    if not claimed:
        return out
    engine = live.get_engine()
    out["handled"] = sorted(claimed)
    for area_id in by_area:
        area = legacy.AREAS[area_id]
        out["units"].append(await _swap_area(engine, area, by_area[area_id]))
    if refs:
        out["units"].extend(await _sync_pluggables(engine, refs))
    return out


def live_refs(engine: Any, unit: str) -> list:
    """Every live instance (`unit@name`) of `unit`, the default one first."""
    refs = [r for r in engine._active if split_ref(r)[0] == unit]
    return sorted(refs, key=lambda r: (split_ref(r)[1] != DEFAULT_INSTANCE, r))


async def _swap_area(engine: Any, area: Any, files: list) -> dict[str, Any]:
    """Swap *every* live instance of the area to the new source version in one engine transaction (all of them swap,
    or none does and the old version keeps serving). The result carries a per-instance report."""
    from bot.runtime import legacy

    result: dict[str, Any] = {"unit": area.id, "files": [f.name for f in files]}
    refs = live_refs(engine, area.id)
    if not refs:
        legacy.purge(area)
        legacy.scrub_inactive_tools()
        return {**result, "action": "dropped", "ok": True,
                "detail": f"{area.id} is not running: nothing registered; its next load imports the new code"}
    version = legacy.source_version(area)
    live_instances = engine.manifest.instances
    stale = [r for r in refs if r not in live_instances or live_instances[r].version != version]
    if not stale:
        return {**result, "action": "unchanged", "ok": True, "version": version, "detail": "same source version",
                "instances": {r: "unchanged" for r in refs}}
    target = engine.manifest
    for ref in stale:
        old = live_instances.get(ref)
        target = target.with_instance(area.id, split_ref(ref)[1],
                                      InstanceSpec(area.id, version, dict(old.settings) if old else {}))
    try:
        report = await engine.apply(target, label=f"reload {area.id} ({len(stale)} instance(s))")
    except Exception as exc:  # noqa: BLE001 - a reload never takes the watcher down
        logger.exception("hot reload of unit %s raised", area.id)
        return {**result, "action": "swap", "ok": False, "version": version, "detail": f"{type(exc).__name__}: {exc}",
                "instances": {r: f"failed: {type(exc).__name__}: {exc}" for r in stale}}
    finally:
        legacy.scrub_inactive_tools()
    result.update(action="swap", ok=report.ok, version=version, generation=report.generation)
    if report.ok:
        result["instances"] = {r: ("swapped" if r in stale else "unchanged") for r in refs}
        result["detail"] = (f"{area.id} swapped to {version} (generation {report.generation}; "
                            f"{len(stale)} instance(s)); the old version drains")
    else:
        result["instances"] = {r: ("failed: " + report.failed[r] if r in report.failed else "kept previous version")
                               for r in stale}
        result["detail"] = f"{area.id} keeps its previous version: {report.error}"
        result["failed"] = dict(report.failed)
    return result


async def _sync_pluggables(engine: Any, refs: dict) -> list:
    from bot.runtime import pluggables

    kinds = tuple(sorted({k for ref in refs for k in pluggables.KINDS if ref.startswith(k)}))
    before = {r: (engine.manifest.instances.get(r).version if r in engine.manifest.instances else None) for r in refs}
    report = await pluggables.sync(engine, kinds=kinds)
    results = []
    for ref in sorted(refs):
        now = engine.manifest.instances.get(ref)
        err = pluggables.errors.get(ref, "")
        if err:
            results.append({"unit": ref.partition("@")[0], "action": "swap", "ok": False,
                            "detail": f"{ref} keeps its previous version: {err}"})
        else:
            changed = (now.version if now else None) != before[ref]
            results.append({"unit": ref.partition("@")[0], "action": "swap" if changed else "unchanged", "ok": True,
                            "version": now.version if now else None,
                            "generation": report.generation if report else engine.generation,
                            "detail": f"{ref} {'reloaded' if changed else 'unchanged'}"})
    return results
