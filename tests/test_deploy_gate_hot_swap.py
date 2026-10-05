"""A deploy behind the always-on gate is a hot swap, not an outage.

scripts/deploy_local.py stops the app, installs over it and starts it again -
a minute or two in which ABP and every Telegram bot it runs are not there.
abp_gate exists so that does not have to happen: one process owns 8787, and the
code behind it can be replaced while the port never goes quiet. These tests are
the join between the two, and they are the only place that join is exercised.

Nothing here is mocked. A real `python -m abp_gate` is started on throwaway
ABP_HOMEs and throwaway localhost ports, it really starts instances by spawning
`python -m bot.main` out of a code root, and the deploy really copies a bundle
into a versioned folder and really asks the gate - over HTTP, authenticated with
the install's own DASHBOARD_TOKEN - to swap to it. The instance is small on
purpose (below): a stand-in for bot/main.py that answers the four routes a swap
and a deploy's verification actually ask for, and reports the build stamp out of
its own code root so a test can tell WHICH version is serving through the gate.

    python -m pytest -q -p no:cacheprovider --no-cov -n 2 tests/test_deploy_gate_hot_swap.py

Every process this file starts goes into a bot.sandbox_ns cell first - a Windows
job object with KILL_ON_JOB_CLOSE - and the cell is closed by the fixture's
teardown, so a failed assertion cannot leave a gate, an instance or a
half-installed folder behind. The gate holds its own per-instance job objects,
so a hard kill of the cell takes the whole tree: that is not tidiness, it is the
difference between a test and six thousand orphaned python.exe.
"""
from __future__ import annotations

import importlib.util
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import psutil
import pytest

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("deploy_local", _SCRIPTS / "deploy_local.py")
deploy_local = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
sys.modules["deploy_local"] = deploy_local
_SPEC.loader.exec_module(deploy_local)

from bot.sandbox_ns.cell import Cell  # noqa: E402
from bot.sandbox_ns.policy import Policy  # noqa: E402

# Every test here starts real ABP instances (and a gate). Run them one after another in one worker: several at once on
# a machine that is also running the rest of the suite starve each other of the CPU their health checks need.
pytestmark = pytest.mark.xdist_group("abp_gate_live")

EXE = deploy_local.EXE_NAME
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: The API the stand-in instance serves, and the one the fake checkout's
#: docs/api/openapi.json declares: an app serving a different API is a failed
#: deploy, however healthy it looks.
SPEC_PATHS = ["/healthz", "/openapi.json", "/api/bots", "/api/lease"]

#: Stripped from the environment these processes inherit: a stray ABP_HOME or
#: ABP_SANDBOX_INSTANCE in the shell that ran pytest must not silently change
#: what is being tested. The gate's own limits go too, so a value left in
#: somebody's shell cannot change what "the default" means.
_STRIPPED = ("ABP_SANDBOX_INSTANCE", "ABP_STANDBY", "ABP_GATE", "ABP_HOME", "ABP_INSTANCES_DIR",
             "DASHBOARD_PORT", "DASHBOARD_HOST", "DASHBOARD_TOKEN", "ABP_GATE_PUBLIC_PORTS",
             "ABP_GATE_CONTROL_PORT", "ABP_LOCALAI_PORT", "ABP_CICD_DB", "ABP_GATE_CODE_ROOT",
             "ABP_GATE_MAX_INSTANCES", "ABP_GATE_RESTART_LIMIT", "ABP_GATE_WATCH_INTERVAL_S",
             "ABP_GATE_INSTANCE_LIFETIME", "ABP_GATE_RESTART_BACKOFF_S", "ABP_GATE_RESTART_WINDOW_S",
             "ABP_GATE_LOCALAI_PORT", "ABP_DEV_WORKTREES_DIR", "ABP_DEPLOY_OUTSIDE_CHECKOUT")


