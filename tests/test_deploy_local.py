"""scripts/deploy_local.py — the deploy that actually deploys the current code.

A running release build executes the Python bundled next to its own exe, so
a rebuild that lands in CARGO_TARGET_DIR while the app is launched from the
checkout's target/release installs nothing at all: every restart reloaded
the same old files. These tests pin the fix down end to end, against real
files and a real process — the install/rollback logic in a temp folder, and
a tiny HTTP server standing in for ABP so the restart and every verification
request are genuine.

Nothing here touches the real install, the real checkout's target/, or any
running ABP: every folder is a throwaway under tmp_path, and the only
process started is the fake server, which the fixture kills with its whole
tree at teardown.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("deploy_local", _SCRIPTS / "deploy_local.py")
deploy_local = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
sys.modules["deploy_local"] = deploy_local
_SPEC.loader.exec_module(deploy_local)

import release_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXE = deploy_local.EXE_NAME

# The fake tauri.conf.json every temp-checkout test builds its bundle from.
# Deliberately the same SHAPES the real one has: a staged directory, a
# staged single file, and a destination that is the install's own state.
FAKE_RESOURCES = {
    "stage/bot": "bot",
    "stage/.abp_build.json": ".abp_build.json",
    "stage/config/backends.yaml": "config/backends.yaml",
    "stage/.venv": ".venv",
    "../../requirements.txt": "requirements.txt",
}
FAKE_SPEC_PATHS = ["/healthz", "/openapi.json", "/api/bots"]


# --------------------------------------------------------------------------- #
# Fixtures: a throwaway checkout, a throwaway build, and a real fake ABP
# --------------------------------------------------------------------------- #
def _write_bundle(build: Path, marker: str) -> None:
    """A build directory that looks like what cargo/tauri leaves behind:
    each bundle.resources DESTINATION, plus the app binary."""
    prof = build / "release"
    for rel in FAKE_RESOURCES.values():
        p = prof / rel
        if rel in ("bot", ".venv"):
            (p / "nested").mkdir(parents=True, exist_ok=True)
            (p / "__init__.py").write_text(f"# {marker}\n", encoding="utf-8")
            (p / "nested" / "deep.py").write_text(f"DEEP = {marker!r}\n", encoding="utf-8")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{marker}\n", encoding="utf-8")
    (prof / EXE).write_bytes(b"MZ not really an exe " + marker.encode())
    (prof / ".venv" / "site-packages" / "cryptography").mkdir(parents=True, exist_ok=True)
    (prof / ".venv" / "site-packages" / "cryptography" / "_rust.pyd").write_bytes(b"\0" * 16)


@pytest.fixture
def checkout(tmp_path):
    """A minimal ABP checkout: enough for the deploy to recognise it as one
    (bot/main.py + tauri.conf.json), to find its spec, and to map a
    target/release install back to it the way bot/envfile.py does."""
    root = tmp_path / "checkout"
    desk = root / "desktop-app" / "src-tauri"
    (root / "bot").mkdir(parents=True)
    (root / "bot" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "backends.yaml").write_text("default_backend: cli\n", encoding="utf-8")
    (root / "docs" / "api").mkdir(parents=True)
    (root / "docs" / "api" / "openapi.json").write_text(
        json.dumps({"openapi": "3.1.0", "paths": {p: {} for p in FAKE_SPEC_PATHS}}), encoding="utf-8")
    desk.mkdir(parents=True)
    (desk / "tauri.conf.json").write_text(
        json.dumps({"bundle": {"resources": FAKE_RESOURCES}}), encoding="utf-8")
    # The state root the app would read while running from this install: a
    # deploy must never put a build's copy over it.
    (root / ".env").write_text("DASHBOARD_TOKEN=unused\n", encoding="utf-8")
    return root


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A finished build, and CARGO_TARGET_DIR pointing at it — the exact
    situation this script exists for."""
    out = tmp_path / "cargo-target"
    _write_bundle(out, "v1")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(out))
    monkeypatch.delenv("ABP_HOME", raising=False)
    return out


def _install_dir(root: Path) -> Path:
    return root / "desktop-app" / "src-tauri" / "target" / "release"


# A real HTTP server, standing in for ABP: /healthz, /openapi.json and
# /api/bots (with the token header the real route requires), driven by a
# JSON spec file so a 770-path spec never has to fit in an environment
# variable.
_FAKE_ABP = textwrap.dedent('''
    import json, os, time, http.server

    spec = json.load(open(os.environ["FAKE_SPEC"], encoding="utf-8"))
    started = time.monotonic()
    calls = {"bots": 0}

    def live(row):
        return bool(row.get("live")) and (time.monotonic() - started) >= spec.get("bots_delay", 0.0)

    class H(http.server.BaseHTTPRequestHandler):
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
                if spec.get("health") == "down":
                    return self._send(503, {"status": "degraded", "db_ok": False})
                return self._send(200, {"status": "ok", "db_ok": True, "bundle": spec.get("bundle", {})})
            if route == "/openapi.json":
                paths = spec["paths"] if spec.get("full_spec", True) else spec["paths"][:1]
                return self._send(200, {"openapi": "3.1.0", "paths": {p: {} for p in paths}})
            if route == "/api/bots":
                expected = spec.get("token")
                if expected is not None and self.headers.get("X-Dashboard-Token") != expected:
                    return self._send(401, {"detail": "invalid dashboard token"})
                calls["bots"] += 1
                if calls["bots"] in spec.get("bots_error_calls", ()):    # a starting app: 503 with an error body
                    return self._send(503, {"detail": "starting"})
                return self._send(200, [dict(row, live_running=live(row)) for row in spec["bots"]])
            return self._send(404, {"detail": "not found"})

        def log_message(self, *a):
            pass

    http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["DASHBOARD_PORT"])), H).serve_forever()
''')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeAbp:
    """A real process, on a real port, answering like ABP. Killed with its
    whole tree at teardown - never left running."""

    def __init__(self, proc: subprocess.Popen, port: int):
        self.proc = proc
        self.port = port

    def stop(self) -> None:
        try:
            release_guard.stop_processes([psutil.Process(self.proc.pid)], grace=5)
        except psutil.Error:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("the fake ABP server did not die")


