"""`python -m abp_gate` — the gate daemon.

Binds the public port(s) and the control port, starts the production instance
unless told not to, and then does nothing but watch: if the active instance
dies, it is restarted on the same data and the same code.

Started windowless by `abp_cli gate start` (or by the desktop app), so it
outlives the terminal that launched it. Run it in the foreground to watch it:
that is the same process, just attached to a console.

    python -m abp_gate                     # bind and serve, start production
    python -m abp_gate --no-start          # bind and serve, start nothing
    python -m abp_gate --public-port 18787 --control-port 18788
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from typing import Any, Optional

from abp_gate import __version__, control, manager, paths, procs, registry
from abp_gate.proxy import ProxyApp, Router

logger = logging.getLogger("abp_gate")

#: How often the daemon looks at the active instance. Cheap (one /healthz and
#: one psutil query), and short enough that a crashed instance is back inside
#: the time it takes somebody to notice.
WATCH_INTERVAL_S = 5.0


def _setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    # A gate log file next to the registry: by the time somebody reads a gate
    # problem, the terminal it was started from is long gone.
    try:
        handler = logging.FileHandler(paths.gate_log_path(), encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        pass
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Gate:
    """The running gate: the proxy apps, the control app, and the one task that
    watches the active instance."""

    def __init__(self, *, public_ports: Optional[list[int]] = None, control_port: Optional[int] = None):
        self.router = Router()
        self.mgr = manager.Manager(self.router)
        self.public_ports = public_ports or paths.public_ports()
        self.control_port = control_port if control_port is not None else paths.control_port()
        self.control_app = control.build_app(self.mgr)
        self._stop = asyncio.Event()
        self._servers: list[Any] = []
        self._control_task: Optional[asyncio.Task] = None

    async def run(self, *, start_production: bool = True) -> None:
        import uvicorn

        control.save_gate_meta({"public_url": f"http://127.0.0.1:{self.public_ports[0]}"})
        logger.info("abp_gate %s up: public %s, control http://127.0.0.1:%s",
                    __version__, self.public_ports, self.control_port)

        _install_signal_handlers(self.request_shutdown)
        self.control_app.state.on_stop = self.request_shutdown
        apps = {port: ProxyApp(self.router, paths.channel_of_port(port)) for port in self.public_ports}
        for port, app in apps.items():
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                    loop="asyncio", ws="auto", access_log=False)
            self._servers.append(uvicorn.Server(config))
        control_config = uvicorn.Config(self.control_app, host="127.0.0.1", port=self.control_port,
                                        log_level="warning", loop="asyncio", access_log=False)
        control_server = uvicorn.Server(control_config)

        for task in [asyncio.create_task(s.serve()) for s in self._servers] + [
            asyncio.create_task(control_server.serve())
        ]:
            task.add_done_callback(lambda t: logger.error("a listener stopped: %s", t.exception()))
        # The control listener has to be answering before we start anything,
        # otherwise `gate start` races its own request and reports a spurious
        # failure on a machine that is actually fine.
        await _await_started(control_server, self.control_port)
        logger.info("abp_gate control API listening on http://127.0.0.1:%s", self.control_port)

        if start_production:
            try:
                result = await self.mgr.start_production()
                logger.info("production instance ready: %s", json.dumps({k: v for k, v in result.items()
                                                                         if k != "log"}))
            except Exception:  # noqa: BLE001 - a broken checkout must not stop the gate existing
                logger.exception("could not start the production instance - the gate is up and will retry")

        self._control_task = asyncio.create_task(self._watch())
        await self._stop.wait()
        # The control API's /api/gate/stop stops the instances itself, then sets
        # the flag this loop is waiting on. Any OTHER exit - Ctrl-C, a taskkill
        # from a supervisor - deliberately leaves the instances running: they
        # outlive the gate on purpose, so a gate that dies is a gate that can
        # come straight back and re-adopt them from the registry.
        await self.shutdown(stop_instances=False)

    async def shutdown(self, *, stop_instances: bool = False) -> None:
        logger.info("abp_gate shutting down (stop_instances=%s)", stop_instances)
        for server in self._servers:
            server.should_exit = True
        if stop_instances:
            try:
                await self.mgr.stop_all()
            except Exception:  # noqa: BLE001 - the gate is going away regardless
                logger.debug("stopping instances failed during shutdown", exc_info=True)
        paths.gate_meta_path().unlink(missing_ok=True)

    def request_shutdown(self) -> None:
        """POST /api/gate/stop set this; the run loop turns it into a real exit
        once the current swap (if any) has finished."""
        self._stop.set()

    async def _watch(self) -> None:
        """The always-on part: a crashed active instance is restarted, on the
        same code and the same data, with the same public port. A sandbox an
        agent started is left alone - it is theirs, and its exit is theirs too."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=WATCH_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._reconcile()
            except Exception:  # noqa: BLE001 - the watcher outlives anything it finds
                logger.exception("the instance watch iteration failed")

    async def _reconcile(self) -> None:
        active_name = registry.active_name()
        logger.debug("watcher reconcile: active_name=%s", active_name)
        if not active_name:
            return
        inst = registry.get(active_name)
        if inst is None:
            logger.debug("watcher: no instance for active_name=%s", active_name)
            return
        logger.debug("watcher: checking instance %r (pid=%s, port=%s)", inst.name, inst.pid, inst.port)
        if procs.alive(inst.pid) and manager.health(inst.port).get("healthy"):
            logger.debug("watcher: instance %r is healthy", inst.name)
            return
        if inst.sandbox:
            # A sandbox that died is just a sandbox that died: record it and let
            # the agent start another. Restarting it behind their back would
            # resurrect an instance whose code root they may have deleted.
            inst.health = registry.HEALTH_STOPPED
            registry.put(inst.name, inst)
            return
        logger.warning("active instance %r is not healthy (pid=%s) - restarting it", inst.name, inst.pid)
        try:
            await self.mgr.restart_active()
        except manager.SwapError as exc:
            logger.error("could not restart the active instance: %s", exc)


