"""Who owns the running ABP: a leader lease on the instance's data directory.

ABP's data directory is a single point of truth that a lot of things write to
at once - one SQLite database, one set of bot tokens, one scheduler, one model
server, one mDNS announcement. None of those may run twice against the same
data, but ABP is now started in more than one place at a time: the gate
(abp_gate/) keeps a standby instance pre-warmed on new code, every agent gets
its own sandbox, and the desktop app may attach to whatever is already up.

This module owns the "only one of us" part, and nothing else:

    lease      an OS advisory file lock (msvcrt.locking on Windows,
               fcntl.flock elsewhere) on `<state root>/data/abp.lease`. The
               kernel drops it when the holding process dies, so a crashed
               leader never needs a stale-lock cleanup path. A small JSON
               sidecar records WHO holds it (pid/host/port/code root) purely so
               `abp_cli instance list` and the dashboard can name the holder.
    Controller drives the gated services (bot/main.py hands it the two
               coroutines that start and stop them) as the lease is taken and
               given up, so the same code serves "start as leader", "--standby:
               wait your turn" and the gate's zero-downtime swap.

Three roles, decided entirely by environment, never by config:

    leader    holds the lease and runs everything.
    standby   started with --standby / ABP_STANDBY=1: serves the API at once,
              waits for the lease, and takes it the moment it frees up.
    sandbox   ABP_SANDBOX_INSTANCE=1: an agent's private copy of the state
              (abp_gate seeds it with SQLite's online-backup API). It never
              takes the lease - its data is not the real data - and it never
              starts an outward connector even if the copied config still
              holds the real bot tokens. Two pollers on one Telegram token is
              not a degraded sandbox, it is the real bot answering twice.

`LEASE_GATED` and `SANDBOX_BLOCKED` below are the authoritative lists; the
dashboard's /api/lease endpoint serves them so nobody has to read source to
find out what an instance is and isn't doing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("bot.lease")

# Set to 1 by abp_gate for a sandboxed instance, and accepted from a shell so a
# human can start one by hand.
SANDBOX_ENV = "ABP_SANDBOX_INSTANCE"
# Same, for "serve now, lead later".
STANDBY_ENV = "ABP_STANDBY"
# The gate sets this on a standby instance so /api/lease can say who sent it.
GATE_ENV = "ABP_GATE"

LEASE_NAME = "abp.lease"
LEASE_META_NAME = "abp.lease.json"

_TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------- the lists

#: Everything bot/main.py starts ONLY while this process holds the lease. Each
#: entry is (service, what it is), and both the docs and GET /api/lease read
#: this one list - a new gated service added here shows up everywhere.
LEASE_GATED: tuple[tuple[str, str], ...] = (
    ("platform_bots", "Telegram/Discord/Slack/Matrix/WhatsApp/GoogleChat/Teams/... pollers, one per enabled bot instance"),
    ("scheduler", "the recurring-prompt scheduler (/cron, /loop, /heartbeat)"),
    ("legacy_instance_migration", "the one-time .env -> bot_instances migration"),
    ("support_bot_warmup", "the Support Bot classifier's warm-up/training"),
    ("config_watchers", "config/backends.yaml + config/providers.yaml watchers"),
    ("hot_reload", "the source hot-reload watcher (bot/hotreload.py)"),
    ("infra_automation", "tailscale/docker/vm upkeep (bot/infra_automation.py)"),
    ("web_hosting", "edge, tunnel, ACME certificates and dynamic DNS (bot/hosting)"),
    ("file_server", "ABP File Server's sync/scrub/mover/index schedules (bot/fileserver)"),
    ("local_ai", "the local model server on 11436 and the exported model store (bot/localai)"),
    ("neural_lab", "system-model tuning and telemetry recording (bot/neurallab)"),
    ("memory_fabric", "the memory fabric's sources, daily close and vault read (bot/memoryfabric)"),
    ("auto_manage", "the reactive half of auto-management (a new kanban card -> a check-in)"),
    ("peers_health", "federated peers' health checks"),
    ("cluster_heartbeat", "the cluster node report and job rescheduling"),
    ("git_stacks_poller", "the git-stacks poller thread (auto_deploy stacks)"),
    ("retention", "the retention/pruning sweep"),
    ("self_preservation", "the Sentinel and its watchdog thread (bot/sentinel)"),
    ("mdns_advertise", "the mDNS announcement other devices discover ABP by"),
)

#: Never started in a sandbox instance, whatever the copied config says. A
#: sandbox holds a COPY of the real .env, so its bot_instances rows carry the
#: REAL tokens and its schedules are the REAL schedules.
SANDBOX_BLOCKED: tuple[tuple[str, str], ...] = (
    ("platform_pollers", "no bot instance poller is ever started - one token, one poller"),
    ("scheduler", "no scheduled job fires - a copied schedule is the real schedule"),
    ("outbox_send", "bot/outbox.py refuses every send, so nothing can reach a real chat"),
    ("module_hubs", "bot/modules/harness.py refuses to auto-start a hub's web UI / OpenAI server"),
)


def is_sandbox(environ: Optional[dict] = None) -> bool:
    """True when this process must behave as an agent's sandboxed copy."""
    env = os.environ if environ is None else environ
    return str(env.get(SANDBOX_ENV, "")).strip().lower() in _TRUTHY