@pytest.fixture
def fake_abp(tmp_path):
    """Starts fake ABPs; every one of them is dead by the time the test is."""
    script = tmp_path / "fake_abp.py"
    script.write_text(_FAKE_ABP, encoding="utf-8")
    started: list[FakeAbp] = []

    def start(spec: dict) -> FakeAbp:
        port = _free_port()
        spec_file = tmp_path / f"spec-{port}.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        env = {**os.environ, "FAKE_SPEC": str(spec_file), "DASHBOARD_PORT": str(port)}
        proc = deploy_local._popen([sys.executable, str(script)], env=env, cwd=str(tmp_path),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"the fake ABP server exited immediately (code {proc.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            proc.kill()
            raise AssertionError("the fake ABP server never started listening")
        server = FakeAbp(proc, port)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def _healthy_spec(**overrides) -> dict:
    spec = {
        "paths": FAKE_SPEC_PATHS,
        "bots": [{"id": 1, "name": "main", "enabled": True, "live": True},
                 {"id": 2, "name": "spare", "enabled": False, "live": False}],
    }
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------- #
# The resource list comes from tauri.conf.json, never from this script
# --------------------------------------------------------------------------- #
def test_the_real_tauri_conf_lists_every_resource_and_nothing_in_here_decides_what_ships(tmp_path):
    """The list is read, never written here: a resource added to
    tauri.conf.json is deployed without touching deploy_local.py at all."""
    resources = deploy_local.read_resources(ROOT)
    assert resources["stage/bot"] == "bot" and resources["stage/.venv"] == ".venv"
    assert resources["stage/.abp_build.json"] == ".abp_build.json"
    assert resources["stage/config/backends.yaml"] == "config/backends.yaml"

    added = dict(resources, **{"stage/something_new": "something_new"})
    conf = tmp_path / "desktop-app" / "src-tauri"
    conf.mkdir(parents=True)
    (conf / "tauri.conf.json").write_text(json.dumps({"bundle": {"resources": added}}), encoding="utf-8")
    build_dir = tmp_path / "cargo-target"
    monkey_free = {**os.environ, "CARGO_TARGET_DIR": str(build_dir)}
    prof = build_dir / "release"
    for dest in added.values():
        p = prof / deploy_local.as_dest(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    (prof / EXE).write_bytes(b"MZ")
    plan = deploy_local.plan_install(tmp_path, prof, tmp_path / "install", environ=monkey_free)
    assert "something_new" in {r.rel for r in plan.install}, "a resource this script has never heard of must be installed"


def test_the_real_resources_all_land_in_the_install_folder(tmp_path, monkeypatch):
    build_dir = tmp_path / "cargo-target"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(build_dir))
    monkeypatch.delenv("ABP_HOME", raising=False)
    prof = build_dir / "release"
    for dest in deploy_local.read_resources(ROOT).values():
        p = prof / deploy_local.as_dest(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    (prof / EXE).write_bytes(b"MZ")

    plan = deploy_local.plan_install(ROOT, prof, _install_dir(ROOT))
    installed = {r.rel for r in plan.install}
    assert plan.ok
    for expected in ("bot", ".venv", ".abp_build.json", "requirements.lock", "icon.ico", "desktop-app/ui", EXE):
        assert expected in installed
    # The one real resource that is NOT installed into the checkout's own
    # target/release is the one the running app reads from the checkout.
    assert [rel for rel, _why in plan.skipped] == ["config/backends.yaml"]
    # And each one is read from the BUILD directory, not from the checkout.
    assert all(str(r.source).startswith(str(prof)) for r in plan.install)


def test_a_leading_dot_is_only_stripped_from_an_explicit_dot_slash():
    """str.lstrip("./") would turn the .venv resource into "venv"."""
    assert deploy_local.as_dest(".venv") == ".venv"
    assert deploy_local.as_dest("./config/backends.yaml") == "config/backends.yaml"
    assert deploy_local.as_dest("desktop-app/ui") == "desktop-app/ui"


def test_a_resource_destination_escaping_the_install_folder_is_refused(checkout, build):
    (checkout / "desktop-app" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"bundle": {"resources": {"stage/bot": "../../elsewhere"}}}), encoding="utf-8")
    with pytest.raises(deploy_local.DeployError, match="outside the install folder"):
        deploy_local.plan_install(checkout, build / "release", _install_dir(checkout))


def test_a_list_shaped_resources_block_still_plans(tmp_path):
    """Tauri also allows a plain list, where each resource keeps its own
    name in the bundle root."""
    src = tmp_path / "desktop-app" / "src-tauri"
    src.mkdir(parents=True)
    (src / "tauri.conf.json").write_text(
        json.dumps({"bundle": {"resources": ["stage/bot", "stage/config/backends.yaml"]}}), encoding="utf-8")
    assert deploy_local.read_resources(tmp_path) == {"stage/bot": "bot", "stage/config/backends.yaml": "backends.yaml"}


