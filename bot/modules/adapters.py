"""Adapters: the modules ABP drove before the framework (VM-Harness, Hermes-Manager, TransferDaemon) keep their own
code, and the framework reaches it through the same verbs it uses for every other module. Their own pages and tools
stay as they are; this makes them show up and work on the Modules page and through the generic module tools too.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from bot.modules.client import ModuleError


@dataclass(frozen=True)
class Adapter:
    package: str          # bot.<package>.harness / bot.<package>.client
    error: str            # the package's own exception class, in client.py
    start: str            # harness functions
    stop: str
    status: str

    def _mod(self, name: str):
        return importlib.import_module(f"bot.{self.package}.{name}")

    def run(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        err = getattr(self._mod("client"), self.error)
        try:
            return fn(*args, **kwargs)
        except err as e:  # the module's own error: same message and code, one type for the framework
            raise ModuleError(str(e), code=str(getattr(e, "code", "error")), status=int(getattr(e, "status", 0) or 0)) from None

    def install_dir(self):
        return self._mod("harness").install_dir()

    def install_info(self, fetch: bool = False) -> dict:
        return self.run(self._mod("harness").install_info, fetch=fetch)

    def setup(self) -> dict:
        return self.run(self._mod("harness").setup)

    def update(self) -> dict:
        return self.run(self._mod("harness").update)

    def jobs(self) -> list:
        return self.run(self._mod("harness").jobs)

    def start_hub(self) -> dict:
        return self.run(getattr(self._mod("harness"), self.start))

    def stop_hub(self) -> dict:
        return self.run(getattr(self._mod("harness"), self.stop))

    def open_window(self) -> dict:
        return self.run(self._mod("harness").open_window)

    def status(self) -> dict:
        return self.run(getattr(self._mod("harness"), self.status))

    def register_mcp(self) -> dict:
        return self.run(self._mod("harness").register_mcp)

    def running(self) -> Optional[dict]:
        try:
            hub = self._mod("client").find()
        except Exception:  # noqa: BLE001
            return None
        if hub is None:
            return None
        return {"url": getattr(hub, "url", ""), "pid": int(getattr(hub, "pid", 0) or 0),
                "version": str(getattr(hub, "version", "") or "")}

    def operations(self, refresh: bool = False) -> list[dict]:
        from bot.modules.client import normalize_op
        return [normalize_op(o) for o in self.run(self._mod("client").operations, refresh)]

    def call(self, op_id: str, args: Optional[dict] = None, timeout: float = 900.0) -> Any:
        return self.run(self._mod("client").call, op_id, dict(args or {}), timeout=timeout)


ADAPTERS: dict[str, Adapter] = {
    "vm_harness": Adapter("vm_harness", "HarnessError", "start_hub", "stop_hub", "status"),
    "hermes_manager": Adapter("hermes_manager", "ManagerError", "start_bridge", "stop_bridge", "status"),
    "transferdaemon": Adapter("transferdaemon", "DaemonError", "start_daemon", "stop_daemon", "summary"),
}


def get(name: str) -> Optional[Adapter]:
    return ADAPTERS.get(name) if name else None