def is_standby(environ: Optional[dict] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(STANDBY_ENV, "")).strip().lower() in _TRUTHY


def sandbox_blocked_reason(environ: Optional[dict] = None) -> Optional[str]:
    """Why this instance may not talk to the outside world, or None."""
    if not is_sandbox(environ):
        return None
    blocked = ", ".join(name for name, _ in SANDBOX_BLOCKED)
    return f"{SANDBOX_ENV} is set: {blocked} are not started"


def gated_services() -> list[dict[str, str]]:
    return [{"service": name, "what": what} for name, what in LEASE_GATED]


def sandbox_blocked() -> list[dict[str, str]]:
    return [{"service": name, "what": what} for name, what in SANDBOX_BLOCKED]


# ---------------------------------------------------------------- the paths


def _state_root(state_root: Optional[Path] = None) -> Path:
    if state_root is not None:
        return Path(state_root)
    from bot.envfile import PROJECT_ROOT

    return PROJECT_ROOT


def lease_path(state_root: Optional[Path] = None) -> Path:
    """The lock file itself. Under data/ because that is the directory two
    instances of one install can never legitimately share silently - and the
    sandbox's own data/<name>/ copy gets its own lease, for free."""
    return _state_root(state_root) / "data" / LEASE_NAME


def meta_path(state_root: Optional[Path] = None) -> Path:
    return lease_path(state_root).with_name(LEASE_META_NAME)


# --------------------------------------------------------- the raw file lock


def _lock(fd: int) -> bool:
    """Non-blocking exclusive lock on byte 0. False when someone else has it."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class Lease:
    """The lock on one data root, held for as long as you keep this object
    holding it. Deliberately a plain synchronous file handle: it is checked
    once a second from an asyncio loop and touched from a FastAPI handler, and
    both of those are microseconds of work."""

    def __init__(self, state_root: Optional[Path] = None):
        self.path = lease_path(state_root)
        self.meta_path = meta_path(state_root)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self, holder: Optional[dict] = None) -> bool:
        """Take the lease if it is free. Idempotent: already holding it is True."""
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            # Fail closed. An unwritable data dir must not turn into "every
            # instance thinks it is the leader".
            logger.error("cannot open the lease file %s (%s) - staying a follower", self.path, exc)
            return False
        if not _lock(fd):
            os.close(fd)
            return False
        self._fd = fd
        self._write_holder(holder)
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        self._clear_holder()
        _unlock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    # -- who, for humans ------------------------------------------------------
    def holder(self) -> dict[str, Any]:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_holder(self, extra: Optional[dict]) -> None:
        info = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired": time.time(),
            **(extra or {}),
        }
        info.setdefault("code_root", _code_root())
        try:
            self.meta_path.write_text(json.dumps(info, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not record the lease holder in %s: %s", self.meta_path, exc)

    def _clear_holder(self) -> None:
        """Drop the sidecar only if it is still OURS - a process that crashed
        and a newer leader that already took over must not have its record
        deleted by the old one's cleanup."""
        if self.holder().get("pid") != os.getpid():
            return
        try:
            self.meta_path.unlink(missing_ok=True)
        except OSError:
            pass

    def locked_by_other(self) -> bool:
        """Is somebody ELSE holding it right now? Probed by briefly taking the
        lock on a throwaway handle - the same thing try_acquire() does, which
        is exactly why it is a truthful answer and not a heuristic."""
        if self.held:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return True
        try:
            free = _lock(fd)
            if free:
                _unlock(fd)
            return not free
        finally:
            os.close(fd)


def _code_root() -> str:
    try:
        from bot.envfile import CODE_ROOT

        return str(CODE_ROOT)
    except Exception:  # noqa: BLE001 - only ever a label in the sidecar
        return ""


def status(state_root: Optional[Path] = None) -> dict[str, Any]:
    """Who owns one data root, without taking the lease (a read-only probe)."""
    lease = Lease(state_root)
    return {
        "path": str(lease.path),
        "held_by_other": lease.locked_by_other(),
        "holder": lease.holder(),
        "sandbox": is_sandbox(),
        "standby": is_standby(),
    }


# ------------------------------------------------------------- the controller