# --------------------------------------------------------------------------- #
# Never touch an install's own state
# --------------------------------------------------------------------------- #
def test_a_checkout_shaped_install_never_gets_the_bundles_own_config(checkout, build):
    """Running from <checkout>/desktop-app/src-tauri/target/release makes
    bot/envfile.py resolve the state root to the CHECKOUT, so the app reads
    the checkout's live config/backends.yaml and the bundled copy is dead
    weight. Installing it would overwrite the person's own settings for
    nothing."""
    install = _install_dir(checkout)
    plan = deploy_local.plan_install(checkout, build / "release", install)
    assert ("config/backends.yaml",
            f"the app reads the live copy at {checkout / 'config' / 'backends.yaml'}") in plan.skipped
    assert "config/backends.yaml" not in {r.rel for r in plan.install}
    assert deploy_local.state_root(install) == checkout.resolve()
    assert deploy_local.state_root(install, {"ABP_HOME": str(checkout / "elsewhere")}) == (checkout / "elsewhere")


def test_a_plain_install_folder_is_its_own_state_root_so_config_does_land(tmp_path):
    """The other side of the same rule: a real install outside a checkout
    has no live config to protect, and would not start without it."""
    root = tmp_path / "install-root"
    (root / "bot").mkdir(parents=True)
    (root / "bot" / "main.py").write_text("", encoding="utf-8")
    desk = root / "desktop-app" / "src-tauri"
    desk.mkdir(parents=True)
    (desk / "tauri.conf.json").write_text(json.dumps({"bundle": {"resources": FAKE_RESOURCES}}), encoding="utf-8")
    prof = tmp_path / "prof"
    _write_bundle(tmp_path / "b", "v1")
    shutil.copytree(tmp_path / "b" / "release", prof)
    install = root / "wherever"
    plan = deploy_local.plan_install(root, prof, install)
    assert "config/backends.yaml" in {r.rel for r in plan.install}
    assert deploy_local.state_root(install) == install.resolve()


def test_env_data_and_logs_are_never_installed_even_as_an_install_s_own_root(tmp_path, checkout, build):
    conf = dict(FAKE_RESOURCES)
    conf.update({"stage/.env": ".env", "stage/data": "data", "stage/logs": "logs"})
    (checkout / "desktop-app" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"bundle": {"resources": conf}}), encoding="utf-8")
    prof = build / "release"
    for rel in (".env", "data", "logs"):
        (prof / rel).parent.mkdir(parents=True, exist_ok=True)
        (prof / rel).write_text("secret" if rel == ".env" else "x", encoding="utf-8")
    install = tmp_path / "bare-install"
    plan = deploy_local.plan_install(checkout, prof, install)
    skipped = {rel for rel, _why in plan.skipped}
    assert {".env", "data", "logs"} <= skipped
    assert not {".env", "data", "logs"} & {r.rel for r in plan.install}


# --------------------------------------------------------------------------- #
# Where it may install
# --------------------------------------------------------------------------- #
def test_a_target_outside_every_checkout_is_refused_until_asked_for(tmp_path, checkout, build):
    outside = tmp_path / "someones-install"
    outside.mkdir()
    refusal = deploy_local.check_install_dir(outside)
    assert refusal and "not inside a checkout" in refusal and "--outside-checkout" in refusal
    assert deploy_local.check_install_dir(outside, outside_ok=True) is None
    with pytest.raises(deploy_local.DeployError, match="not inside a checkout"):
        deploy_local.deploy(checkout, install_dir=str(outside), do_build=False, port=_free_port())


def test_a_target_inside_the_checkout_needs_no_permission(tmp_path, checkout, build):
    assert deploy_local.check_install_dir(_install_dir(checkout)) is None
    assert deploy_local.check_install_dir(tmp_path / "checkout" / "desktop-app" / "src-tauri" / "target") is None


def test_the_install_dir_can_come_from_the_environment(checkout, monkeypatch):
    monkeypatch.setenv("ABP_INSTALL_DIR", str(checkout / "somewhere-else"))
    assert deploy_local.resolve_install_dir(checkout) == (checkout / "somewhere-else").resolve()
    assert deploy_local.resolve_install_dir(checkout, "d:/explicit") == Path("d:/explicit").resolve()
    monkeypatch.delenv("ABP_INSTALL_DIR")
    assert deploy_local.resolve_install_dir(checkout) == _install_dir(checkout).resolve()


