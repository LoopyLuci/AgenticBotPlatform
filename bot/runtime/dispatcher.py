"""The dynamic dispatcher: one ASGI app that routes `/api/<unit>[@instance]/...` to the live instance's router.

The route table is an immutable object replaced by a single reference swap at commit, so a request sees the old
table or the new one, never a mix. Resolution and the in-flight increment happen with no `await` in between, so a
commit cannot slip between "found the instance" and "counted as inside it". HTTP requests are counted per instance
(the drain waits for zero); a WebSocket is counted as a connection and stays on the instance that accepted it.

Also served: `/api/_staged/<unit>...` (instances being verified) and `/api/_branch/<name>/<unit>...` (shadow
previews). An unknown, unloaded or failed unit answers 404 JSON, never a stack trace.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from bot.runtime.unit import DEFAULT_INSTANCE, Instance, make_ref


@dataclass(frozen=True)
class RouteTable:
    mounts: Mapping[str, Instance]          # ref -> instance
    states: Mapping[str, str]               # ref -> loading | failed | unloaded, for refs without a mount
    paths: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))   # absolute /api/... prefix -> ref (units whose routes keep their own paths)

    @staticmethod
    def empty() -> "RouteTable":
        return RouteTable(MappingProxyType({}), MappingProxyType({}))


def build_table(instances: Iterable[Instance], states: Optional[Mapping[str, str]] = None) -> RouteTable:
    mounts = {i.ref: i for i in instances if i.app is not None}
    paths = {p: i.ref for i in mounts.values() for p in i.contributions.route_prefixes}
    return RouteTable(MappingProxyType(mounts), MappingProxyType(dict(states or {})), MappingProxyType(paths))


class Dispatcher:
    def __init__(self, prefix: str = "/api") -> None:
        self.prefix = prefix.rstrip("/")
        self._table = RouteTable.empty()
        self._staged = RouteTable.empty()
        self._branches: Mapping[str, RouteTable] = MappingProxyType({})
        self._known: dict[str, str] = {}          # every absolute prefix ever mounted -> ref (so an unload answers unit_unavailable)

    # -- publishing (each is one reference swap) --
    @property
    def table(self) -> RouteTable:
        return self._table

    def publish(self, table: RouteTable) -> None:
        self._known.update(table.paths)
        self._table = table

    def is_known_path(self, path: str) -> bool:
        """Whether `path` falls under an absolute prefix a unit has mounted (now or earlier)."""
        return self._match(self._known, path) is not None

    @staticmethod
    def _match(paths: Mapping[str, str], path: str) -> Optional[str]:
        best = None
        for prefix, ref in paths.items():
            if (path == prefix or path.startswith(prefix + "/")) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, ref)
        return best[1] if best else None

    def set_staged(self, table: Optional[RouteTable]) -> None:
        self._staged = table or RouteTable.empty()

    def set_branch(self, name: str, table: Optional[RouteTable]) -> None:
        branches = dict(self._branches)
        if table is None:
            branches.pop(name, None)
        else:
            branches[name] = table
        self._branches = MappingProxyType(branches)

    # -- ASGI --
    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return
        found = self._resolve(scope)
        if isinstance(found[0], Instance):
            inst, rest = found
            ws = kind == "websocket"
            inst.enter(ws)                       # counted before any await: a commit cannot interleave
            try:
                inner = dict(scope)
                inner["path"] = rest
                if "raw_path" in scope:
                    inner["raw_path"] = rest.encode()
                await inst.app(inner, receive, send)
            finally:
                inst.leave(ws)
            return
        await self._unavailable(scope, receive, send, found[1], found[2])

    def _resolve(self, scope: dict) -> tuple:
        """(instance, remaining_path) when found, else (None, unit, state)."""
        path = scope.get("path", "/")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):]
        full = path
        if path.startswith(self.prefix + "/"):
            path = path[len(self.prefix):]
        segs = path.lstrip("/").split("/")
        table = self._table
        if segs[0] == "_staged":
            table, segs = self._staged, segs[1:]
        elif segs[0] == "_branch" and len(segs) >= 2:
            table, segs = self._branches.get(segs[1], RouteTable.empty()), segs[2:]
        key = segs[0] if segs else ""
        rest = "/" + "/".join(segs[1:])
        unit, _, inst_id = key.partition("@")
        ref = make_ref(unit, inst_id or DEFAULT_INSTANCE)
        inst = table.mounts.get(ref)
        if inst is None:
            owner = self._match(table.paths, full)
            if owner is not None and owner in table.mounts:
                return table.mounts[owner], full         # absolute: the unit's router holds the full path
            if owner is None and table is self._table:
                owner = self._match(self._known, full)
                if owner is not None:
                    return None, owner.partition("@")[0], table.states.get(owner, "unloaded")
            return None, key, table.states.get(ref, "unloaded")
        return inst, rest

    async def _unavailable(self, scope: dict, receive: Any, send: Any, unit: str, state: str) -> None:
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": {"code": "unit_unavailable", "unit": unit, "state": state}}).encode()
        await send({"type": "http.response.start", "status": 404,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
