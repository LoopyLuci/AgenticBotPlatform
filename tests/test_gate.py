"""abp_gate end to end, with real processes and real sockets: the front door, the
hot swap (and its rollback), the sandbox, the leader lease - and the two things
that keep the gate from eating the machine: every instance is stoppable as a
whole, and the watcher cannot start processes forever.

Nothing here is mocked. `python -m abp_gate` and `python -m bot.main` are really
spawned, on throwaway ABP_HOMEs under tmp_path, on real localhost ports, and every
assertion is made against what a real client (httpx, websockets, a thread) actually
got back.

    python -m pytest -q -p no:cacheprovider --no-cov tests/test_gate.py

The tests are synchronous like the rest of this suite; asyncio.run() appears only
where something has to happen while something else is in flight - reading a stream
that stays open, and probing the public port throughout a swap.

The expensive part is a bot.main boot (~7s: it migrates the database and builds the
OpenAPI schema), so the gate and its production instance are started ONCE per
module and every test that can share them does. Each swap costs one more boot,
which is why the swap tests are the slowest ones here.

Every process these tests start goes into a `Cell` first (a Windows job object,
bot/agent_runtime/win_job.py, with KILL_ON_JOB_CLOSE), and the cell is closed by
the fixture's teardown. That is not tidiness: a venv's python.exe is a launcher
that leaves its interpreter running when it is killed, so a test that only killed
the pid it had would leave an ABP behind, holding a port, forever - which is the
exact failure that filled this machine with six thousand orphaned interpreters in
one run. A job object also cannot outlive this process, so even a crashed or
SIGKILLed pytest takes everything it started with it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import pytest

from abp_gate import limits, manager

CODE_ROOT = Path(__file__).resolve().parent.parent
PYTHON = Path(sys.executable)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: Stripped from the environment these processes inherit: a stray
#: ABP_SANDBOX_INSTANCE or DASHBOARD_PORT in the shell that ran pytest must not
#: silently change what is being tested. The gate's own limits are stripped too,
#: so a value left in somebody's shell cannot change what "the default" means -
#: the tests that care set them explicitly.
#: A Telegram bot token shape the dashboard's validator accepts, and which is
#: obviously not anybody's: bot/validators.py wants `<digits>:<35+ chars>`, and a
#: row that cannot be created is a row no poller will ever start. This test is
#: about the sandbox refusing to poll it, not about the token being real.
FAKE_TELEGRAM_TOKEN = "123456789:AAExampleTokenFromBotFather-unused"

_STRIPPED = ("ABP_SANDBOX_INSTANCE", "ABP_STANDBY", "ABP_GATE", "ABP_HOME", "DASHBOARD_PORT",
             "DASHBOARD_HOST", "ABP_GATE_PUBLIC_PORTS", "ABP_GATE_CONTROL_PORT", "ABP_INSTANCES_DIR",
             "ABP_LOCALAI_PORT", "ABP_CICD_DB", "ABP_GATE_CODE_ROOT", "ABP_DEV_WORKTREES_DIR",
             "ABP_GATE_MAX_INSTANCES", "ABP_GATE_RESTART_LIMIT", "ABP_GATE_RESTART_WINDOW_S",
             "ABP_GATE_RESTART_BACKOFF_S", "ABP_GATE_WATCH_INTERVAL_S", "ABP_GATE_INSTANCE_LIFETIME",
             "ABP_GATE_UNHEALTHY_GRACE_S")

#: How long `start_gate` waits for the gate's first healthy instance, worked out
#: from the gate's own numbers rather than picked: `abp gate start`'s promise is
#: that ABP comes up, and the gate's answer to a boot that did not make it is to
#: try again inside a bounded budget - manager.DEFAULT_HEALTH_TIMEOUT_S per
#: attempt, limits.restart_limit() attempts, the restart backoff in between. A
#: test that gives up before that budget is spent is racing the product instead
#: of measuring it, and on a loaded machine (where a cold boot that used to take
#: 3s takes 40) that is exactly what happens. Returns the instant the instance
#: answers, so the normal case costs nothing.
GATE_UP_TIMEOUT_S = (manager.DEFAULT_HEALTH_TIMEOUT_S * limits.restart_limit()
                     + sum(limits.restart_backoff_s()[:limits.restart_limit() - 1]) + 30.0)


# ------------------------------------------------------------------- helpers


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def base_env(**extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    env.update(PYTHONPATH=str(CODE_ROOT), PYTHONUNBUFFERED="1", PYTHONUTF8="1", ABP_DISABLE_MDNS="1")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _win_job():
    """bot.agent_runtime.win_job when it can confine anything, else None."""
    if sys.platform != "win32":
        return None
    try:
        from bot.agent_runtime import win_job

        return win_job if win_job.is_supported() else None
    except Exception:  # noqa: BLE001 - then this test relies on process groups
        return None


class Cell:
    """One process tree's worth of confinement, owned by the test that made it.

    Windows: a job object. Everything spawned into it - the launcher, the
    interpreter it started, and every process either of those starts afterwards -
    dies when the handle is closed, including when this process dies without
    ever closing it, which is what makes "a crashed test cannot leak an ABP" a
    property of the OS rather than a promise in a finally block.

    Elsewhere: the process group, because procs.spawn starts every child with
    start_new_session=True. `wait_gone` then lets a test assert on the same
    thing either way."""

    def __init__(self, label: str = "test"):
        self.label = label
        self.roots: list[int] = []
        self._win = _win_job()
        self._handle = self._win.create() if self._win else 0

    def add(self, pid: int) -> None:
        self.roots.append(pid)
        if not self._handle:
            return
        from abp_gate import procs

        for target in procs.tree_pids(pid):
            with contextlib.suppress(OSError):
                self._win.assign(self._handle, target)

    def close(self) -> None:
        handle, self._handle = self._handle, 0
        if handle:
            # TerminateJobObject + CloseHandle; the handle is the only thing
            # keeping this job alive, so a test process that dies without getting
            # here still takes the whole cell with it.
            self._win.terminate(handle, 1)
        else:
            for pid in self.roots:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
        self.roots = []

    def __enter__(self) -> "Cell":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def wait_http(url: str, *, until: str = "any", timeout: float = 150.0, headers: Optional[dict] = None):
    """Poll `url` until it answers. `until="any"` accepts any status at all (the
    gate answers 503 while it has no instance behind it); `"200"` waits for a
    fully healthy answer."""
    deadline = time.monotonic() + timeout
    last = "never answered"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=3.0, headers=headers)
            if until == "any" or resp.status_code == 200:
                return resp
            last = f"{resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.25)
    raise AssertionError(f"{url} never answered ({until}) within {timeout}s: {last}")


def wait_gone(url: str, timeout: float = 40.0) -> None:
    """Wait for a URL to stop answering. Returns when connection is refused
    or returns an error status. Raises if still answering after timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            # If we get a response (even 200), the port is still answering
            if resp.status_code >= 400:
                return
        except httpx.ConnectError:
            # Connection refused - port is gone
            return
        except httpx.TimeoutException:
            # Connection timeout - port might be in TIME_WAIT or filtered
            # Treat as gone for practical purposes
            return
        except Exception:
            # Other errors (e.g. SSL, protocol) - treat as gone
            return
        time.sleep(0.25)
    raise AssertionError(f"{url} is still answering after {timeout}s")