# --------------------------------------------------------------------------- #
# Install and roll back, in a temp folder with real files
# --------------------------------------------------------------------------- #
def test_installing_replaces_every_file_and_folder_and_keeps_exactly_one_previous_copy(checkout, build):
    install = _install_dir(checkout)   # inside the checkout, as the real one is
    deploy_local.install_resources(deploy_local.plan_install(checkout, build / "release", install),
                                   install, log=lambda _s: None)
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v1'\n"
    assert (install / EXE).exists() and (install / ".venv" / "site-packages" / "cryptography" / "_rust.pyd").exists()

    _write_bundle(build, "v2")
    prev = deploy_local.install_resources(deploy_local.plan_install(checkout, build / "release", install),
                                          install, log=lambda _s: None)
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v2'\n"
    assert prev.name.endswith(deploy_local.PREVIOUS_SUFFIX)
    assert (prev / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v1'\n"

    _write_bundle(build, "v3")   # a third deploy must not accumulate copies
    prev = deploy_local.install_resources(deploy_local.plan_install(checkout, build / "release", install),
                                          install, log=lambda _s: None)
    assert (prev / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v2'\n"
    assert not list(prev.rglob("*v1*"))


def test_rolling_back_puts_the_previous_files_back(tmp_path):
    """Two real deploys against the same temp install folder: the second
    one's files go back to being the first one's, and anything the second
    one created for the first time is removed again."""
    root = tmp_path / "root"
    (root / "desktop-app" / "src-tauri").mkdir(parents=True)
    (root / "bot").mkdir()
    (root / "bot" / "main.py").write_text("", encoding="utf-8")
    (root / "desktop-app" / "src-tauri" / "tauri.conf.json").write_text(json.dumps({"bundle": {"resources": {
        "stage/bot": "bot", "stage/.abp_build.json": ".abp_build.json", "../../requirements.txt": "requirements.txt"}}}),
        encoding="utf-8")
    install = tmp_path / "install"
    prof = tmp_path / "prof"

    _write_bundle(tmp_path / "old", "old")
    (tmp_path / "old" / "release" / ".abp_build.json").unlink()   # not in the first build at all
    shutil.copytree(tmp_path / "old" / "release", prof)
    deploy_local.install_resources(deploy_local.plan_install(root, prof, install), install, log=lambda _s: None)
    assert (install / "bot" / "__init__.py").read_text(encoding="utf-8") == "# old\n"
    assert not (install / ".abp_build.json").exists()

    _write_bundle(tmp_path / "new", "new")
    shutil.rmtree(prof)
    shutil.copytree(tmp_path / "new" / "release", prof)
    deploy_local.install_resources(deploy_local.plan_install(root, prof, install), install, log=lambda _s: None)
    assert (install / "bot" / "__init__.py").read_text(encoding="utf-8") == "# new\n"
    assert (install / ".abp_build.json").exists(), "this build is the first to ship a build stamp"

    restored = deploy_local.rollback(install, log=lambda _s: None)
    assert (install / "bot" / "__init__.py").read_text(encoding="utf-8") == "# old\n"
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'old'\n"
    assert (install / "requirements.txt").read_text(encoding="utf-8") == "old\n"
    assert (install / EXE).read_bytes() == b"MZ not really an exe old"
    assert not (install / ".abp_build.json").exists(), "a resource that did not exist before must be removed again"
    assert {"bot", "requirements.txt", EXE} <= set(restored)


def test_an_install_that_dies_half_way_is_still_rollback_able(tmp_path):
    """The manifest is written before each copy, not once at the end: a copy
    that fails on resource five must not leave the install folder in a state
    nothing can put back."""
    root = tmp_path / "root"
    (root / "desktop-app" / "src-tauri").mkdir(parents=True)
    (root / "bot").mkdir()
    (root / "bot" / "main.py").write_text("", encoding="utf-8")
    (root / "desktop-app" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"bundle": {"resources": {"stage/bot": "bot", "../../requirements.txt": "requirements.txt"}}}),
        encoding="utf-8")
    install = tmp_path / "install"
    prof = tmp_path / "prof"
    _write_bundle(tmp_path / "old", "old")
    shutil.copytree(tmp_path / "old" / "release", prof)
    deploy_local.install_resources(deploy_local.plan_install(root, prof, install), install, log=lambda _s: None)

    # The second build's bot/ cannot be copied (it vanished mid-deploy).
    _write_bundle(tmp_path / "new", "new")
    shutil.rmtree(prof)
    shutil.copytree(tmp_path / "new" / "release", prof)
    plan = deploy_local.plan_install(root, prof, install)
    broken = [r for r in plan.install if r.rel == "bot"][0]
    original_copytree = deploy_local.shutil.copytree

    def copytree(src, dst, *a, **kw):
        if Path(dst) == broken.dest:
            raise OSError("the copy failed half way through")
        return original_copytree(src, dst, *a, **kw)

    deploy_local.shutil.copytree = copytree
    try:
        with pytest.raises(OSError, match="half way"):
            deploy_local.install_resources(plan, install, log=lambda _s: None)
    finally:
        deploy_local.shutil.copytree = original_copytree

    deploy_local.rollback(install, log=lambda _s: None)
    assert (install / "bot" / "__init__.py").read_text(encoding="utf-8") == "# old\n", \
        "the half-copied folder must be put back to what was there before"
    assert (install / "requirements.txt").read_text(encoding="utf-8") == "old\n"


def test_rolling_back_with_nothing_to_roll_back_to_says_so(tmp_path):
    with pytest.raises(deploy_local.DeployError, match="no usable previous copy"):
        deploy_local.rollback(tmp_path / "install", log=lambda _s: None)


def test_a_dry_run_changes_nothing(checkout, build):
    install = _install_dir(checkout)
    before = sorted(p.name for p in install.iterdir()) if install.exists() else None
    lines: list[str] = []
    result = deploy_local.deploy(checkout, dry_run=True, do_build=False, port=_free_port(), log=lines.append)
    assert result.ok
    after = sorted(p.name for p in install.iterdir()) if install.exists() else None
    assert after == before
    assert not deploy_local.previous_dir(install).exists()
    printed = "\n".join(lines)
    assert "would install" in printed and EXE in printed
    assert "would verify" in printed and "docs/api/openapi.json" in printed
    assert "would roll back" in printed
    assert "config/backends.yaml" in printed  # and why it is skipped


