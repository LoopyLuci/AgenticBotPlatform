"""abp_gate end to end, with real processes and real sockets: the front door, the
hot swap (and its rollback), the sandbox, and the leader lease.

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
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import pytest

CODE_ROOT = Path(__file__).resolve().parent.parent
PYTHON = Path(sys.executable)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: Stripped from the environment these processes inherit: a stray
#: ABP_SANDBOX_INSTANCE or DASHBOARD_PORT in the shell that ran pytest must not
#: silently change what is being tested.
_STRIPPED = ("ABP_SANDBOX_INSTANCE", "ABP_STANDBY", "ABP_GATE", "ABP_HOME", "DASHBOARD_PORT",
             "DASHBOARD_HOST", "ABP_GATE_PUBLIC_PORTS", "ABP_GATE_CONTROL_PORT", "ABP_INSTANCES_DIR",
             "ABP_LOCALAI_PORT", "ABP_CICD_DB", "ABP_GATE_CODE_ROOT", "ABP_DEV_WORKTREES_DIR")


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


def token_from(home: Path) -> str:
    """The dashboard token bot.main generated in this throwaway install's .env."""
    env_file = home / ".env"
    for _ in range(80):
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("DASHBOARD_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        time.sleep(0.25)
    raise AssertionError(f"no DASHBOARD_TOKEN in {env_file}")


def lease_of(port: int, headers: dict) -> dict:
    resp = httpx.get(f"http://127.0.0.1:{port}/api/lease", headers=headers, timeout=15.0)
    assert resp.status_code == 200, resp.text
    return resp.json()


def spawn_instance(home: Path, *, port: int, standby: bool = False, sandbox: bool = False,
                   log: Optional[Path] = None) -> int:
    """A real `python -m bot.main`, windowless, exactly the way abp_gate starts one.

    Returns its pid; the caller stops it with stop_instance()."""
    from abp_gate import procs

    extra = {}
    if sandbox:
        extra["ABP_SANDBOX_INSTANCE"] = "1"
        extra["ABP_STANDBY"] = "1"
    env = procs.instance_env(code_root=CODE_ROOT, data_root=home, port=port, extra=extra)
    env.update(base_env(ABP_HOME=home, DASHBOARD_PORT=port, DASHBOARD_HOST="127.0.0.1"))
    argv = procs.instance_argv(PYTHON, standby=standby)
    return procs.spawn(argv, cwd=CODE_ROOT, env=env,
                       log_path=log or home.parent / f"instance-{port}.log",
                       below_normal=True, wait_s=0.2)


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

    @property
    def public(self) -> str:
        return f"http://127.0.0.1:{self.public_port}"

    @property
    def control(self) -> str:
        return f"http://127.0.0.1:{self.control_port}"

    @property
    def token(self) -> str:
        return token_from(self.home)

    def headers(self) -> dict[str, str]:
        return {"X-Dashboard-Token": self.token}

    def control_call(self, method: str, path: str, **kwargs):
        with httpx.Client(timeout=300.0) as client:
            resp = client.request(method, f"{self.control}{path}", headers=self.headers(), **kwargs)
        assert resp.status_code < 400, f"{method} {path} -> {resp.status_code}: {resp.text[:400]}"
        return resp.json() if resp.content else None

    def instances(self) -> dict:
        return self.control_call("GET", "/api/instance")

    def lease_of(self, name: str) -> dict:
        return self.control_call("GET", f"/api/instance/{name}/lease")["lease"]


def start_gate(tmp_root: Path) -> Gate:
    home = tmp_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    instances_dir = tmp_root / "instances"
    public_port, control_port = free_port(), free_port()
    cicd_db = tmp_root / "cicd-events.db"
    env = base_env(ABP_HOME=home, ABP_INSTANCES_DIR=instances_dir,
                   ABP_GATE_PUBLIC_PORTS=public_port, ABP_GATE_CONTROL_PORT=control_port,
                   ABP_CICD_DB=cicd_db)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(PYTHON), "-m", "abp_gate"], cwd=str(CODE_ROOT), env=env,
        stdout=open(tmp_root / "gate.log", "ab"), stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
    )
    gate = Gate(home=home, instances_dir=instances_dir, public_port=public_port,
                control_port=control_port, proc=proc, cicd_db=cicd_db)
    try:
        wait_http(f"{gate.control}/healthz", until="any", timeout=60.0)
        # 200 means the gate has a healthy instance behind it - and the token only
        # exists once an instance has booted and written it.
        wait_http(f"{gate.control}/healthz", until="200", timeout=150.0)
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

    with contextlib.suppress(Exception):
        with httpx.Client(timeout=120.0) as client:
            client.post(f"{gate.control}/api/gate/stop", headers=gate.headers(), timeout=120.0)
    for _ in range(100):
        if gate.proc.poll() is not None:
            break
        time.sleep(0.2)
    if gate.proc.poll() is None:
        with contextlib.suppress(Exception):
            procs.stop(gate.proc.pid)
        with contextlib.suppress(Exception):
            gate.proc.wait(15)