def wait_lease(port: int, headers: dict, until, timeout: float = 120.0) -> dict:
    """Wait until an instance's own /api/lease satisfies `until`, and return it.

    /healthz answering 200 does NOT mean the boot is finished. bot/main.py binds
    the dashboard from a task it creates BEFORE the lease controller exists (see
    bot/main.py: the controller task and the role banner both come after
    `await mcp_client.connect_all_enabled()`), and starting the lease-gated
    services then blocks that same event loop - measured at 2.5-3.1s on an idle
    machine, and much more on a loaded one, which is longer than the health
    probe's own timeout. So reading the lease (or a log line) the moment the
    port starts answering is reading state the boot has not written yet, and on a
    loaded machine that is a failed test rather than a wrong answer.

    Waits for the condition with a generous deadline; never sleeps a fixed time
    and hopes."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = lease_of(port, headers)
        if until(last):
            return last
        time.sleep(0.25)
    raise AssertionError(f"the instance on {port} never reached the wanted lease state within "
                         f"{timeout:.0f}s; last said {last}")


def wait_log(log: Path, needle: str, timeout: float = 120.0) -> str:
    """Wait for `needle` to appear in an instance's log; return the log.

    The same gap as wait_lease above, in the log rather than in an API: what a
    boot decides (this instance is a sandbox, so nothing reaches outward) is
    written to its log at a point an answering port knows nothing about.
    Asserting the absence of the markers a poller or the scheduler would leave is
    only meaningful once the boot has got that far."""
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if needle in text:
            return text
        time.sleep(0.25)
    raise AssertionError(f"{log} never said {needle!r} within {timeout}s:\n{text[-1500:]}")


def token_from(home: Path) -> str:
    """The dashboard token bot.main generated in this throwaway install's .env."""
    token = token_or_none(home)
    if not token:
        raise AssertionError(f"no DASHBOARD_TOKEN in {home / '.env'}")
    return token


def token_or_none(home: Path) -> Optional[str]:
    """The token, or None if this install has never booted an instance.

    A gate started with --no-start has no .env at all until something starts an
    instance, and waiting for one that is never coming is how a test ends up
    timing out in the wrong place."""
    try:
        text = (home / ".env").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("DASHBOARD_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def lease_of(port: int, headers: dict) -> dict:
    resp = httpx.get(f"http://127.0.0.1:{port}/api/lease", headers=headers, timeout=15.0)
    assert resp.status_code == 200, resp.text
    return resp.json()


def spawn_instance(home: Path, *, port: int, standby: bool = False, sandbox: bool = False,
                   log: Optional[Path] = None, cell: Optional[Cell] = None) -> int:
    """A real `python -m bot.main`, windowless, exactly the way abp_gate starts one.

    Returns its pid; the caller stops it with stop_instance() and the cell (if
    given) guarantees it even if the test does not."""
    from abp_gate import procs

    extra = {}
    if sandbox:
        extra["ABP_SANDBOX_INSTANCE"] = "1"
        extra["ABP_STANDBY"] = "1"
    env = procs.instance_env(code_root=CODE_ROOT, data_root=home, port=port, extra=extra)
    # ABP_GATE=1 would tell these instances they are gate-managed, and bot/lease.py
    # reads it as "a swap may override --standby". These are hand-started, so it is
    # cleared: a standby here has to behave like a standby.
    env.pop("ABP_GATE", None)
    env.update(base_env(ABP_HOME=home, DASHBOARD_PORT=port, DASHBOARD_HOST="127.0.0.1"))
    argv = procs.instance_argv(PYTHON, standby=standby)
    pid = procs.spawn(argv, cwd=CODE_ROOT, env=env,
                      log_path=log or home.parent / f"instance-{port}.log",
                      below_normal=True, wait_s=0.2)
    if cell is not None:
        cell.add(pid)
    return pid


def stop_instance(pid: int, port: int = 0) -> None:
    from abp_gate import procs

    if pid:
        with contextlib.suppress(Exception):
            procs.stop(pid)
    if port:
        with contextlib.suppress(Exception):
            procs.wait_port_closed(port, 5.0)


def db_names(db_path: Path) -> set[str]:
    """The bot_instances names in one SQLite file, opened read-only."""
    try:
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10) as conn:
            return {row[0] for row in conn.execute("SELECT name FROM bot_instances")}
    except sqlite3.Error:
        return set()


def spawns_in(log: Path) -> int:
    """How many times a process has been started for one instance.

    procs.spawn writes a `--- <time> starting ...` banner into the instance's log
    before every single start, so this counts real starts - which is how the
    watcher tests prove it stopped starting things instead of merely appearing
    to."""
    if not log.is_file():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
               if line.startswith("--- ") and " starting " in line)


# ------------------------------------------------------------------ the gate


@dataclass
class Gate:
    """A running `python -m abp_gate` and everything a test needs to talk to it."""
    home: Path
    instances_dir: Path
    public_port: int
    control_port: int
    proc: subprocess.Popen
    cicd_db: Path
    cell: Cell = field(repr=False, default_factory=Cell)
    root: Path = field(repr=False, default=None)

    @property
    def public(self) -> str:
        return f"http://127.0.0.1:{self.public_port}"

    @property
    def control(self) -> str:
        return f"http://127.0.0.1:{self.control_port}"

    @property
    def token(self) -> str:
        """Empty for a gate that has never booted an instance: its read-only
        endpoints need no token, and a call that does need one gets a clean 401
        instead of an exception from here."""
        return token_or_none(self.home) or ""

    def headers(self) -> dict[str, str]:
        return {"X-Dashboard-Token": self.token} if self.token else {}

    def control_call(self, method: str, path: str, **kwargs):
        with httpx.Client(timeout=300.0) as client:
            resp = client.request(method, f"{self.control}{path}", headers=self.headers(), **kwargs)
        assert resp.status_code < 400, f"{method} {path} -> {resp.status_code}: {resp.text[:400]}"
        return resp.json() if resp.content else None

    def control_raw(self, method: str, path: str, **kwargs) -> httpx.Response:
        """For the calls that are SUPPOSED to be refused."""
        with httpx.Client(timeout=300.0) as client:
            return client.request(method, f"{self.control}{path}", headers=self.headers(), **kwargs)

    def instances(self) -> dict:
        return self.control_call("GET", "/api/instance")

    def lease_of(self, name: str) -> dict:
        return self.control_call("GET", f"/api/instance/{name}/lease")["lease"]

    def spawns_for(self, name: str) -> int:
        """How many processes this gate has actually started for one instance.

        A restart's replacement is named <name>-<timestamp>, so every log in that
        lineage counts: what is being bounded is process STARTS, not log files."""
        return sum(spawns_in(log) for log in sorted(self.instances_dir.glob(f"{name}*.log")))

    def gate_processes(self) -> list[int]:
        from abp_gate import procs

        return procs.tree_pids(self.proc.pid)