def test_a_dry_run_on_a_machine_that_was_never_built_still_prints_the_whole_plan(checkout, monkeypatch):
    """An empty build directory is not a failed deploy - the build would
    have produced those files. The dry run says so and carries on."""
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp := checkout.parent / "empty-target"))
    lines: list[str] = []
    result = deploy_local.deploy(checkout, dry_run=True, do_build=False, port=_free_port(), log=lines.append)
    assert result.ok, result.failures
    printed = "\n".join(lines)
    assert "do not exist yet and the build would produce them" in printed
    assert printed.count("would install") >= 5
    assert not (tmp).exists(), "a dry run must not create anything"


def test_a_dry_run_reports_the_outside_checkout_refusal_instead_of_hiding_it(tmp_path, checkout, build, capsys):
    outside = tmp_path / "an-install"
    outside.mkdir()
    result = deploy_local.deploy(checkout, install_dir=str(outside), dry_run=True, do_build=False,
                                 port=_free_port(), log=print)
    assert result.ok
    assert "would stop here" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The build: reuse when it is up to date
# --------------------------------------------------------------------------- #
def test_an_up_to_date_build_is_reused_instead_of_rebuilt(checkout, build, monkeypatch):
    future = time.time() + 120
    prof = build / "release"
    os.utime(prof / EXE, (future, future))     # the build is newer than every input
    assert deploy_local.build_is_current(checkout) is True
    os.utime(checkout / "bot" / "main.py", (future + 60, future + 60))   # a source edit is newer still
    assert deploy_local.build_is_current(checkout) is False

    ran: list[str] = []
    monkeypatch.setattr(deploy_local, "build_is_current", lambda *_a, **_k: False)
    monkeypatch.setattr(release_guard, "run_cmd", lambda cmd, **_k: ran.append(cmd) or release_guard.CmdResult(0, ""))
    monkeypatch.setattr(deploy_local.shutil, "which", lambda _name: "/usr/bin/cargo")
    assert deploy_local.build(checkout) is True
    assert ran == [["cargo", "tauri", "build"]]


def test_a_build_missing_a_resource_is_never_called_current(checkout, build):
    future = time.time() + 120
    os.utime(build / "release" / EXE, (future, future))
    assert deploy_local.build_is_current(checkout) is True
    shutil.rmtree(build / "release" / "bot")
    assert deploy_local.build_is_current(checkout) is False


def test_builds_need_cargo(checkout, build, monkeypatch):
    monkeypatch.setattr(deploy_local.shutil, "which", lambda _name: None)
    with pytest.raises(deploy_local.DeployError, match="cargo is not on PATH"):
        deploy_local.build(checkout, force=True)


# --------------------------------------------------------------------------- #
# Restarting: the way the user's session would, never with a console window
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "win32", reason="asserts the Windows launch command; it must not run it")
def test_on_windows_the_app_is_started_through_explorer_so_it_gets_the_users_environment():
    """The command is asserted, not run: launching the real GUI binary (or
    explorer.exe) from a test is not this test's business."""
    exe = Path("D:/somewhere/agentic-bot-platform.exe")
    assert deploy_local.launch_command(exe) == ["explorer.exe", str(exe)]


@pytest.mark.skipif(sys.platform != "win32", reason="asserts Windows creation flags")
def test_every_spawn_from_here_uses_a_hidden_console_and_never_a_detached_one():
    """A console-less child (DETACHED_PROCESS) makes the first console
    program it starts allocate and SHOW a window - so the flags are the
    thing to assert, not the absence of a visible one."""
    from bot.sandbox_ns import guard

    flags = deploy_local._spawn_flags()
    assert flags == guard.CREATE_NO_WINDOW
    assert not flags & guard.DETACHED_PROCESS
    assert not guard.rewrite_flags(flags) & guard.DETACHED_PROCESS


@pytest.mark.skipif(sys.platform != "win32", reason="needs a process whose exe path is inside the install folder")
def test_stopping_finds_and_kills_what_runs_out_of_the_install_folder(tmp_path):
    """Real processes: the app, and the child it leaves behind. Both must be
    gone — the orphan is what pins the bundled venv's .pyd files, and
    matching on the executable's own path is the only way to find it."""
    install = tmp_path / "install"
    install.mkdir()
    child_pid_file = tmp_path / "child_pid.txt"
    app = install / EXE
    # The BASE interpreter, not a venv's launcher stub: a copied stub can't
    # find the venv it belongs to and exits before running anything.
    base = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python")
    shutil.copy2(base if base.is_file() else Path(sys.executable), app)
    script = ("import subprocess, sys, time\n"
              f"open(r'{child_pid_file}', 'w').write(str(subprocess.Popen("
              "[sys.executable, '-c', 'import time; time.sleep(120)']).pid))\n"
              "time.sleep(120)\n")
    proc = deploy_local._popen([str(app), "-c", script], cwd=str(tmp_path))
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not child_pid_file.exists():
            assert proc.poll() is None, "the copied interpreter exited instead of running"
            time.sleep(0.2)
        assert child_pid_file.exists(), "the copied interpreter never ran"
        assert proc.pid in {p.pid for p in deploy_local.install_processes(install)}

        stopped = deploy_local.stop_app(install, log=lambda _s: None)
        time.sleep(1)
        assert any(str(proc.pid) in name for name in stopped), stopped
        alive = psutil.pid_exists(proc.pid) and psutil.Process(proc.pid).status() != psutil.STATUS_ZOMBIE
        assert not alive, "the app process is still running after stop_app"
        orphan = int(child_pid_file.read_text().strip())
        if psutil.pid_exists(orphan) and psutil.Process(orphan).status() != psutil.STATUS_ZOMBIE:
            assert orphan in {p.pid for p in deploy_local.install_processes(install)}, \
                "an orphaned backend was left behind holding the bundled venv open"
            release_guard.stop_processes([psutil.Process(orphan)])
    finally:
        for pid in [proc.pid, int(child_pid_file.read_text().strip()) if child_pid_file.exists() else 0]:
            if pid and psutil.pid_exists(pid):
                try:
                    release_guard.stop_processes([psutil.Process(pid)])
                except psutil.Error:
                    pass
    assert deploy_local.install_processes(install) == []