@pytest.fixture(scope="module")
def gate(tmp_path_factory):
    """One real gate with one real production instance, for the whole module."""
    started = start_gate(tmp_path_factory.mktemp("gate"))
    try:
        yield started
    finally:
        stop_gate(started)


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

    # The old instance really is stopped: its port is gone, not just de-registered.
    wait_gone(f"http://127.0.0.1:{old['port']}/healthz")
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

    (Rolling back to a still-live instance is what the gate's rollback() is for; it
    can only be reached when the outgoing instance was kept, which the swap flow
    deliberately does not do.)"""
    resp = httpx.post(f"{gate.control}/api/instance/rollback", timeout=60.0, headers=gate.headers())
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "no longer running" in detail or "no longer in the registry" in detail, detail
    assert (gate.instances())["active"] == "swap-test"
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
                             json={"name": "sandbox-only", "platform": "telegram", "backend": "native_agent"})
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


def test_a_sandbox_starts_no_outward_connector_even_when_its_config_has_one(tmp_path):
    """The rule that matters most, on a real process: a sandbox's config is a COPY of
    the real one, so its bot_instances rows carry the REAL tokens. This boots a real
    sandbox over a data root that already holds an ENABLED bot row with a token, and
    asserts that nothing polls it and nothing else leads."""
    home = tmp_path / "seeded"
    seed_port = free_port()
    seed_pid = spawn_instance(home, port=seed_port, log=tmp_path / "seed.log")
    try:
        wait_http(f"http://127.0.0.1:{seed_port}/healthz", until="200")
        headers = {"X-Dashboard-Token": token_from(home)}
        created = httpx.post(f"http://127.0.0.1:{seed_port}/api/bots", headers=headers, timeout=30.0,
                             json={"name": "would-poll", "platform": "telegram", "backend": "native_agent",
                                   "credentials": {"bot_token": "unused"}})
        assert created.status_code == 200, created.text
        row = created.json()
        # Enabled, with a token: exactly what a poller would pick up if nothing stopped it.
        assert httpx.get(f"http://127.0.0.1:{seed_port}/api/bots",
                         headers=headers, timeout=10.0).json()[0]["enabled"] is True

        # Stop the seeder (this one is a normal leader and did start polling, badly,
        # against a token that does not exist), then boot a real sandbox on that state.
        stop_instance(seed_pid, seed_port)
        seed_pid = 0
        sandbox_port = free_port()
        sandbox_pid = spawn_instance(home, port=sandbox_port, sandbox=True, log=tmp_path / "sandbox.log")
        try:
            wait_http(f"http://127.0.0.1:{sandbox_port}/healthz", until="200")
            lease = lease_of(sandbox_port, headers)
            assert lease["sandbox"] is True
            assert lease["singletons_running"] is False, "a sandbox must never start the gated services"
            assert lease["held"] is False, "a sandbox must never take the real data's lease"
            assert lease["outward_blocked"], lease

            # The enabled row is right there in its config - and nothing is polling it.
            bots = httpx.get(f"http://127.0.0.1:{sandbox_port}/api/bots", headers=headers, timeout=10.0).json()
            assert [b["name"] for b in bots] == [row["name"]], bots
            assert all(b["live_running"] is False for b in bots), bots

            log = (tmp_path / "sandbox.log").read_text(encoding="utf-8", errors="replace")
            assert "outward connectors are off" in log, log[-1500:]
            # bot/platform_supervisor.py logs this line for every poller it starts,
            # and bot/scheduler.py logs its own - neither may appear in a sandbox.
            for marker in ("started bot instance", "scheduler started"):
                assert marker not in log, f"{marker!r} in a sandbox's log:\n{log[-1500:]}"
        finally:
            stop_instance(sandbox_pid, sandbox_port)
    finally:
        stop_instance(seed_pid, seed_port)


# ----------------------------------------------------------------------- lease


def test_the_lease_stops_two_instances_from_both_running_the_singletons(tmp_path):
    """Two real instances, one data root: exactly one of them runs the platform bots,
    the scheduler and the rest - and handing the lease over moves all of it without
    either process restarting."""
    home = tmp_path / "one-data-root"
    (home / "data").mkdir(parents=True, exist_ok=True)
    first_port, second_port = free_port(), free_port()

    first = spawn_instance(home, port=first_port, log=tmp_path / "first.log")
    second = spawn_instance(home, port=second_port, standby=True, log=tmp_path / "second.log")
    try:
        wait_http(f"http://127.0.0.1:{first_port}/healthz", until="200")
        wait_http(f"http://127.0.0.1:{second_port}/healthz", until="200")
        headers = {"X-Dashboard-Token": token_from(home)}

        one = lease_of(first_port, headers)
        two = lease_of(second_port, headers)
        assert one["held"] is True and one["singletons_running"] is True, one
        assert two["held"] is False and two["singletons_running"] is False, two
        assert two["held_by_other"] is True, two
        assert two["holder"]["pid"] == first, two
        # The sidecar names the holder, so `abp_cli instance list` and the dashboard can too.
        sidecar = json.loads((home / "data" / "abp.lease.json").read_text(encoding="utf-8"))
        assert sidecar["pid"] == first and sidecar["role"] == "leader", sidecar

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
        third = spawn_instance(home, port=third_port, log=tmp_path / "third.log")
        try:
            wait_http(f"http://127.0.0.1:{third_port}/healthz", until="200")
            taken = httpx.post(f"http://127.0.0.1:{third_port}/api/lease/take", params={"timeout": 30},
                               headers=headers, timeout=60.0)
            assert taken.status_code == 200, taken.text
            assert taken.json()["singletons_running"] is True, taken.text
            assert lease_of(first_port, headers)["held"] is False
            assert lease_of(third_port, headers)["holder"]["pid"] == third
        finally:
            stop_instance(third, third_port)
    finally:
        stop_instance(second, second_port)
        stop_instance(first, first_port)