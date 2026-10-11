"""The registries: tools, panels, cli commands and the config overlay, each published as one immutable snapshot.

`prepare()` is pure and validates (a tool name that clashes raises there, before anything is committed);
`publish()` applies the tool difference through `toolspec.register` / `unregister` and swaps the snapshot reference.
Non-default instances get suffixed tool names (`tool@instance`).
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from bot.agent_runtime import toolspec
from bot.runtime.unit import DEFAULT_INSTANCE, Instance


@dataclass(frozen=True)
class Snapshot:
    tools: Mapping[str, tuple]       # exposed name -> (schema, spec, handler, enabled)
    panels: tuple
    cli: tuple
    overlay: Mapping[str, Any]
    registrations: tuple = ()        # apply()/revert() objects of the live instances

    @staticmethod
    def empty() -> "Snapshot":
        return Snapshot(MappingProxyType({}), (), (), MappingProxyType({}))


def deep_merge(base: Mapping, over: Mapping) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _guarded(fn: Any) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - a registration hook must not undo a commit
        logging.getLogger("bot.runtime.registries").exception("registration hook %r failed", fn)


def _suffixed(entry: tuple, instance_id: str) -> tuple:
    schema, spec, handler, *rest = entry
    enabled = rest[0] if rest else None
    if instance_id != DEFAULT_INSTANCE:
        name = f"{spec.name}@{instance_id}"
        schema = {**schema, "name": name}
        spec = dataclasses.replace(spec, name=name)
    return schema, spec, handler, enabled


def _panel(raw: dict, inst: Instance) -> dict:
    """A panel descriptor in the UI manifest's shape (docs/runtime.md)."""
    kind = raw.get("kind") or ("script" if raw.get("script") else "builtin")
    out = {"id": raw.get("id"), "unit": inst.unit_id, "instance": inst.instance_id, "version": inst.version,
           "title": raw.get("title") or raw.get("id"), "group": raw.get("group") or raw.get("nav_group") or "modules",
           "icon": raw.get("icon"), "order": raw.get("order", 100), "kind": kind}
    if kind == "builtin":
        out["section"] = raw.get("section") or raw.get("id")
    else:
        out["script"] = raw.get("script")
        if raw.get("styles"):
            out["styles"] = list(raw["styles"])
    return out


class Registries:
    def __init__(self) -> None:
        self.snapshot = Snapshot.empty()

    @staticmethod
    def prepare(instances: Iterable[Instance]) -> Snapshot:
        tools: dict[str, tuple] = {}
        panels: list[dict] = []
        cli: list = []
        overlay: dict = {}
        registrations: list = []
        for inst in sorted(instances, key=lambda i: i.ref):
            c = inst.contributions
            for raw in c.tools:
                schema, spec, handler, enabled = _suffixed(tuple(raw), inst.instance_id)
                if spec.name in tools or spec.name in toolspec.builtin_names():
                    raise ValueError(f"tool name {spec.name!r} from {inst.ref} is already taken")
                if schema.get("name") != spec.name:
                    raise ValueError(f"tool {spec.name!r} from {inst.ref}: schema name and spec name differ")
                tools[spec.name] = (schema, spec, handler, enabled)
            for panel in c.panels:
                panels.append(_panel(panel, inst))
            cli.extend(c.cli)
            overlay = deep_merge(overlay, c.config_overlay)
            registrations.extend(c.registrations)
        panels.sort(key=lambda p: (p.get("order", 100), p.get("id", "")))
        return Snapshot(MappingProxyType(tools), tuple(panels), tuple(cli), MappingProxyType(overlay), tuple(registrations))

    def publish(self, snap: Snapshot) -> None:
        new_ids = {id(r) for r in snap.registrations}
        old_ids = {id(r) for r in self.snapshot.registrations}
        for reg in self.snapshot.registrations:           # what leaves is reverted before what arrives is applied
            if id(reg) not in new_ids:
                _guarded(reg.revert)
        for name in set(self.snapshot.tools) - set(snap.tools):
            toolspec.unregister(name)
        for schema, spec, handler, enabled in snap.tools.values():
            toolspec.register(schema, spec, handler, enabled)
        for reg in snap.registrations:
            if id(reg) not in old_ids:
                _guarded(reg.apply)
        self.snapshot = snap