def test_a_dry_run_start_never_needs_the_exe_to_exist(tmp_path):
    lines: list[str] = []
    assert deploy_local.start_app(tmp_path / "not-installed.exe", dry_run=True, log=lines.append) is None
    assert "not-installed.exe" in lines[0]


# --------------------------------------------------------------------------- #
# Verification, against a real HTTP server standing in for ABP
# --------------------------------------------------------------------------- #
def test_verify_accepts_a_healthy_app_serving_this_commits_api(checkout, fake_abp):
    server = fake_abp(_healthy_spec(bundle={"commit": "abc12345", "checkout_commit": "abc12345", "stale": False}))
    lines: list[str] = []
    result = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, timeout_s=30, log=lines.append)
    assert result.ok, result.failures
    assert "health ok" in "\n".join(lines)
    assert "matches docs/api/openapi.json (3 paths)" in "\n".join(lines)
    assert "1 enabled bot instance(s) are live again" in "\n".join(lines)


def test_verify_waits_for_a_bot_instance_that_is_still_coming_back(checkout, fake_abp):
    server = fake_abp(_healthy_spec(bots_delay=3.0))
    started = time.monotonic()
    result = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, timeout_s=60,
                                 log=lambda _s: None)
    assert result.ok, result.failures
    assert time.monotonic() - started >= 3.0, "it did not actually wait for the instance"


def test_verify_keeps_polling_through_an_error_body_while_the_bots_come_back(checkout, fake_abp):
    """The real first deploy: /api/bots answered a list, then a 503 error body while the instances started,
    and verify iterated that body (AttributeError) instead of polling again."""
    server = fake_abp(_healthy_spec(bots_delay=4.0, bots_error_calls=[2, 3]))
    result = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, timeout_s=60,
                                 log=lambda _s: None)
    assert result.ok, result.failures


def test_verify_fails_when_an_enabled_bot_never_comes_back(checkout, fake_abp):
    server = fake_abp(_healthy_spec(bots=[{"id": 1, "name": "main", "enabled": True, "live": False}]))
    result = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, timeout_s=4,
                                 log=lambda _s: None)
    assert not result.ok
    assert "did not come back" in result.failures[0] and "main" in result.failures[0]


def test_verify_fails_when_the_served_api_is_not_this_commits(checkout, fake_abp):
    """The failure this whole task is about, seen from the other side: the
    app is up, healthy, and serving an API that is not this commit's."""
    server = fake_abp(_healthy_spec(full_spec=False))
    result = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, timeout_s=10,
                                 log=lambda _s: None)
    assert not result.ok
    assert "not this commit's API" in result.failures[0]
    assert "/api/bots" in result.failures[0]


def test_verify_fails_when_the_app_never_becomes_healthy(checkout, fake_abp):
    server = fake_abp(_healthy_spec(health="down"))
    result = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, timeout_s=4,
                                 log=lambda _s: None)
    assert not result.ok and "never became healthy" in result.failures[0]


def test_verify_reports_a_stale_bundle_even_though_it_passes(checkout, fake_abp):
    """A stale bundle is not a rollback trigger - rolling back would restore
    the same stale files - but it must never be silent."""
    server = fake_abp(_healthy_spec(bundle={"commit": "old11111", "checkout_commit": "new22222", "stale": True}))
    result = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, timeout_s=10,
                                 log=lambda _s: None)
    assert result.ok, result.failures
    assert any("STALE BUNDLE" in note and "old11111" in note for note in result.notes)


def test_verify_reports_an_authentication_failure_rather_than_passing_silently(checkout, fake_abp):
    server = fake_abp(_healthy_spec(token="unused"))
    assert deploy_local.verify(_install_dir(checkout), server.port, root=checkout, token="unused",
                               timeout_s=10, log=lambda _s: None).ok
    bad = deploy_local.verify(_install_dir(checkout), server.port, root=checkout, token="wrong",
                              timeout_s=10, log=lambda _s: None)
    assert not bad.ok and "could not authenticate" in bad.failures[0]


# --------------------------------------------------------------------------- #
# The whole deploy, end to end, against the real code paths
# --------------------------------------------------------------------------- #
def test_a_full_deploy_installs_starts_and_verifies(checkout, build, fake_abp):
    """stop -> install -> start (a real process) -> verify (real HTTP)."""
    server = fake_abp(_healthy_spec(bundle={"commit": "abc12345", "checkout_commit": "abc12345", "stale": False}))
    install = _install_dir(checkout)
    started: list[Path] = []
    result = deploy_local.deploy(
        checkout, do_build=False, port=server.port, log=lambda _s: None,
        launcher=lambda exe: started.append(exe) or fake_abp_process_started(exe, server),
    )
    assert result.ok, result.failures
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v1'\n"
    assert started == [install / EXE]
    assert "verified" in result.ran