def start_gate(tmp_root: Path, *, wait_for_instance: bool = True,
               argv: Optional[list[str]] = None, **gate_env: str) -> Gate:
    home = tmp_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    instances_dir = tmp_root / "instances"
    public_port, control_port = free_port(), free_port()
    cicd_db = tmp_root / "cicd-events.db"
    env = base_env(ABP_HOME=home, ABP_INSTANCES_DIR=instances_dir,
                   ABP_GATE_PUBLIC_PORTS=public_port, ABP_GATE_CONTROL_PORT=control_port,
                   ABP_CICD_DB=cicd_db, **gate_env)
    cell = Cell(f"gate@{control_port}")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(PYTHON), "-m", "abp_gate", *(argv or [])], cwd=str(CODE_ROOT), env=env,
        stdout=open(tmp_root / "gate.log", "ab"), stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
    )
    cell.add(proc.pid)
    gate = Gate(home=home, instances_dir=instances_dir, public_port=public_port,
                control_port=control_port, proc=proc, cicd_db=cicd_db, cell=cell, root=tmp_root)
    try:
        wait_http(f"{gate.control}/healthz", until="any", timeout=60.0)
        if wait_for_instance:
            # 200 means the gate has a healthy instance behind it - and the token only
            # exists once an instance has booted and written it.
            wait_http(f"{gate.control}/healthz", until="200", timeout=GATE_UP_TIMEOUT_S)
            token_from(home)   # it only exists once an instance has booted and written it
    except BaseException:
        stop_gate(gate)
        raise AssertionError(f"the gate never came up; its log:\n{gate_log(tmp_root)}") from None
    return gate