# --------------------------------------------------------------------------- #
# The stand-in for bot.main
# --------------------------------------------------------------------------- #
#: What a swap and a deploy's verification ask an instance for, and nothing
#: else: /healthz (with the build stamp out of its own code root), /openapi.json,
#: the token-protected /api/bots, and the two /api/lease verbs the gate uses to
#: hand the leader lease over. The lease is REAL state in this process - the
#: handover only works if the outgoing instance is told to let go and the
#: incoming one is told to take it, and a swap that skipped that would pass.
_TINY_MAIN = textwrap.dedent('''
    """A stand-in for bot.main: the routes a gate swap and a deploy's own
    verification ask an instance for, on the port the gate told it to bind."""

    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    CODE_ROOT = os.getcwd()
    SPEC = json.load(open(os.environ["TINY_SPEC"], encoding="utf-8"))
    PORT = int(os.environ["DASHBOARD_PORT"])
    _lock = threading.Lock()
    _lease = {"running": True}

    def home():
        return os.environ.get("ABP_HOME") or CODE_ROOT

    def token():
        """This install's own DASHBOARD_TOKEN, read the way bot/envfile.py reads
        it: out of the state root's .env, which is where the gate and the deploy
        look for it too."""
        try:
            text = open(os.path.join(home(), ".env"), encoding="utf-8").read()
        except OSError:
            return ""
        for line in text.splitlines():
            if line.startswith("DASHBOARD_TOKEN="):
                return line.split("=", 1)[1].strip().strip(\'"\').strip("\'")
        return ""

    def ensure_token():
        """A real ABP generates DASHBOARD_TOKEN on its first boot, and the
        gate's control API refuses every call without one - so the stand-in
        writes it, which is what makes this install authenticatable at all."""
        os.makedirs(home(), exist_ok=True)
        path = os.path.join(home(), ".env")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("DASHBOARD_TOKEN=unused\\n")
        return token()

    def stamp():
        """The .abp_build.json a deploy installed into THIS code root. It is what
        /healthz reports as `bundle`, so a test (and a person) can tell which
        version is actually serving."""
        try:
            with open(os.path.join(CODE_ROOT, ".abp_build.json"), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = self.path.split("?")[0]
            if route == "/healthz":
                return self._send(200, {"status": "ok", "db_ok": True, "bundle": stamp()})
            if route == "/openapi.json":
                paths = SPEC["paths"]
                # A build stamp may ask for a TRUNCATED spec, which is how a
                # "this deploy swapped fine and serves the wrong API" bundle is
                # built - the case a verification failure has to catch.
                shown = stamp().get("paths_shown")
                if shown:
                    paths = paths[:shown]
                return self._send(200, {"openapi": "3.1.0", "paths": {p: {} for p in paths}})
            if route == "/api/lease":
                with _lock:
                    return self._send(200, {"held": True, "singletons_running": _lease["running"],
                                           "pid": os.getpid()})
            if route == "/api/bots":
                if self.headers.get("X-Dashboard-Token") != TOKEN:
                    return self._send(401, {"detail": "invalid dashboard token"})
                rows = [dict(row, live_running=bool(row.get("live", True))) for row in SPEC.get("bots", [])]
                return self._send(200, rows)
            return self._send(404, {"detail": "not found"})

        def do_POST(self):
            route = self.path.split("?")[0]
            if route == "/api/lease/release":
                with _lock:
                    _lease["running"] = False
                return self._send(200, {"held": False, "singletons_running": False, "pid": os.getpid()})
            if route == "/api/lease/take":
                with _lock:
                    _lease["running"] = True
                return self._send(200, {"held": True, "singletons_running": True, "pid": os.getpid()})
            return self._send(404, {"detail": "not found"})

        def log_message(self, *args):
            pass

    TOKEN = ensure_token()
    print("stand-in instance listening on %d (code root %s)" % (PORT, CODE_ROOT), flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
''').lstrip("\n")

#: Just enough of bot/envfile.py for a deploy to resolve this install's own
#: DASHBOARD_TOKEN the way it resolves the real one: out of $ABP_HOME/.env, and
#: only that file. Nothing here is a shortcut around the real thing - the deploy
#: spawns an interpreter and asks it, so the answer comes from the code that is
#: going to be running.
_TINY_ENVFILE = textwrap.dedent('''
    """The one thing of bot/envfile.py a deploy asks for: DASHBOARD_TOKEN out of
    the state root's own .env."""

    import os
    from pathlib import Path


    def get_var(key):
        home = os.environ.get("ABP_HOME") or str(Path.cwd())
        try:
            text = Path(home).joinpath(".env").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip(\'"\').strip("\'")
        return None
''').lstrip("\n")