def test_a_failed_verification_rolls_the_previous_files_back_and_starts_them(checkout, build, fake_abp):
    """Install v1, then a v2 whose app serves the wrong API: the deploy must
    fail, put v1 back, and start that."""
    good = fake_abp(_healthy_spec())
    install = _install_dir(checkout)
    assert deploy_local.deploy(checkout, do_build=False, port=good.port, log=lambda _s: None,
                               launcher=lambda _exe: None).ok
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v1'\n"
    good.stop()

    bad = fake_abp(_healthy_spec(full_spec=False))
    _write_bundle(build, "v2")
    started: list[Path] = []
    result = deploy_local.deploy(checkout, do_build=False, port=bad.port, log=lambda _s: None,
                                 launcher=lambda exe: started.append(exe) or None)
    assert not result.ok
    assert any("not this commit's API" in f for f in result.failures)
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v1'\n"
    assert "rolled back" in result.ran
    # Once to install and verify the failed v2, once to bring the rolled-back copy back up.
    assert len(started) == 2


def test_no_rollback_is_possible_when_the_build_is_incomplete(checkout, build, fake_abp):
    (build / "release" / "bot" / "__init__.py").unlink()
    shutil.rmtree(build / "release" / ".venv")
    result = deploy_local.deploy(checkout, do_build=False, port=_free_port(), log=lambda _s: None,
                                 launcher=lambda _exe: None)
    assert not result.ok
    assert "missing" in result.failures[0] and "--no-build" in result.failures[0]
    assert not _install_dir(checkout).exists(), "nothing may be installed from an incomplete build"


def test_nothing_was_running_so_the_app_is_installed_but_not_started(checkout, build):
    """The next launch picks this commit up. Starting a GUI app the person
    deliberately closed is not a deploy's decision — but not verifying that
    was possible must be said out loud."""
    install = _install_dir(checkout)
    result = deploy_local.deploy(checkout, do_build=False, start=False, port=_free_port(), log=lambda _s: None,
                                 launcher=lambda _exe: pytest.fail("must not start anything"))
    assert result.ok, result.failures
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v1'\n"
    assert result.ran and result.ran[0].startswith("installed") and "restarted" not in " ".join(result.ran)
    assert any("unverified by necessity" in note for note in result.notes)


def test_a_registered_service_is_restarted_even_when_nothing_was_running(checkout, build, fake_abp):
    server = fake_abp(_healthy_spec())
    restarts: list[int] = []

    def restart():
        restarts.append(1)

    result = deploy_local.deploy(checkout, do_build=False, start=False, port=server.port, restart=restart,
                                 log=lambda _s: None, launcher=lambda _exe: pytest.fail("must not launch the exe"))
    assert result.ok, result.failures
    assert restarts == [1]
    assert "verified" in result.ran


def test_deploy_uses_the_registered_service_restart_when_there_is_one(checkout, build, fake_abp):
    server = fake_abp(_healthy_spec())
    restarts: list[int] = []

    def restart():
        restarts.append(1)

    result = deploy_local.deploy(checkout, do_build=False, port=server.port, restart=restart,
                                 log=lambda _s: None, launcher=lambda _exe: pytest.fail("must not launch the exe too"))
    assert result.ok, result.failures
    assert restarts == [1]