def gate_log(tmp_root: Path, lines: int = 40) -> str:
    try:
        text = (tmp_root / "gate.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no gate log)"
    return "\n".join(text.splitlines()[-lines:])


def stop_gate(gate: Gate) -> None:
    from abp_gate import procs

    # A gate with no token has no instance to stop and refuses the control call,
    # so asking politely first would only be 20 seconds of waiting for a 401.
    if gate.token:
        with contextlib.suppress(Exception):
            with httpx.Client(timeout=120.0) as client:
                client.post(f"{gate.control}/api/gate/stop", headers=gate.headers(), timeout=120.0)
        for _ in range(100):
            if gate.proc.poll() is not None:
                break
            time.sleep(0.2)
    if gate.proc.poll() is None:
        # taskkill rather than procs.stop: procs.stop only signals processes whose
        # command line contains "bot.main", and a gate's is `-m abp_gate`.
        with contextlib.suppress(Exception):
            procs.taskkill_tree(gate.proc.pid)
        with contextlib.suppress(Exception):
            gate.proc.wait(15)
    # The cell goes last and unconditionally: it is the thing that makes "the
    # test crashed" safe, so it must not be conditional on anything above.
    gate.cell.close()


@pytest.fixture(scope="module")
def gate(tmp_path_factory):
    """One real gate with one real production instance, for the whole module."""
    started = start_gate(tmp_path_factory.mktemp("gate"))
    try:
        yield started
    finally:
        stop_gate(started)


@pytest.fixture
def cell():
    """Everything this test starts, confined to one job object.

    `stop_instance()` is still called by hand in these tests, because they are
    about *when* things stop; this is the backstop for the case where they never
    get there - an assertion fails, the test is interrupted, pytest is killed."""
    one = Cell()
    try:
        yield one
    finally:
        one.close()


# --------------------------------------------------------------- the proxying


def test_requests_a_stream_and_a_websocket_all_go_through_the_gate(gate: Gate):
    """The three things a proxy in front of ABP has to get right, against the real
    API of a real instance."""
    # 1. an ordinary request, with the real token, through the public port
    resp = httpx.get(f"{gate.public}/healthz", timeout=10.0)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert resp.json()["db_ok"] is True

    assert httpx.get(f"{gate.public}/api/bots", headers=gate.headers(), timeout=10.0).json() == []
    overview = httpx.get(f"{gate.public}/api/overview", headers=gate.headers(), timeout=10.0)
    assert overview.status_code == 200
    assert overview.json()["db_size_mb"] > 0

    # The front door must not become a way around the dashboard's own auth.
    assert httpx.get(f"{gate.public}/api/bots", timeout=10.0).status_code in (401, 403)

    # 2. a STREAMING response, relayed chunk by chunk instead of buffered, and
    # 3. a WEBSOCKET, both directions.
    asyncio.run(_streaming_and_websocket(gate))


async def _streaming_and_websocket(gate: Gate) -> None:
    from abp_cicd.store import EventStore

    chunks: list[str] = []

    async def read_stream() -> None:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("GET", f"{gate.public}/api/cicd/events/stream",
                                     params={"follow": "true"}, headers=gate.headers()) as resp:
                assert resp.status_code == 200, resp.text
                assert resp.headers["content-type"].startswith("text/event-stream")
                assert "chunked" in resp.headers.get("transfer-encoding", "")
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    reader = asyncio.create_task(read_stream())
    try:
        # The stream is SSE over the CI/CD log, which polls once a second - so an
        # event appended by ANOTHER process shows up in the middle of a response
        # that is still open. A proxy that buffered would show it at the end, or
        # not at all until it closed.
        EventStore(gate.cicd_db).append("note", {"level": "info", "message": "gate-stream-one"})
        assert await _await_text(chunks, "gate-stream-one"), chunks

        quiet_at = len(chunks)
        await asyncio.sleep(2.5)
        assert len(chunks) == quiet_at, f"the stream produced data with nothing to say: {chunks[quiet_at:]}"

        EventStore(gate.cicd_db).append("note", {"level": "info", "message": "gate-stream-two"})
        assert await _await_text(chunks, "gate-stream-two"), chunks
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader

    import websockets

    # A real paired device key, so the upstream treats this socket as a device and
    # delivers a signal addressed to its own id straight back to it - which can
    # only happen if the gate relayed the frame up AND the answer down.
    key = httpx.post(f"{gate.public}/api/mobile-keys", headers=gate.headers(),
                     json={"label": "gate-test", "tier": "none"}, timeout=60.0)
    assert key.status_code == 200, key.text
    device = key.json()
    async with websockets.connect(f"ws://127.0.0.1:{gate.public_port}/api/ws?token={device['key']}",
                                  open_timeout=30) as sock:
        first = json.loads(await asyncio.wait_for(sock.recv(), timeout=30))
        assert first["type"] == "device_list", first
        await sock.send(json.dumps({"type": "webrtc_signal", "to_api_key_id": device["id"],
                                    "data": {"marker": "gate-ws-probe"}}))
        seen = []
        for _ in range(10):
            message = json.loads(await asyncio.wait_for(sock.recv(), timeout=30))
            seen.append(message)
            if message.get("type") == "webrtc_signal":
                break
        assert any(m.get("type") == "webrtc_signal" and m.get("data", {}).get("marker") == "gate-ws-probe"
                   for m in seen), seen


async def _await_text(chunks: list[str], needle: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(needle in chunk for chunk in chunks):
            return True
        await asyncio.sleep(0.1)
    return False


# ------------------------------------------------------------------- the swap


class Probe:
    """A client that never stops asking, and counts every failure."""

    def __init__(self, url: str):
        self.url = url
        self.requests = 0
        self.failures: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "Probe":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(30)

    def _run(self) -> None:
        with httpx.Client(timeout=20.0) as client:
            while not self._stop.is_set():
                self.requests += 1
                try:
                    resp = client.get(self.url)
                    if resp.status_code != 200 or resp.json().get("status") != "ok":
                        self.failures.append(f"{resp.status_code}: {resp.text[:120]}")
                except Exception as exc:  # noqa: BLE001 - that is what is being measured
                    self.failures.append(f"{type(exc).__name__}: {exc}")
                self._stop.wait(0.02)


def test_swap_keeps_answering_throughout_and_moves_the_lease(gate: Gate):
    """New code in while the public port never goes quiet, and the leader lease
    moving with it: every other program talking to ABP must not notice.

    The "new code" is this same checkout on a second boot, which is enough to
    exercise every step that matters - a second process on the SAME data, a lease
    handover, a routing flip, a drain, a stop. What it cannot exercise is a
    behavioural difference between two versions, because there is nothing
    behavioural in the gate.
    """
    before = gate.instances()
    old_active = before["active"]
    assert old_active, before
    old = before["instances"][old_active]
    old_lease = gate.lease_of(old_active)
    assert old_lease["singletons_running"] is True, old_lease

    from abp_gate import procs

    # The whole tree, not the one pid: on Windows this is the launcher AND the
    # interpreter it started, and an assertion that only watched the launcher
    # would call a swap "clean" while the old ABP went on holding its port and
    # its database open.
    old_tree = procs.tree_pids(old["pid"])
    if sys.platform == "win32":
        assert len(old_tree) >= 2, f"expected a launcher + interpreter, got {old_tree}"

    with Probe(f"{gate.public}/healthz") as probe:
        result = gate.control_call("POST", "/api/instance/swap",
                                   params={"code_root": str(CODE_ROOT), "name": "swap-test"})
        assert result["ok"] is True
        assert result["instance"] == "swap-test"
        assert result["previous"] == old_active

    assert probe.requests > 5, "the probe barely ran - this test proved nothing"
    assert probe.failures == [], f"{len(probe.failures)} of {probe.requests} request(s) failed during the swap: {probe.failures[:5]}"

    after = gate.instances()
    assert after["active"] == "swap-test"
    assert after["previous"] == old_active
    new_lease = gate.lease_of("swap-test")
    assert new_lease["held"] is True and new_lease["singletons_running"] is True, new_lease
    assert new_lease["pid"] != old_lease["pid"]
    assert new_lease["holder"]["gate"] == "1", new_lease

    # The old instance really is stopped: its port is gone, not just de-registered,
    # and not one of its processes is still running.
    wait_gone(f"http://127.0.0.1:{old['port']}/healthz")
    survivors = procs.wait_gone(old_tree, 60)
    assert survivors == [], f"the replaced instance left processes behind: {survivors} (tree was {old_tree})"
    assert httpx.get(f"{gate.public}/healthz", timeout=10.0).json()["status"] == "ok"


def test_swap_to_broken_code_rolls_back_and_says_why(gate: Gate):
    """New code that cannot boot must leave the running instance exactly as it was -
    still serving, still leading - and the failure has to name itself."""
    active_before = gate.instances()["active"]
    lease_before = gate.lease_of(active_before)
    assert lease_before["singletons_running"] is True

    broken = gate.instances_dir.parent / "broken-checkout"
    (broken / "bot").mkdir(parents=True, exist_ok=True)
    # A real code root whose real bot.main dies on the spot: exactly the shape of
    # "somebody pushed a commit that does not start".
    (broken / "bot" / "__init__.py").write_text("", encoding="utf-8")
    (broken / "bot" / "main.py").write_text(
        'import sys\nprint("this build is broken", file=sys.stderr)\nraise SystemExit(3)\n', encoding="utf-8")

    with Probe(f"{gate.public}/healthz") as probe:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(f"{gate.control}/api/instance/swap", headers=gate.headers(),
                               params={"code_root": str(broken)})
    assert probe.failures == [], probe.failures[:5]
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "failed at step 1" in detail, detail

    after = gate.instances()
    assert after["active"] == active_before
    still = gate.lease_of(active_before)
    assert still["singletons_running"] is True and still["held"] is True, still
    assert still["pid"] == lease_before["pid"]
    # The failed instance is gone from the registry rather than left half-alive.
    assert all(i["health"] != "healthy" or i["name"] == active_before for i in after["instances"].values())


def test_rollback_refuses_when_there_is_nothing_to_roll_back_to(gate: Gate):
    """A swap stops the instance it replaced, so a rollback asked for afterwards has
    nothing to go back to. Saying so plainly is the point: starting the newest code
    again and calling it a rollback would be a lie.

    The swap is done here rather than assumed from another test, so this is a
    statement about the gate rather than about the order the suite happened to run
    in (which `-n 2` does not keep).

    (Rolling back to a still-live instance is what the gate's rollback() is for; it
    can only be reached when the outgoing instance was kept, which the swap flow
    deliberately does not do.)"""
    swapped = gate.control_call("POST", "/api/instance/swap",
                                params={"code_root": str(CODE_ROOT), "name": "rollback-test"})
    assert swapped["ok"] is True, swapped
    assert gate.instances()["active"] == "rollback-test"

    resp = httpx.post(f"{gate.control}/api/instance/rollback", timeout=60.0, headers=gate.headers())
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "no longer running" in detail or "no longer in the registry" in detail, detail
    # Refusing is not a failure of the gate: it is still serving.
    assert gate.instances()["active"] == "rollback-test"
    assert httpx.get(f"{gate.public}/healthz", timeout=10.0).json()["status"] == "ok"


# ------------------------------------------------------------------ sandboxed


def test_sandbox_writes_never_touch_the_real_data(gate: Gate):
    """A sandbox is a COPY of the state. A real write through it - a real bot row,
    created through the real API - must exist in the sandbox and nowhere else."""
    assert httpx.get(f"{gate.public}/api/bots", headers=gate.headers(), timeout=10.0).json() == []

    result = gate.control_call("POST", "/api/instance/sandbox", params={"code_root": str(CODE_ROOT)})
    assert result["ok"] is True, result
    name, port, url = result["instance"], result["port"], result["url"]
    data_root = Path(result["data_root"])
    try:
        # Its own state, under the instances dir - never the real one.
        assert data_root.parent == gate.instances_dir
        assert data_root != gate.home
        assert (data_root / "data" / "bot.db").is_file()
        assert (data_root / "data" / "sandbox.json").is_file()
        # Seeded with a copy of the real .env (so its own providers decrypt) and a
        # consistent copy of the live database (SQLite's online backup API).
        assert (data_root / ".env").read_bytes() == (gate.home / ".env").read_bytes()
        # Reachable directly, on its own port, never through the public one.
        assert url == f"http://127.0.0.1:{port}"

        created = httpx.post(f"{url}/api/bots", headers=gate.headers(), timeout=30.0,
                             json={"name": "sandbox-only", "platform": "telegram", "backend": "native_agent",
                                   "allowed_user_ids": [111],
                                   "credentials": {"bot_token": FAKE_TELEGRAM_TOKEN}})
        assert created.status_code == 200, created.text
        bot_id = created.json()["id"]
        sandbox_bots = httpx.get(f"{url}/api/bots", headers=gate.headers(), timeout=10.0).json()
        assert [b["name"] for b in sandbox_bots] == ["sandbox-only"], sandbox_bots

        # The real instance, behind the public port, never saw any of it.
        assert httpx.get(f"{gate.public}/api/bots", headers=gate.headers(), timeout=10.0).json() == []
        assert "sandbox-only" not in db_names(gate.home / "data" / "bot.db")
        assert "sandbox-only" in db_names(data_root / "data" / "bot.db")

        # A sandbox never leads and never reaches outward, whatever its copied config says.
        lease = lease_of(port, gate.headers())
        assert lease["sandbox"] is True and lease["held"] is False and lease["singletons_running"] is False
        assert lease["outward_blocked"], lease
        blocked = {entry["service"] for entry in lease["sandbox_blocked"]}
        assert {"platform_pollers", "scheduler", "outbox_send", "module_hubs"} <= blocked, blocked

        # ...and the send path refuses too, in this process as in any other.
        import bot.outbox as outbox

        outbox.register(bot_id, _never_sends)
        os.environ["ABP_SANDBOX_INSTANCE"] = "1"
        try:
            with pytest.raises(RuntimeError, match="never messages anyone"):
                asyncio.run(outbox.send_message(bot_id, "unused", "unused"))
        finally:
            os.environ.pop("ABP_SANDBOX_INSTANCE", None)
            outbox.unregister(bot_id)
    finally:
        gate.control_call("POST", "/api/instance/stop", params={"name": name})
    assert name not in gate.instances()["instances"]


async def _never_sends(chat_id, text) -> None:
    """A sender that WOULD work. The sandbox check has to refuse before this is
    reached, so being called at all is the failure."""
    raise AssertionError(f"a sandbox sent a message to {chat_id}")


def test_a_sandbox_starts_no_outward_connector_even_when_its_config_has_one(tmp_path, cell):
    """The rule that matters most, on a real process: a sandbox's config is a COPY of
    the real one, so its bot_instances rows carry the REAL tokens. This boots a real
    sandbox over a data root that already holds an ENABLED bot row with a token, and
    asserts that nothing polls it and nothing else leads."""
    home = tmp_path / "seeded"
    seed_port = free_port()
    seed_pid = spawn_instance(home, port=seed_port, log=tmp_path / "seed.log", cell=cell)
    try:
        wait_http(f"http://127.0.0.1:{seed_port}/healthz", until="200")
        headers = {"X-Dashboard-Token": token_from(home)}
        created = httpx.post(f"http://127.0.0.1:{seed_port}/api/bots", headers=headers, timeout=30.0,
                             json={"name": "would-poll", "platform": "telegram", "backend": "native_agent",
                                   "allowed_user_ids": [111],
                                   "credentials": {"bot_token": FAKE_TELEGRAM_TOKEN}})
        # Enabled, with a token: exactly what a poller would pick up if nothing stopped it.
        assert created.status_code == 200, created.text
        assert httpx.get(f"http://127.0.0.1:{seed_port}/api/bots",
                         headers=headers, timeout=10.0).json()[0]["enabled"] is True

        # Stop the seeder (this one is a normal leader and did start polling, badly,
        # against a token that does not exist), then boot a real sandbox on that state.
        stop_instance(seed_pid, seed_port)
        seed_pid = 0
        sandbox_port = free_port()
        sandbox_pid = spawn_instance(home, port=sandbox_port, sandbox=True, log=tmp_path / "sandbox.log", cell=cell)
        try:
            wait_http(f"http://127.0.0.1:{sandbox_port}/healthz", until="200")
            lease = lease_of(sandbox_port, headers)
            assert lease["sandbox"] is True
            assert lease["singletons_running"] is False, "a sandbox must never start the gated services"
            assert lease["held"] is False, "a sandbox must never take the real data's lease"
            assert lease["outward_blocked"], lease

            # The enabled row is right there in its config - and nothing is polling it.
            bots = httpx.get(f"http://127.0.0.1:{sandbox_port}/api/bots", headers=headers, timeout=10.0).json()
            assert [b["name"] for b in bots] == ["would-poll"], bots
            assert all(b["live_running"] is False for b in bots), bots

            # The banner is this boot saying out loud what it decided, and it is
            # written after the port starts answering (bot/main.py logs it once the
            # lease controller exists, which is after the dashboard is bound). So
            # wait for it - then, and only then, is "no poller started" a claim
            # about the whole boot rather than about the first three seconds of it.
            log = wait_log(tmp_path / "sandbox.log", "nothing reaches the outside world")
            # bot/platform_supervisor.py logs this line for every poller it starts,
            # and bot/scheduler.py logs its own - neither may appear in a sandbox.
            for marker in ("started bot instance", "scheduler started"):
                assert marker not in log, f"{marker!r} in a sandbox's log:\n{log[-1500:]}"
        finally:
            stop_instance(sandbox_pid, sandbox_port)
    finally:
        stop_instance(seed_pid, seed_port)


# ----------------------------------------------------------------------- lease


def test_the_lease_stops_two_instances_from_both_running_the_singletons(tmp_path, cell):
    """Two real instances, one data root: exactly one of them runs the platform bots,
    the scheduler and the rest - and handing the lease over moves all of it without
    either process restarting."""
    from abp_gate import procs

    home = tmp_path / "one-data-root"
    (home / "data").mkdir(parents=True, exist_ok=True)
    first_port, second_port = free_port(), free_port()

    first = spawn_instance(home, port=first_port, log=tmp_path / "first.log", cell=cell)
    second = spawn_instance(home, port=second_port, standby=True, log=tmp_path / "second.log", cell=cell)
    try:
        wait_http(f"http://127.0.0.1:{first_port}/healthz", until="200")
        wait_http(f"http://127.0.0.1:{second_port}/healthz", until="200")
        headers = {"X-Dashboard-Token": token_from(home)}

        # Both ports answering is not the same moment as "the first one leads":
        # the lease controller is started after the dashboard is bound (see
        # wait_lease), and it takes the lease itself, then starts the services
        # that go with it. So wait for the leader decision instead of assuming a
        # healthy port already made it - on a loaded machine the gap is seconds,
        # not milliseconds, and the standby's own boot is not a clock to trust
        # with somebody else's lease.
        one = wait_lease(first_port, headers, until=lambda s: s["held"] and s["singletons_running"])
        two = wait_lease(second_port, headers, until=lambda s: s["held_by_other"])
        assert one["held"] is True and one["singletons_running"] is True, one
        assert two["held"] is False and two["singletons_running"] is False, two
        assert two["held_by_other"] is True, two
        # The lease is held by a process OF the first instance. On Windows that is
        # the interpreter the launcher started, not the launcher pid we hold -
        # which is the whole reason the gate stops instances by job rather than
        # by pid.
        first_tree = procs.tree_pids(first)
        assert two["holder"]["pid"] in first_tree, (two["holder"], first_tree)
        # The sidecar names the holder, so `abp_cli instance list` and the dashboard can too.
        sidecar = json.loads((home / "data" / "abp.lease.json").read_text(encoding="utf-8"))
        assert sidecar["pid"] in first_tree and sidecar["role"] == "leader", sidecar

        # A --standby instance refuses to lead on request: standing by is the point.
        refused = httpx.post(f"http://127.0.0.1:{second_port}/api/lease/take", headers=headers, timeout=30.0)
        assert refused.status_code == 200, refused.text
        assert "standby" in refused.json().get("error", ""), refused.text

        # Handing over: the leader gives it up and stops its singletons, and is STILL
        # SERVING afterwards - which is what makes a zero-downtime swap possible.
        released = httpx.post(f"http://127.0.0.1:{first_port}/api/lease/release", headers=headers, timeout=60.0)
        assert released.status_code == 200, released.text
        assert released.json()["singletons_running"] is False
        assert httpx.get(f"http://127.0.0.1:{first_port}/healthz", timeout=10.0).json()["status"] == "ok"
        assert lease_of(first_port, headers)["held"] is False

        # A third, ordinary instance takes it as soon as it is free.
        third_port = free_port()
        third = spawn_instance(home, port=third_port, log=tmp_path / "third.log", cell=cell)
        try:
            wait_http(f"http://127.0.0.1:{third_port}/healthz", until="200")
            taken = httpx.post(f"http://127.0.0.1:{third_port}/api/lease/take", params={"timeout": 30},
                               headers=headers, timeout=60.0)
            assert taken.status_code == 200, taken.text
            assert taken.json()["singletons_running"] is True, taken.text
            assert lease_of(first_port, headers)["held"] is False
            assert lease_of(third_port, headers)["holder"]["pid"] in procs.tree_pids(third)
        finally:
            stop_instance(third, third_port)
    finally:
        stop_instance(second, second_port)
        stop_instance(first, first_port)


# ------------------------------------------- not becoming the machine's problem
#
# Everything below is about the gate refusing to run away: killing an instance has
# to take the whole tree with it, a gate that dies must not leave a pile of
# orphans behind it, and the watcher must stop restarting something that cannot
# start. These are the tests that would have caught the six thousand orphaned
# python.exe processes.


def broken_checkout(tmp_root: Path) -> Path:
    """A code root whose real bot/main.py dies on the spot: exactly the shape of
    "somebody pushed a commit that does not start"."""
    broken = tmp_root / "broken-checkout"
    (broken / "bot").mkdir(parents=True, exist_ok=True)
    (broken / "bot" / "__init__.py").write_text("", encoding="utf-8")
    (broken / "bot" / "main.py").write_text(
        'import sys\nprint("this build is broken", file=sys.stderr)\nraise SystemExit(3)\n', encoding="utf-8")
    return broken


def registry_path(gate: Gate) -> Path:
    """The gate's registry FILE, spelled out.

    abp_gate.paths resolves ABP_HOME/ABP_INSTANCES_DIR from the environment of
    whatever process asks, and the test process is not the gate - so a test that
    wants to write the gate's registry has to say which one it means."""
    return gate.instances_dir / "gate" / "registry.json"


def forget(gate: Gate, name: str) -> None:
    """Take a hand-written registry entry back out of the gate's way."""
    from abp_gate import registry

    path = registry_path(gate)

    def _apply(data: dict) -> None:
        data["instances"].pop(name, None)
        if data.get("active") == name:
            data["active"] = None

    registry.update(_apply, path)


def publish(gate: Gate, name: str, **fields) -> None:
    """Put one instance into the gate's registry by hand.

    Used to describe a situation the machine is very bad at reaching on demand -
    "the active instance is a build that cannot start" - without waiting for a
    real deployment to go wrong. The gate re-reads this file on every cycle, so
    this is the same registry the gate itself writes, and the watcher below then
    does exactly what it would do in production."""
    from abp_gate import registry

    raw = {"name": name, "code_root": str(CODE_ROOT), "data_root": str(gate.home),
           "port": free_port(), "pid": None, "role": registry.ROLE_ACTIVE,
           "health": registry.HEALTH_UNHEALTHY, "started": registry.stamp(),
           "sandbox": False, "standby": False, "log": str(gate.instances_dir / f"{name}.log"),
           **fields}
    path = registry_path(gate)

    def _apply(data: dict) -> None:
        data["instances"][name] = raw
        data["active"] = name

    registry.update(_apply, path)


def test_a_tree_kill_takes_the_launcher_and_the_interpreter_with_it(tmp_path, cell):
    """The mechanism everything else relies on, proved on its own and fast.

    On Windows `python -m bot.main` is TWO processes: the venv's python.exe
    launcher and the interpreter it starts. Signalling the launcher leaves the
    interpreter holding the instance's port and its database open, which is how a
    "restart" becomes a second ABP on top of the first one. This starts a real one
    (no gate involved) and stops it the way the gate stops an instance."""
    from abp_gate import procs

    home = tmp_path / "one-instance"
    port = free_port()
    pid = spawn_instance(home, port=port, log=tmp_path / "instance.log", cell=cell)
    wait_http(f"http://127.0.0.1:{port}/healthz", until="200")
    tree = procs.tree_pids(pid)
    if sys.platform == "win32":
        assert len(tree) >= 2, f"a venv python is a launcher with an interpreter under it; got {tree}"

    assert procs.stop(pid) is True
    assert procs.wait_gone(tree, 60) == [], f"stopping the launcher left the interpreter running: {tree}"
    assert procs.wait_port_closed(port, 30)


def test_killing_the_gate_takes_the_gate_and_the_instance_with_it(tmp_path):
    """What a supervisor, a person or a stray `taskkill` actually does to a gate.

    It kills the one process it has, and that must be enough: no half-dead gate
    still restarting instances, and no orphaned ABP holding a port. This is the
    gate's default lifetime (`gate`): every instance is in a kill-on-close job
    the gate holds the handle to, so the handle dies with it.

    It also checks the ceiling on how much of this can happen at once: with the
    cap at one instance, a second one is refused instead of started."""
    from abp_gate import procs

    gate = start_gate(tmp_path, ABP_GATE_MAX_INSTANCES="1", ABP_GATE_WATCH_INTERVAL_S="1")
    try:
        data = gate.instances()
        active = data["active"]
        assert active, data
        instance = data["instances"][active]
        assert data["limits"] == {"max_instances": 1, "alive": 1, "instance_lifetime": "gate"}, data["limits"]

        # The cap: a second instance is a refusal with a reason, not a new process.
        refused = gate.control_raw("POST", "/api/instance/sandbox", params={"code_root": str(CODE_ROOT)})
        assert refused.status_code == 409, refused.text
        assert "cap is 1" in refused.json()["detail"], refused.text
        assert gate.instances()["limits"]["alive"] == 1

        instance_tree = procs.tree_pids(instance["pid"])
        gate_tree = procs.tree_pids(gate.proc.pid)
        if sys.platform == "win32":
            assert len(instance_tree) >= 2 and len(gate_tree) >= 2, (instance_tree, gate_tree)

        # Killed as narrowly as it is possible to kill a gate: the interpreter
        # only, no /T. A /T would walk the process tree, and the instance is in
        # that tree, so it would prove nothing about the jobs - this way the
        # instances die because the gate's handles went with it, and nothing else
        # killed them. (The launcher follows its interpreter out on its own,
        # which is why killing "the gate's pid" ends up killing both.)
        assert procs.taskkill(real_interpreter(gate.proc.pid), tree=False)
        assert procs.wait_gone(gate_tree, 60) == [], f"the gate outlived its interpreter: {gate_tree}"
        assert procs.wait_gone(instance_tree, 60) == [], f"the instance outlived the gate: {instance_tree}"
        assert procs.wait_port_closed(instance["port"], 30)
        # ...and the public port is gone with it, rather than answering 503 from
        # a proxy with nothing behind it.
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{gate.public}/healthz", timeout=5.0)
    finally:
        stop_gate(gate)


def real_interpreter(launcher_pid: int) -> int:
    """The interpreter a venv launcher started, which is not the pid it reports.

    The venv's python.exe is a launcher: it starts the real interpreter as a
    child and waits for it. Two tests need the child specifically - one to prove
    the whole tree dies together, one to kill the gate WITHOUT taking a detached
    instance down with it - so this is where that distinction is made."""
    import psutil

    parent = psutil.Process(launcher_pid)
    kids = [c for c in parent.children() if c.name().lower().startswith("python")]
    assert len(kids) == 1, f"expected exactly one interpreter under {launcher_pid}, got {kids}"
    return kids[0].pid


def test_the_detached_lifetime_is_the_one_documented_exception(tmp_path):
    """`abp_cli gate start` asks for `detached`, and this is what that buys: the
    ACTIVE instance survives the gate being killed, so a gate that comes straight
    back can re-adopt it and ABP is up again in seconds instead of a boot.

    Killed the way a Windows supervisor actually kills a gate - the interpreter,
    not the tree - because a `taskkill /T` walks the process tree and the instance
    is in that tree: it would die with the gate either way and the test would
    prove nothing. Everything that is in a job (standbys, sandboxes) still dies;
    the `gate` lifetime above is that case."""
    from abp_gate import procs

    gate = start_gate(tmp_path, argv=["--instance-lifetime", "detached"],
                      ABP_GATE_WATCH_INTERVAL_S="1")
    try:
        started = gate.instances()
        instance = started["instances"][started["active"]]
        assert started["limits"]["instance_lifetime"] == "detached", started["limits"]
        tree = procs.tree_pids(instance["pid"])
        interpreter = real_interpreter(gate.proc.pid)

        assert procs.taskkill(interpreter, tree=False)
        assert procs.wait_gone(procs.tree_pids(gate.proc.pid), 60) == [], "the gate outlived its interpreter"

        # Still serving, still holding the data - which is the point.
        assert httpx.get(f"http://127.0.0.1:{instance['port']}/healthz", timeout=10.0).json()["status"] == "ok"
        assert procs.alive(instance["pid"]), f"the detached instance died with the gate: {tree}"
        assert procs.stop(instance["pid"]) is True
        assert procs.wait_gone(tree, 60) == []
    finally:
        stop_gate(gate)


@pytest.fixture
def watcher_gate(tmp_path):
    """A gate with no instance of its own, watching fast, with a small budget.

    The limits are the ones limits.py exposes, lowered so the whole budget is
    spent inside a test instead of over ten minutes: a restart every half second,
    at most two of them in two minutes."""
    started = start_gate(
        tmp_path, argv=["--no-start"], wait_for_instance=False,
        ABP_GATE_WATCH_INTERVAL_S="0.5", ABP_GATE_RESTART_LIMIT="2",
        ABP_GATE_RESTART_WINDOW_S="120", ABP_GATE_RESTART_BACKOFF_S="0.2",
    )
    try:
        yield started
    finally:
        stop_gate(started)


def wait_for_error(gate: Gate, name: str, needle: str, timeout: float = 90.0) -> dict:
    """Wait until the gate has written `needle` into one instance's error field.

    Read straight from the registry file the gate writes - that is the gate's own
    verdict, not one re-derived by asking it politely over the control API while
    the thing being measured is still happening. Health on its own is too early a
    signal: an exhausted restart budget looks exactly like a failed restart for
    one cycle, and what is under test is that the gate then says it has
    stopped trying."""
    from abp_gate import registry

    path = registry_path(gate)
    deadline = time.monotonic() + timeout
    seen: dict = {}
    while time.monotonic() < deadline:
        inst = registry.get(name, path)
        seen = inst.to_dict() if inst is not None else {}
        if needle in (seen.get("error") or ""):
            return seen
        time.sleep(0.25)
    raise AssertionError(f"{name} never reported {needle!r}; last seen {seen}\n"
                         f"{gate_log(gate.root)}")


def test_a_gate_with_nothing_running_gives_up_on_starting_production_too(tmp_path):
    """The mirror image of the restart budget: a boot that failed is worth
    another go, and a checkout that cannot start is not.

    `abp gate start`'s promise is that ABP comes up; a gate whose own first
    attempt failed has to try again or it is a promise with a hole in it. Bounded
    the same way, and it says so when it stops. The gate here runs a code root
    whose bot/main.py exits at once, so every attempt is a real process that dies
    - counted here, not assumed."""
    gate = start_gate(
        tmp_path, argv=["--code-root", str(broken_checkout(tmp_path))], wait_for_instance=False,
        ABP_GATE_WATCH_INTERVAL_S="0.5", ABP_GATE_RESTART_LIMIT="2",
        ABP_GATE_RESTART_WINDOW_S="120", ABP_GATE_RESTART_BACKOFF_S="0.2",
    )
    try:
        deadline = time.monotonic() + 60.0
        gave_up = ""
        while time.monotonic() < deadline:
            gave_up = "\n".join(line for line in gate_log(tmp_path).splitlines()
                               if "has stopped trying" in line)
            if gave_up:
                break
            time.sleep(0.5)
        assert gave_up, f"the gate never said it had stopped trying:\n{gate_log(tmp_path)}"
        assert "abp_cli instance swap" in gave_up, gave_up

        # Its own first attempt plus one retry, and not one more: two starts, and
        # the gate is done asking.
        assert gate.spawns_for("prod") == 2, gate.spawns_for("prod")
        time.sleep(2.0)
        assert gate.spawns_for("prod") == 2, "the gate kept starting a build that cannot start"
        assert gate.instances()["active"] is None
    finally:
        stop_gate(gate)


def test_a_gate_started_with_no_start_never_starts_anything(tmp_path):
    """`--no-start` means bind the ports and start nothing, and the watcher's
    "there is no instance, let me start one" has to respect that: it is exactly
    the instruction a person gives when they do not want an ABP yet."""
    gate = start_gate(
        tmp_path, argv=["--no-start"], wait_for_instance=False, ABP_GATE_WATCH_INTERVAL_S="0.5",
        ABP_GATE_RESTART_BACKOFF_S="0.2",
    )
    try:
        time.sleep(3.0)
        assert gate.instances()["instances"] == {}, gate.instances()
        assert not list(gate.instances_dir.glob("prod*.log")), "it started something anyway"
        assert httpx.get(f"{gate.control}/healthz", timeout=10.0).status_code == 503
    finally:
        stop_gate(gate)


def test_an_instance_that_never_worked_is_not_restarted(watcher_gate: Gate):
    """A build that cannot start is not a process that crashed.

    The watcher used to restart whatever was unhealthy on every cycle, which is
    how one broken checkout becomes thousands of running processes; this is the
case that has to produce zero starts."""
    name = "never-worked"
    # No boot at all: the instance is described, not run. A pid that has already
    # exited is exactly what a crashed instance looks like from the registry.
    dead = subprocess.Popen([str(PYTHON), "-c", "pass"], creationflags=NO_WINDOW)
    dead.wait(30)
    publish(watcher_gate, name, code_root=str(broken_checkout(watcher_gate.root)),
            port=free_port(), pid=dead.pid, ever_healthy=False)
    try:
        inst = wait_for_error(watcher_gate, name, "never answered /healthz")
        assert inst["health"] == "failed", inst
        assert watcher_gate.spawns_for(name) == 0, (
            f"the watcher started {watcher_gate.spawns_for(name)} process(es) for an instance "
            f"that never worked:\n{gate_log(watcher_gate.root)}")
        # And it says so in the status somebody actually runs.
        status = watcher_gate.control_call("GET", "/api/gate")
        assert status["restarts"]["instances"][name]["ever_healthy"] is False
        assert status["instances"][name]["health"] == "failed"
    finally:
        forget(watcher_gate, name)


def test_the_watcher_stops_after_its_restart_budget(watcher_gate: Gate):
    """An instance that WAS healthy, and now cannot start, gets a bounded number
    of attempts - and then the gate stops, marks it failed, and says so.

    Two restarts of three attempts each (manager.restart_active's own bound) is
    six processes, and not one more however long the test waits: this is the exact
arithmetic the runaway was missing."""
    name = "keeps-dying"
    from abp_gate import procs

    broken = broken_checkout(watcher_gate.root)
    dead = subprocess.Popen([str(PYTHON), "-c", "pass"], creationflags=NO_WINDOW)
    dead.wait(30)
    publish(watcher_gate, name, code_root=str(broken), port=free_port(), pid=dead.pid,
            ever_healthy=True)
    try:
        inst = wait_for_error(watcher_gate, name, "stopped restarting it")
        assert inst["health"] == "failed", inst
        starts = watcher_gate.spawns_for(name)
        assert starts == 6, f"expected 2 restarts x 3 attempts, saw {starts} start(s):\n" \
                            f"{gate_log(watcher_gate.root)}"

        # ...and it stays stopped: the gate does not keep trying behind our back.
        time.sleep(3.0)
        assert watcher_gate.spawns_for(name) == starts
        status = watcher_gate.control_call("GET", "/api/gate")
        report = status["restarts"]["instances"][name]
        assert report["circuit_open"] is True and report["restarts_last_window"] == 2, report
        assert status["restarts"]["limit"] == 2
        # Nothing is left running from those six attempts.
        assert not [i for i in status["instances"].values() if procs.alive(i["pid"])]
    finally:
        forget(watcher_gate, name)


def wait_for_starts(gate: Gate, name: str, at_least: int, timeout: float = 60.0) -> int:
    """Wait until the gate has really started `at_least` processes for `name`.

    Counted from the spawn banners in the logs, not from the registry: the thing
    under test is whether the gate STARTS a replacement, and a registry entry
    saying "unhealthy" says nothing about processes."""
    from abp_gate import registry

    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        seen = gate.spawns_for(name)
        if seen >= at_least:
            return seen
        time.sleep(0.25)
    raise AssertionError(f"the gate started {seen} process(es) for {name!r} within {timeout:.0f}s, "
                         f"expected {at_least}:\n{gate_log(gate.root)}"
                         f"\n{registry.get(name, registry_path(gate))}")


def test_a_running_instance_too_busy_to_answer_is_left_alone(tmp_path):
    """The other half of "is it crashed?": a process the OS says is THERE, that
    missed one probe, is not a crashed process.

    Measured on this machine: a booting ABP blocks its own event loop for 2.5-3.1s
    while it starts its lease-gated services, against a health probe with a 3s
    timeout - so on a loaded machine one missed probe is what a healthy instance
    looks like. A gate that acts on it kills a working ABP and boots a
    replacement: an outage manufactured by the thing that exists to prevent them,
    and a moving pid for anything reading the registry.

    So an unanswerable-but-running instance has to stay that way for
    ABP_GATE_UNHEALTHY_GRACE_S first - and must NOT stay that way forever, which
    is the second half of this test: once the grace is spent the gate does try.
    The instance here is a real running process that serves nothing, published by
    hand like the two watcher tests above; a dead pid is the crash case, and
    `test_the_watcher_stops_after_its_restart_budget` already covers that one."""
    from abp_gate import procs

    grace = 8.0
    gate = start_gate(tmp_path, argv=["--no-start"], wait_for_instance=False,
                      ABP_GATE_WATCH_INTERVAL_S="0.5", ABP_GATE_RESTART_BACKOFF_S="0.2",
                      ABP_GATE_UNHEALTHY_GRACE_S=str(grace))
    # A real process that is up and is not serving: exactly what the gate sees
    # when an instance's event loop cannot answer in time.
    busy = subprocess.Popen([str(PYTHON), "-c", "import time; time.sleep(300)"],
                            creationflags=NO_WINDOW)
    name = "busy-not-dead"
    try:
        publish(gate, name, code_root=str(broken_checkout(tmp_path)),
                port=free_port(), pid=busy.pid, ever_healthy=True)
        # Wait for the gate to have LOOKED and decided to wait - its own words,
        # not a sleep - and then check what it did while waiting: nothing.
        wait_log(tmp_path / "gate.log", "leaving it alone rather than replacing a busy instance",
                 timeout=30.0)
        assert gate.spawns_for(name) == 0, (
            f"the gate replaced an instance that is running and simply not answering:\n"
            f"{gate_log(gate.root)}")
        assert procs.alive(busy.pid), "the gate killed a process that was still there"

        # ...and once the grace is spent it does act: delayed, not disabled.
        wait_for_starts(gate, name, 1)
    finally:
        forget(gate, name)
        stop_gate(gate)
        # taskkill, not procs.stop: this is not one of ours by command line, and
        # killing the launcher alone would leave the interpreter it started
        # sleeping on for another five minutes.
        with contextlib.suppress(Exception):
            procs.taskkill_tree(busy.pid)
        busy.wait(30)