# --------------------------------------------------------------------------- #
# The world: a fake checkout, a real build, a real gate
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class World:
    """Everything one test needs: where the checkout is, where the build is, and
    where the gate is listening."""

    root: Path
    checkout: Path
    build: Path
    home: Path
    instances_dir: Path
    spec: Path
    public_port: int = 0
    control_port: int = 0
    lines: list[str] = field(default_factory=list)

    @property
    def install_dir(self) -> Path:
        return self.checkout / "desktop-app" / "src-tauri" / "target" / "release"

    @property
    def public(self) -> str:
        return f"http://127.0.0.1:{self.public_port}"

    def versioned(self) -> list[Path]:
        return [path for _n, path in deploy_local.versioned_dirs(self.install_dir)]

    def registry(self) -> dict:
        return json.loads((self.instances_dir / "gate" / "registry.json").read_text(encoding="utf-8"))

    def active(self) -> dict:
        data = self.registry()
        return (data.get("instances") or {}).get(data.get("active") or "") or {}

    def served_commit(self) -> Optional[str]:
        return httpx.get(f"{self.public}/healthz", timeout=10.0).json().get("bundle", {}).get("commit")

    def log(self, message: str) -> None:
        self.lines.append(message)


def _write_tiny_app(root: Path) -> None:
    """A code root whose bot/main.py is the stand-in above: enough for the gate
    to accept it as an ABP checkout and to really start a process out of it."""
    pkg = root / "bot"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "main.py").write_text(_TINY_MAIN, encoding="utf-8")
    (pkg / "envfile.py").write_text(_TINY_ENVFILE, encoding="utf-8")