class Controller:
    """Owns "the gated services are running" as a consequence of "this process
    holds the lease", so the two can never drift apart.

    bot/main.py passes the two coroutines that do the real work; they close
    over the dashboard app, the stop event and the port, which keeps every
    business import out of here and keeps bot/main.py's own startup order
    readable in one place. Both are optional so a caller that only wants to
    ask questions (a test, a status page) can construct one with none."""

    def __init__(
        self,
        start: Optional[Callable[[], Awaitable[Any]]] = None,
        stop: Optional[Callable[[], Awaitable[Any]]] = None,
        *,
        standby: Optional[bool] = None,
        poll_s: float = 1.0,
    ):
        self._start = start
        self._stop = stop
        self._standby = is_standby() if standby is None else standby
        self._poll_s = max(0.05, float(poll_s))
        self._lease = Lease()
        self._running = False
        # A sandbox has copied data, so leading is meaningless at best and a
        # second writer of somebody else's database at worst.
        self._want_lead = not self._standby and not is_sandbox()
        self._lock = asyncio.Lock()
        self._stop_event: Optional[asyncio.Event] = None

    # -- introspection -------------------------------------------------------
    @property
    def lease(self) -> Lease:
        return self._lease

    @property
    def running(self) -> bool:
        """Are the lease-gated services up right now?"""
        return self._running

    def status(self) -> dict[str, Any]:
        return {
            "path": str(self._lease.path),
            "held": self._lease.held,
            "singletons_running": self._running,
            "standby": self._standby,
            "sandbox": is_sandbox(),
            "wants_leadership": self._want_lead,
            "held_by_other": (not self._lease.held) and self._lease.locked_by_other(),
            "holder": self._lease.holder(),
            "outward_blocked": sandbox_blocked_reason(),
            "gated_services": gated_services(),
            "sandbox_blocked": sandbox_blocked(),
            "pid": os.getpid(),
            "code_root": _code_root(),
        }

    # -- the loop ------------------------------------------------------------
    async def supervise(self, stop_event: asyncio.Event) -> None:
        """Holds the lease (and the gated services with it) until asked to
        give it up or the process shuts down. Started as a task by bot.main
        right after the dashboard is already answering, so the gate's health
        check never waits on the leader decision."""
        self._stop_event = stop_event
        while not stop_event.is_set():
            try:
                await self._step()
            except Exception:  # noqa: BLE001 - the lease loop outlives anything it starts
                logger.exception("the lease supervisor step failed; retrying")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_s)
            except asyncio.TimeoutError:
                continue
        await self.release()

    async def _step(self) -> None:
        async with self._lock:
            if self._running:
                if not self._want_lead:
                    await self._give_up()
            elif self._want_lead and self._start is not None:
                await self._take()

    async def _take(self) -> None:
        holder = {
            "role": "sandbox" if is_sandbox() else ("standby" if self._standby else "leader"),
            "port": os.environ.get("DASHBOARD_PORT", ""),
            "version": _version(),
            "gate": os.environ.get(GATE_ENV, ""),
        }
        if not self._lease.try_acquire(holder=holder):
            return
        logger.info("leader lease acquired (%s) - starting the lease-gated services", self._lease.path)
        try:
            await self._start()  # type: ignore[misc]
        except Exception:
            # Never hold the lease for services that aren't running: the next
            # instance would happily wait for a leader that does not exist.
            logger.exception("the lease-gated services failed to start - releasing the lease")
            self._lease.release()
            return
        self._running = True

    async def _give_up(self) -> None:
        logger.info("releasing the leader lease - stopping the lease-gated services")
        try:
            if self._stop is not None:
                await self._stop()
        finally:
            self._running = False
            self._lease.release()

    # -- the two endpoints ---------------------------------------------------
    async def release(self) -> dict[str, Any]:
        """Give the lease up NOW: stop the gated services, keep serving the API.
        This is what the gate calls on the outgoing instance during a swap."""
        self._want_lead = False
        async with self._lock:
            if self._running:
                await self._give_up()
            else:
                self._lease.release()
        return self.status()

    async def acquire(self, timeout: float = 30.0, *, force: bool = False) -> dict[str, Any]:
        """Take the lease as soon as it is free, up to `timeout`.
        
        By default, a --standby instance refuses (standing by is what it was asked to do).
        Pass `force=True` to override (used by the gate during a swap).
        """
        if self._standby and not force:
            return {**self.status(), "error": "this instance was started --standby; it will not lead"}
        self._want_lead = True
        deadline = time.monotonic() + timeout
        while not self._running:
            await self._step()
            if self._running:
                break
            if self._stop_event is not None and self._stop_event.is_set():
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self._poll_s)
        return self.status()


def _version() -> str:
    try:
        from bot import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


# The one controller this process has, so bot/dashboard's lease routes and any
# other in-process caller can reach it without run() threading it everywhere.
_controller: Optional[Controller] = None


def set_controller(controller: Optional[Controller]) -> None:
    global _controller
    _controller = controller


def controller() -> Optional[Controller]:
    return _controller