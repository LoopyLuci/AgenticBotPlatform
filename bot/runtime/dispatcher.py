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
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from bot.runtime.unit import DEFAULT_INSTANCE, Instance, make_ref


@dataclass(frozen=True)
class RouteTable:
    mounts: Mapping[str, Instance]          # ref -> instance
    states: Mapping[str, str]               # ref -> loading | failed | unloaded, for refs without a mount

    @staticmethod
    def empty() -> "RouteTable":
        return RouteTable(MappingProxyType({}), MappingProxyType({}))


def build_table(instances: Iterable[Instance], states: Optional[Mapping[str, str]] = None) -> RouteTable:
    mounts = {i.ref: i for i in instances if i.app is not None}
    return RouteTable(MappingProxyType(mounts), MappingProxyType(dict(states or {})))


class Dispatcher:
    def __init__(self, prefix: str = "/api") -> None:
        self.prefix = prefix.rstrip("/")
        self._table = RouteTable.empty()
        self._staged = RouteTable.empty()
        self._branches: Mapping[str, RouteTable] = MappingProxyType({})

    # -- publishing (each is one reference swap) --
    @property
    def table(self) -> RouteTable:
        return self._table

    def publish(self, table: RouteTable) -> None:
        self._table = table

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