def test_a_failing_service_restart_is_reported_and_rolled_back(checkout, build, fake_abp):
    server = fake_abp(_healthy_spec())
    install = _install_dir(checkout)
    assert deploy_local.deploy(checkout, do_build=False, port=server.port, log=lambda _s: None,
                               launcher=lambda _exe: None).ok
    good_contents = (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8")
    _write_bundle(build, "v2")

    def restart():
        raise deploy_local.DeployError("service restart failed")

    result = deploy_local.deploy(checkout, do_build=False, port=server.port, restart=restart,
                                 log=lambda _s: None, launcher=lambda _exe: None)
    assert not result.ok and "service restart failed" in result.failures[0]
    assert (install / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == good_contents


def fake_abp_process_started(exe: Path, server: FakeAbp):
    """The launcher hook a test hands to deploy(): the install folder's exe
    is not a launchable binary, so the "app" is the already-running fake
    ABP. What is under test is that deploy() really starts something and
    then really talks to it."""
    assert exe.name == EXE
    assert psutil.pid_exists(server.proc.pid)
    return server.proc


# --------------------------------------------------------------------------- #
# The always-on path: versioned folders, and a gate in front of the port
#
# The end-to-end half of this (a real gate, a real swap, a client that never
# sees a failed request) is tests/test_deploy_gate_hot_swap.py. What is here is
# the decisions around it, which are worth pinning down on their own: which
# folder a deploy writes to, what may be deleted, and when this script is
# allowed to believe there is a gate in front of the port.
# --------------------------------------------------------------------------- #
def test_versioned_folders_are_numbered_beside_the_install_folder_and_never_reused(tmp_path):
    install = tmp_path / "release"
    install.mkdir()
    for name in ("release.v1", "release.v2", "release.v10", "release.previous", "release.vbs"):
        (tmp_path / name).mkdir()
    (tmp_path / "release.v1" / "bot").mkdir()

    assert deploy_local.next_version_dir(install) == tmp_path / "release.v11", "must sort as a number, not as text"
    assert [p.name for _n, p in deploy_local.versioned_dirs(install)] == ["release.v1", "release.v2", "release.v10"], \
        "only numbered folders are versions, and .previous/.vbs are somebody else's"
    assert deploy_local.next_version_dir(tmp_path / "nothing-here-yet") == tmp_path / "nothing-here-yet.v1"


def test_only_the_last_n_versioned_folders_go_and_never_one_an_instance_runs_from(tmp_path):
    """Three rules, and the third is the one that matters: a folder an instance
    is still running from is kept past the limit and SAID out loud, because
    quietly keeping more than asked for beats quietly deleting the code a
    rollback would need."""
    install = tmp_path / "release"
    install.mkdir()
    for n in range(1, 6):
        (tmp_path / f"release.v{n}").mkdir()
    lines: list[str] = []
    in_use = {tmp_path / "release.v1", tmp_path / "release.v3"}

    deleted, spared = deploy_local.prune_versions(install, keep=2, in_use=in_use, log=lines.append)

    assert deleted == [tmp_path / "release.v2"], deleted
    assert spared == [tmp_path / "release.v1", tmp_path / "release.v3"], spared
    assert sorted(p.name for p in tmp_path.glob("release*")) == ["release", "release.v1", "release.v3",
                                                                  "release.v4", "release.v5"]
    assert any("keeping release.v1: an instance is still running from it" in line for line in lines), lines
    assert any("deleted release.v2" in line for line in lines), lines

    # Nothing left in use: the limit is then exactly the limit.
    deleted, spared = deploy_local.prune_versions(install, keep=2, in_use={tmp_path / "release.v4"},
                                                  log=lambda _s: None)
    assert deleted == [tmp_path / "release.v1", tmp_path / "release.v3"], deleted
    assert spared == []


def test_a_versioned_folder_is_installed_into_with_no_second_copy_beside_it(checkout, build):
    """A versioned folder is new and empty, so there is nothing to put back to -
    and keeping a whole second copy of a multi-hundred-megabyte bundle beside
    every version is how a disk fills up. Recovery is a swap, not a copy."""
    install = _install_dir(checkout)
    versioned = deploy_local.next_version_dir(install)
    plan = deploy_local.plan_install(checkout, build / "release", versioned)
    assert deploy_local.install_resources(plan, versioned, log=lambda _s: None, keep_previous=False) is None
    assert (versioned / "bot" / "nested" / "deep.py").read_text(encoding="utf-8") == "DEEP = 'v1'\n"
    assert not deploy_local.previous_dir(versioned).exists()
    assert list(versioned.parent.iterdir()) == [versioned], "no .previous was left behind"


def test_a_gate_is_only_trusted_when_it_says_so_and_owns_the_port(tmp_path, monkeypatch):
    """Three ways "is there a gate in front of this port?" can go wrong, and all
    three have to be answered no:

      * nothing is there (the ordinary case - this is the whole point, a deploy
        on a machine with no gate behaves exactly as it always did);
      * something answers the control port's /healthz but is not a gate - the
        same decision the desktop app makes, so a stray service can never be
        taken for one;
      * a real gate, for a different ABP_HOME, that therefore does not own this
        port. Swapping through it would deploy somebody else's install.

    The healthz answer is stubbed here because the decision is what is under
    test; the real gate, really answering, really swapping, is
    tests/test_deploy_gate_hot_swap.py."""
    from abp_gate import control

    state = tmp_path / "home"
    state.mkdir()
    env = {"ABP_HOME": str(state), "ABP_INSTANCES_DIR": str(tmp_path / "instances")}

    assert deploy_local.gate_in_front(state, 8787, environ=env) is None, "no gate has ever run here"

    monkeypatch.setattr(control, "gate_running", lambda: True)
    monkeypatch.setattr(control, "read_gate_meta", lambda: {
        "pid": 4242, "control_url": "http://127.0.0.1:9999", "public_ports": [8787]})

    monkeypatch.setattr(deploy_local, "_get", lambda *a, **k: (200, {"status": "ok"}))
    assert deploy_local.gate_in_front(state, 8787, environ=env) is None, "a service that does not name itself is not a gate"

    monkeypatch.setattr(deploy_local, "_get", lambda *a, **k: (200, {"gate": "0.1.0", "healthy": True}))
    found = deploy_local.gate_in_front(state, 8787, environ=env)
    assert found and found["control_url"] == "http://127.0.0.1:9999" and found["pid"] == 4242
    assert deploy_local.gate_in_front(state, 9999, environ=env) is None, "a gate that does not own this port is another machine's"

    monkeypatch.setattr(control, "gate_running", lambda: False)
    assert deploy_local.gate_in_front(state, 8787, environ=env) is None, "a stale gate.json must not conjure a gate"


def test_a_gate_refusal_is_reported_in_the_gates_own_words():
    """A 409 from a swap says which step failed and who is still serving, and
    that sentence is the whole diagnosis. Keeping it verbatim is the point; a
    bare "HTTP 409" would send somebody to the gate's log to re-read it."""
    detail = "swap to X failed at step 1 (the instance exited at once); prod-1 is still serving"
    reason = deploy_local._gate_reason(409, {"detail": detail})
    assert "HTTP 409" in reason and "failed at step 1" in reason and "still serving" in reason
    assert "no answer from the gate" in deploy_local._gate_reason(0, "Connection refused")
    assert "HTTP 503" in deploy_local._gate_reason(503, "no DASHBOARD_TOKEN in this install's .env yet")
    # Whatever shape the body is in, it must not raise: this is on the path of a
    # command a person just ran.
    assert deploy_local._gate_reason(500, "<html>gateway</html>").startswith("HTTP 500")