def _stage(world: World, *, commit: str, broken: bool = False, paths_shown: int = 0) -> Path:
    """A finished build in the cargo target dir, exactly the shape
    `cargo tauri build` leaves: each bundle.resources destination plus the app
    binary. The bot/ in it is the stand-in app, so the instance the gate starts
    from a versioned folder is a real process serving the real build stamp.

    `broken` is the bundle that cannot start at all - the shape of "somebody
    pushed a commit that does not boot", which the gate has to roll back.
    `paths_shown` is the subtler one: it boots, becomes healthy, takes the
    lease, and then serves an API this commit does not document."""
    profile = world.build / "release"
    shutil.rmtree(profile / "bot", ignore_errors=True)
    _write_tiny_app(profile)
    if broken:
        (profile / "bot" / "main.py").write_text(
            'import sys\nprint("this build is broken", file=sys.stderr)\nraise SystemExit(3)\n',
            encoding="utf-8")
    stamp = {"commit": commit, "built_at": "2026-10-05T00:00:00+00:00", "dirty": False}
    if paths_shown:
        stamp["paths_shown"] = paths_shown
    (profile / ".abp_build.json").write_text(json.dumps(stamp), encoding="utf-8")
    (profile / EXE).write_bytes(b"MZ not really an exe " + commit.encode())
    return profile


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A throwaway ABP_HOME, a fake checkout, a real build - and the environment
    both the deploy and the gate resolve their roots from."""
    root = tmp_path / "gate"
    checkout = root / "checkout"
    desk = checkout / "desktop-app" / "src-tauri"
    desk.mkdir(parents=True)
    _write_tiny_app(checkout)
    (desk / "tauri.conf.json").write_text(json.dumps({"bundle": {"resources": {
        "stage/bot": "bot", "stage/.abp_build.json": ".abp_build.json"}}}), encoding="utf-8")
    (checkout / "docs" / "api").mkdir(parents=True)
    (checkout / "docs" / "api" / "openapi.json").write_text(
        json.dumps({"openapi": "3.1.0", "paths": {p: {} for p in SPEC_PATHS}}), encoding="utf-8")
    (checkout / ".env").write_text("DASHBOARD_TOKEN=unused\n", encoding="utf-8")
    (checkout / "config").mkdir()
    (checkout / "config" / "backends.yaml").write_text("default_backend: cli\n", encoding="utf-8")

    build = root / "cargo-target"
    (build / "release").mkdir(parents=True)
    world = World(root=root, checkout=checkout, build=build, home=root / "home",
                  instances_dir=root / "instances", spec=root / "spec.json")
    world.home.mkdir(parents=True)
    world.spec.write_text(json.dumps({
        "paths": SPEC_PATHS,
        "bots": [{"id": 1, "name": "main", "enabled": True, "live": True},
                 {"id": 2, "name": "spare", "enabled": False, "live": True}],
    }), encoding="utf-8")

    # The install folder a desktop app would have been launched from, holding an
    # OLDER build. A gate deploy must never write into it: the point of the
    # versioned folders is that the files a live process has open are not the
    # ones being replaced, and this folder is the one the app is launched from.
    world.install_dir.mkdir(parents=True)
    (world.install_dir / EXE).write_bytes(b"MZ the build from before")
    (world.install_dir / "bot").mkdir()
    (world.install_dir / "bot" / "old.py").write_text("OLD = True\n", encoding="utf-8")

    for name in _STRIPPED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(build))
    monkeypatch.setenv("ABP_HOME", str(world.home))
    monkeypatch.setenv("ABP_INSTANCES_DIR", str(world.instances_dir))
    monkeypatch.setenv("ABP_SANDBOX_NS_FILE", str(root / "sandbox-ns.json"))
    return world


@dataclass
class Gate:
    """A real `python -m abp_gate`, its cell, and the two ports it owns."""

    proc: subprocess.Popen
    cell: Cell

    def log_tail(self, world: World, lines: int = 30) -> str:
        try:
            text = (world.root / "gate.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no gate log)"
        return "\n".join(text.splitlines()[-lines:])


def _start_gate(world: World) -> Gate:
    world.public_port, world.control_port = _free_port(), _free_port()
    env = {k: v for k, v in os.environ.items()
           if k not in _STRIPPED and not k.startswith(("COV_CORE_", "COVERAGE_"))}  # never run instances traced
    env.update({
        "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
        "ABP_HOME": str(world.home), "ABP_INSTANCES_DIR": str(world.instances_dir),
        "ABP_GATE_PUBLIC_PORTS": str(world.public_port),
        "ABP_GATE_CONTROL_PORT": str(world.control_port),
        "TINY_SPEC": str(world.spec),
    })
    # One job object for the gate and, through it, every instance it starts: the
    # cell dies with the test even if the test dies first, and kill-on-close
    # means no orphaned interpreter can go on holding a port.
    cell = Cell(Policy(name="deploy-gate-test", max_processes=64),
                name=f"gate@{world.control_port}", owner="tests/test_deploy_gate_hot_swap.py")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "abp_gate"], cwd=str(ROOT), env=env,
        stdout=open(world.root / "gate.log", "ab"), stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
    )
    cell.admit(proc.pid)
    gate = Gate(proc=proc, cell=cell)
    try:
        _wait_http(f"http://127.0.0.1:{world.control_port}/healthz", until="200", timeout=120.0)
    except BaseException:
        # The gate never came up, so the reason it logged is the interesting
        # thing to report - not anything a cleanup assertion might say.
        with contextlib.suppress(Exception):
            _stop_gate(gate, world)
        raise AssertionError(f"the gate never came up; its log:\n{gate.log_tail(world, 60)}") from None
    return gate


def _stop_gate(gate: Gate, world: World) -> None:
    """Stop the gate the way a person would, then prove nothing survived it.

    The polite call needs the install's own DASHBOARD_TOKEN (the control API
    refuses every verb without one), so it is read out of the throwaway state
    root the stand-in instance wrote. The cell is closed afterwards whatever
    happened - it is the thing that makes "the test crashed" safe - and then
    every instance pid the registry knew about is asserted dead: a venv's
    python.exe is a launcher, so "the gate is gone" is not the same claim as
    "the interpreter it started is gone", and only the second one is the one
    that has to be true."""
    pids = []
    try:
        for info in world.registry().get("instances", {}).values():
            if info.get("pid"):
                pids.append(int(info["pid"]))
    except (OSError, ValueError):
        pass
    if gate.proc.poll() is None:
        try:
            httpx.post(f"http://127.0.0.1:{world.control_port}/api/gate/stop",
                       headers={"X-Dashboard-Token": _state_token(world)}, timeout=60.0)
        except httpx.HTTPError:
            pass
        for _ in range(100):
            if gate.proc.poll() is not None:
                break
            time.sleep(0.1)
    gate.cell.close()
    if gate.proc.poll() is None:
        gate.proc.kill()
        try:
            gate.proc.wait(15)
        except subprocess.TimeoutExpired:  # pragma: no cover - the cell already tried
            pass
    for _ in range(100):
        survivors = [pid for pid in pids if _alive(pid)]
        if not survivors:
            return
        time.sleep(0.1)
    raise AssertionError(f"the gate left instance processes running after the cell was closed: {survivors}")


def _state_token(world: World) -> str:
    for line in (world.home / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DASHBOARD_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


@pytest.fixture
def gate(world):
    """A real gate with a real (small) production instance behind it."""
    started = _start_gate(world)
    try:
        yield started
    finally:
        _stop_gate(started, world)


def _wait_http(url: str, *, until: str = "200", timeout: float = 60.0) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last = "never answered"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=3.0)
            if until == "any" or resp.status_code == 200:
                return resp
            last = f"{resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.2)
    raise AssertionError(f"{url} never answered ({until}) within {timeout}s: {last}")


# --------------------------------------------------------------------------- #
# A client that never stops asking, and counts every failure
# --------------------------------------------------------------------------- #
class Probe:
    """The thing a zero-downtime deploy has to be able to promise: a client
    asking the public port continuously, and not one failed request while the
    code underneath it is replaced."""

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
        self._thread.join(60)

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


def _deploy(world: World, commit: str, **kwargs) -> "deploy_local.DeployResult":
    _stage(world, commit=commit, broken=kwargs.pop("broken", False),
           paths_shown=kwargs.pop("paths_shown", 0))
    return deploy_local.deploy(world.checkout, do_build=False, port=world.public_port,
                               log=world.log, timeout_s=60.0, **kwargs)


# --------------------------------------------------------------------------- #
# The swap
# --------------------------------------------------------------------------- #
def test_a_deploy_behind_the_gate_installs_beside_the_running_code_and_swaps_to_it(gate, world):
    """Three deploys, one client asking the public port throughout: not one
    failed request, and every one of them the version it says it is.

    What is asserted, in order of what it is worth:
      * no failed request (the claim: a deploy is no longer an outage);
      * the code serving afterwards is the LAST bundle deployed, which is the
        claim that the swap actually happened rather than the deploy reporting
        success and changing nothing;
      * the app's own install folder - the one the desktop app is launched from,
        and the one holding the files a live process would have open - was never
        written to;
      * only the last N versioned folders are kept, the oldest one gone."""
    with Probe(f"{world.public}/healthz") as probe:
        results = [_deploy(world, commit=f"v{n}") for n in (1, 2, 3)]

    for n, result in enumerate(results, start=1):
        assert result.ok, (n, result.failures, "\n".join(world.lines))
        assert any(f"into release.v{n}" in step for step in result.ran), result.ran
        assert any(step.startswith("swapped to ") for step in result.ran), result.ran
        assert "verified" in result.ran, result.ran

    assert probe.requests > 30, "the probe barely ran - this test proved nothing"
    assert probe.failures == [], f"{len(probe.failures)} of {probe.requests} request(s) failed: {probe.failures[:5]}"
    assert world.served_commit() == "v3", "the last deploy is not what is serving"

    # Each swap really did replace the previous instance, and left nothing of it
    # running: the registry's active instance is v3's own code root.
    active = world.active()
    assert active["code_root"] == str(world.versioned()[-1]), active
    assert active["data_root"] == str(world.home), "a swap must keep the SAME state"
    running = {info["code_root"] for info in world.registry()["instances"].values()
               if info.get("pid") and _alive(info["pid"])}
    assert running == {str(world.versioned()[-1])}, \
        f"the replaced instances are still running out of their folders: {running}"

    # The app's own install folder is untouched: same exe, same bot/.
    assert (world.install_dir / EXE).read_bytes() == b"MZ the build from before"
    assert (world.install_dir / "bot" / "old.py").read_text(encoding="utf-8") == "OLD = True\n"
    assert any("the app binary itself changed" in note for note in results[0].notes), results[0].notes

    # KEEP_VERSIONS = 2: the version that is running, and the one it replaced.
    kept = world.versioned()
    assert [p.name for p in kept] == ["release.v2", "release.v3"], kept
    assert any("deleted release.v1" in line for line in world.lines), world.lines[-8:]


def test_a_broken_new_bundle_is_rolled_back_by_the_gate_and_the_old_version_keeps_serving(gate, world):
    """New code that cannot start must change nothing at all: the gate rolls the
    swap back itself, the version that was serving keeps serving, and the
    failure says which step failed and who is still up."""
    good = _deploy(world, commit="v1")
    assert good.ok, good.failures
    before = world.active()

    with Probe(f"{world.public}/healthz") as probe:
        bad = _deploy(world, commit="v2", broken=True)

    assert probe.failures == [], probe.failures[:5]
    assert not bad.ok
    assert "failed at step 1" in bad.failures[0], bad.failures
    assert "still serving" in bad.failures[0], bad.failures
    assert any("swap refused" in step for step in bad.ran), bad.ran
    assert "verified" not in bad.ran, "a deploy that never swapped cannot claim to have verified anything"

    # The gate really did leave the previous instance alone: same name, same pid,
    # same code root, and the port never went quiet.
    after = world.active()
    assert after["name"] == before["name"] and after["pid"] == before["pid"], (before, after)
    assert world.served_commit() == "v1", "a bundle that cannot start is now serving traffic"
    assert httpx.get(f"{world.public}/healthz", timeout=10.0).json()["status"] == "ok"

    # The broken version is on disk (the evidence, and what a person would look
    # at) but nothing is running from it, and it did not become the newest
    # retained version by being deployed.
    broken_dir = world.install_dir.with_name("release.v2")
    assert broken_dir.is_dir(), "the bundle that failed should still be there to look at"
    running = {info["code_root"] for info in world.registry()["instances"].values()
               if info.get("pid") and _alive(info["pid"])}
    assert str(broken_dir) not in running


def test_a_deploy_that_swaps_but_serves_the_wrong_api_goes_back_to_the_previous_version(gate, world):
    """The failure a swap cannot undo on its own: the new code boots, takes the
    lease, and only then turns out to serve an API this commit does not
    document. A swap stops what it replaced, so the gate has nothing to roll
    back to - but the folder that code came from is still on disk, and swapping
    back to it is the same move in the other direction.

    Without versioned folders there would be nothing to go back to at all, which
    is the reason this is the deploy's recovery path."""
    good = _deploy(world, commit="v1")
    assert good.ok, good.failures
    with Probe(f"{world.public}/healthz") as probe:
        wrong = _deploy(world, commit="v2", paths_shown=1)

    assert probe.failures == [], probe.failures[:5]
    assert not wrong.ok
    assert any("not this commit's API" in failure for failure in wrong.failures), wrong.failures
    assert any("swapped back to" in step for step in wrong.ran), wrong.ran
    assert any("the deploy was undone" in note for note in wrong.notes), wrong.notes

    # v1 is serving again, and the deployment that broke is not.
    assert world.served_commit() == "v1", "the failed deploy is still the code in front of traffic"
    active = world.active()
    assert active["code_root"].endswith("release.v1"), active
    running = {info["code_root"] for info in world.registry()["instances"].values()
               if info.get("pid") and _alive(info["pid"])}
    assert running == {str(world.install_dir.with_name("release.v1"))}, running


def test_a_dry_run_through_the_gate_prints_the_swap_and_installs_nothing(gate, world):
    """The dry run has to be readable before anybody commits to it: what it would
    install, where, that it would not stop anything, and that it would keep two
    versions. And it must touch nothing at all - not even a new folder."""
    _deploy(world, commit="v1")
    before = {p.name for p in world.install_dir.parent.iterdir()}
    world.lines.clear()
    result = deploy_local.deploy(world.checkout, do_build=False, dry_run=True, port=world.public_port,
                                 log=world.log, timeout_s=30.0)
    assert result.ok, result.failures
    printed = "\n".join(world.lines)
    assert "a gate owns" in printed and "this is a hot swap, not a restart" in printed
    assert "release.v2" in printed and "beside the running instance rather than over it" in printed
    assert "would ask the gate to swap to" in printed
    assert "would verify" in printed and "docs/api/openapi.json" in printed
    assert "keep the last 2 versioned folder" in printed
    assert "would stop" not in printed, "a hot swap stops nothing - that is the whole difference"
    assert {p.name for p in world.install_dir.parent.iterdir()} == before
    assert not world.install_dir.with_name("release.v2").exists()


def _alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