def _install_signal_handlers(on_stop) -> None:
    """Ctrl-C stops the gate. Windows has no loop-level signal handler for
    SIGTERM, which is fine: a taskkill of the gate is a hard stop, and the
    instances it left behind are re-adopted when the gate comes back."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_stop)
        except (NotImplementedError, RuntimeError):
            pass


async def _await_started(server: Any, port: int, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"the control API did not bind 127.0.0.1:{port} within {timeout_s:.0f}s")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m abp_gate", description="ABP's stable front door.")
    ap.add_argument("--public-port", type=int, action="append", default=None,
                    help="a public port to own and reverse-proxy (repeatable; default 8787)")
    ap.add_argument("--control-port", type=int, default=None,
                    help=f"the control API port (default {paths.CONTROL_PORT})")
    ap.add_argument("--code-root", default=None, help="the ABP checkout to run (default: this package's parent)")
    ap.add_argument("--state-root", default=None, help="the real ABP state root (default: ABP_HOME or the code root)")
    ap.add_argument("--instances-dir", default=None,
                    help=f"where instances live (default <state root>/{paths.INSTANCES_ENV})")
    ap.add_argument("--no-start", action="store_true", help="do not start the production instance")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.code_root:
        os.environ["ABP_GATE_CODE_ROOT"] = args.code_root
    if args.state_root:
        os.environ["ABP_HOME"] = args.state_root
    if args.instances_dir:
        os.environ[paths.INSTANCES_ENV] = args.instances_dir
    if args.public_port:
        os.environ["ABP_GATE_PUBLIC_PORTS"] = ",".join(str(p) for p in args.public_port)
    if args.control_port is not None:
        os.environ["ABP_GATE_CONTROL_PORT"] = str(args.control_port)
    _setup_logging(args.verbose)

    gate = Gate()
    try:
        asyncio.run(gate.run(start_production=not args.no_start))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        logger.error("abp_gate could not start: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())