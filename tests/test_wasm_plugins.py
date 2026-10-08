"""bot/wasm_plugins/ and bot/sandbox_ns/wasm.py, against real compiled components.

Nothing here is mocked. The components under test are the ones in `examples/wasm/`, built for
`wasm32-wasip2` with cargo; the HTTP server the network test reaches is a real one on
127.0.0.1 that this file starts and stops; the files the filesystem test reads are real files
in tmp_path; the audit rows are real rows in a real database. What the tests assert is that
the sandbox actually stops things, which cannot be established by a mock that agrees.

    python -m pytest tests/test_wasm_plugins.py -q -p no:cacheprovider

The components are built on first use (about half a minute each) or found already built under
`$CARGO_TARGET_DIR`. When neither is possible - no cargo, no wasm32-wasip2 target - the tests
that need a component are skipped with the reason, rather than passing vacuously.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bot import db, envfile
from bot.wasm_plugins import manager, runtime, tools

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples" / "wasm"

#: The bound the throughput test holds the runtime to. A wasm call is an instantiate and a
#: call, both in-process; 1,000 of them in five seconds is a very loose ceiling that still
#: fails loudly if a call ever grows a process-shaped cost.
THROUGHPUT_BOUND_S = 5.0
THROUGHPUT_CALLS = 1000


def _run(coro):
    """The house style: asyncio.run, no pytest-asyncio."""
    return asyncio.run(coro)


def _target_dir() -> Path:
    return Path(os.environ.get("CARGO_TARGET_DIR") or (REPO_ROOT / "native" / "abp-wasm" / "target"))


def _built(name: str) -> Path:
    return _target_dir() / "wasm32-wasip2" / "release" / f"{name}.wasm"


def _build(example: str, crate: str) -> Path:
    """Build one example component for wasm32-wasip2, windowless and with a timeout.

    cargo is a console program, so it goes through bot.sandbox_ns.spawn: that is what makes
    every process ABP starts windowless, and it puts the build in a cell that can be killed
    whole if it hangs.
    """
    from bot.sandbox_ns import spawn

    existing = _built(crate)
    if existing.exists():
        return existing
    if not shutil.which("cargo"):
        pytest.skip("cargo is not on PATH: cannot build the example components")
    workdir = EXAMPLES / example
    result = spawn.run(
        ["cargo", "build", "--release", "--target", "wasm32-wasip2", "-j", "2"],
        cwd=workdir,
        timeout=600,
        check=False,
    )
    if result.returncode != 0 or not existing.exists():
        pytest.skip(f"could not build {example} for wasm32-wasip2 (cargo exited {result.returncode})")
    return existing


@pytest.fixture(scope="session")
def probes_component() -> Path:
    """The `probes` example: the plugin whose tools exist to be stopped."""
    if not runtime.available():
        pytest.skip("the native wasm module is not built")
    return _build("rust-probes", "abp_wasm_example_probes")


@pytest.fixture(scope="session")
def hello_component() -> Path:
    """The `hello` example: the smallest plugin there is."""
    if not runtime.available():
        pytest.skip("the native wasm module is not built")
    return _build("rust-hello", "abp_wasm_example_hello")


@pytest.fixture(scope="session")
def py_hello_component() -> Path:
    """The Python example, compiled with componentize-py when it is installed (on PATH, or
    importable); skipped otherwise. Built once into the cargo target folder."""
    if not runtime.available():
        pytest.skip("the native wasm module is not built")
    out = _target_dir() / "abp_py_hello.wasm"
    src = EXAMPLES / "py-hello"
    if out.exists() and out.stat().st_mtime >= (src / "abp_plugin.py").stat().st_mtime:
        return out
    exe = shutil.which("componentize-py")
    if exe:
        cmd = [exe]
    else:
        try:
            import componentize_py  # noqa: F401
        except ImportError:
            pytest.skip("componentize-py is not installed: cannot build the Python example")
        cmd = [sys.executable, "-c", "import sys, componentize_py; sys.argv[0] = \"componentize-py\"; componentize_py.script()"]
    from bot.sandbox_ns import spawn

    out.parent.mkdir(parents=True, exist_ok=True)
    result = spawn.run(cmd + ["-d", str(EXAMPLES.parent.parent / "wit"), "-w", "plugin", "componentize", "abp_plugin",
                              "-o", str(out)], cwd=src, timeout=600, check=False,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0 or not out.exists():
        said = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode(errors="replace")
        said = said[-1500:]
        pytest.fail(f"componentize-py could not build the Python example (exit {result.returncode}):\n{said}")
    return out


@pytest.fixture(scope="session")
def js_hello_component() -> Path:
    """The JavaScript example, compiled with jco when `jco` is on PATH; skipped otherwise."""
    if not runtime.available():
        pytest.skip("the native wasm module is not built")
    out = _target_dir() / "abp_js_hello.wasm"
    src = EXAMPLES / "js-hello"
    if out.exists() and out.stat().st_mtime >= (src / "abp_plugin.js").stat().st_mtime:
        return out
    jco = shutil.which("jco")
    if not jco:
        pytest.skip("jco is not on PATH: cannot build the JavaScript example")
    from bot.sandbox_ns import spawn

    out.parent.mkdir(parents=True, exist_ok=True)
    result = spawn.run([jco, "componentize", "abp_plugin.js", "--wit", str(EXAMPLES.parent.parent / "wit"),
                        "--world-name", "plugin", "--disable", "http", "fetch-event", "-o", str(out)],
                       cwd=src, timeout=600, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0 or not out.exists():
        said = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode(errors="replace")
        pytest.fail(f"jco could not build the JavaScript example (exit {result.returncode}):\n{said[-1500:]}")
    return out


@pytest.fixture
def project(tmp_path, monkeypatch):
    """ABP's data directory, under tmp_path, so installs and stores land somewhere throwaway."""
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(envfile, "PROJECT_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def _clean_registry(project):
    """Every test starts with no plugins loaded and none registered, and leaves none behind."""
    manager.reset_for_tests()
    tools.reset_for_tests()
    yield
    manager.reset_for_tests()
    tools.reset_for_tests()
    runtime.Runtime.reset()


def _services(name: str, project: Path, network: list | None = None):
    return runtime.HostServices(name, project / "stores" / name, network=list(network or []))


# ---------------------------------------------------------------- the runtime's own limits

def test_a_tool_call_returns_the_plugins_own_result(hello_component, project):
    handle = runtime.Runtime.shared().load(
        hello_component, grants=runtime.grants_for(), services=_services("hello", project)
    )
    assert handle.tool_names() == ["greet"]
    result = handle.invoke("greet", json.dumps({"name": "world"}), timeout_ms=2000)
    assert json.loads(result["ok"]) == {"greeting": "Hello, world!"}


def test_a_tool_that_reports_its_own_failure_comes_back_as_an_error(hello_component, project):
    handle = runtime.Runtime.shared().load(
        hello_component, grants=runtime.grants_for(), services=_services("hello", project)
    )
    result = handle.invoke("no-such-tool", "{}", timeout_ms=2000)
    assert "ok" not in result and result["error"]


def test_the_python_example_runs(py_hello_component, project):
    handle = runtime.Runtime.shared().load(
        py_hello_component, grants=runtime.grants_for(), services=_services("py-hello", project)
    )
    assert handle.tool_names() == ["greet"]
    result = handle.invoke("greet", json.dumps({"name": "world"}), timeout_ms=5000)
    assert json.loads(result["ok"]) == {"greeting": "Hello, world!"}
    assert result["logs"] == ["[info] greeted world"]
    assert "name" in handle.invoke("greet", json.dumps({}), timeout_ms=5000)["error"]


def test_the_javascript_example_runs(js_hello_component, project):
    handle = runtime.Runtime.shared().load(
        js_hello_component, grants=runtime.grants_for(), services=_services("js-hello", project)
    )
    assert handle.tool_names() == ["greet"]
    result = handle.invoke("greet", json.dumps({"name": "world"}), timeout_ms=5000)
    assert json.loads(result["ok"]) == {"greeting": "Hello, world!"}
    assert result["logs"] == ["[info] greeted world"]
    assert "name" in handle.invoke("greet", json.dumps({}), timeout_ms=5000)["error"]


def test_the_manifest_says_what_the_plugin_offers(hello_component, project):
    handle = runtime.Runtime.shared().load(
        hello_component, grants=runtime.grants_for(), services=_services("hello", project)
    )
    manifest = handle.manifest
    assert manifest["name"] == "hello"
    assert manifest["tools"][0]["name"] == "greet"
    # The schema the model sees is the plugin's own, parsed rather than invented here.
    assert json.loads(manifest["tools"][0]["schema"])["required"] == ["name"]


def test_an_infinite_loop_is_stopped_by_its_deadline(probes_component, project):
    """The whole point of a deadline: a guest that never returns is stopped, not waited on."""
    handle = runtime.Runtime.shared().load(
        probes_component,
        grants=runtime.grants_for(),
        services=_services("probes", project),
        memory_limit_bytes=8 * 1024 * 1024,
    )
    started = time.perf_counter()
    with pytest.raises(runtime.PluginCallError) as raised:
        handle.invoke("spin", "{}", timeout_ms=250)
    elapsed = time.perf_counter() - started
    assert "deadline" in str(raised.value).lower()
    # Generously over the budget, and nowhere near "it ran to completion".
    assert elapsed < 5.0, f"the deadline did not stop the call promptly ({elapsed:.1f}s)"


def test_the_memory_limit_holds(probes_component, project):
    """A guest that allocates without end stops at the cap, with the memory error rather than
    the deadline: the two limits are different and a caller has to be able to tell them."""
    handle = runtime.Runtime.shared().load(
        probes_component,
        grants=runtime.grants_for(),
        services=_services("probes", project),
        memory_limit_bytes=8 * 1024 * 1024,
    )
    with pytest.raises(runtime.PluginCallError) as raised:
        handle.invoke("grow", "{}", timeout_ms=10_000)
    assert "memory" in str(raised.value).lower()


def test_a_result_over_the_output_cap_is_refused(probes_component, project):
    handle = runtime.Runtime.shared().load(
        probes_component,
        grants=runtime.grants_for(max_output_bytes=64 * 1024),
        services=_services("probes", project),
    )
    result = handle.invoke("flood", json.dumps({"bytes": 512 * 1024}), timeout_ms=5000)
    assert "cap" in result["error"]


def test_a_request_outside_the_allowlist_is_denied(probes_component, project):
    handle = runtime.Runtime.shared().load(
        probes_component,
        grants=runtime.grants_for(network=[]),
        services=_services("probes", project),
    )
    result = handle.invoke("fetch", json.dumps({"url": "http://example.com/"}), timeout_ms=2000)
    assert "may not reach example.com" in result["error"]


def test_a_request_inside_the_allowlist_goes_through(probes_component, project):
    """The other side of the same rule: a granted host is reachable, and the answer is real."""
    with _loopback_server() as server:
        handle = runtime.Runtime.shared().load(
            probes_component,
            grants=runtime.grants_for(network=[f"127.0.0.1:{server.server_address[1]}"]),
            services=_services("probes", project, network=[f"127.0.0.1:{server.server_address[1]}"]),
        )
        url = f"http://127.0.0.1:{server.server_address[1]}/hello"
        result = handle.invoke("fetch", json.dumps({"url": url}), timeout_ms=5000)
    assert json.loads(result["ok"])["status"] == 200


def test_a_file_inside_a_preopen_can_be_read(probes_component, project):
    granted = project / "data"
    granted.mkdir(parents=True, exist_ok=True)
    (granted / "hello.txt").write_text("hello from disk", encoding="utf-8")
    handle = runtime.Runtime.shared().load(
        probes_component,
        grants=runtime.grants_for(preopens={"/data": str(granted)}),
        services=_services("probes", project),
    )
    result = handle.invoke("read", json.dumps({"path": "/data/hello.txt"}), timeout_ms=2000)
    assert json.loads(result["ok"])["bytes"] == len("hello from disk")


def test_a_file_outside_the_preopens_cannot_be_reached(probes_component, project, tmp_path):
    """With nothing preopened, the guest has no way in - not a path it guesses and not one it
    climbs out to."""
    outside = project / "secret.txt"
    outside.write_text("not for plugins", encoding="utf-8")
    granted = project / "data"
    granted.mkdir(parents=True, exist_ok=True)
    handle = runtime.Runtime.shared().load(
        probes_component,
        grants=runtime.grants_for(preopens={"/data": str(granted)}),
        services=_services("probes", project),
    )
    for path in ("/etc/hosts", str(outside).replace("\\", "/"), "/data/../secret.txt"):
        result = handle.invoke("read", json.dumps({"path": path}), timeout_ms=2000)
        assert "ok" not in result, f"{path} was readable: {result}"
        assert "could not read" in result["error"]


def test_each_plugins_store_is_its_own(probes_component, hello_component, project):
    """Two plugins, two stores: one plugin's keys are invisible to the other, and to whatever
    the host keeps for itself."""
    probes = runtime.Runtime.shared().load(
        probes_component, grants=runtime.grants_for(), services=_services("probes", project)
    )
    hello = runtime.Runtime.shared().load(
        hello_component, grants=runtime.grants_for(), services=_services("hello", project)
    )
    probes.invoke("kv", json.dumps({"key": "secret", "value": "probes' own"}), timeout_ms=2000)

    probes_store = runtime.HostServices("probes", project / "stores" / "probes", network=[])
    hello_store = runtime.HostServices("hello", project / "stores" / "hello", network=[])
    assert probes_store.store_get("secret") == "probes' own"
    assert hello_store.store_get("secret") is None
    # The keys really are on disk under the plugin's own directory, not in a shared map.
    assert (project / "stores" / "probes" / "secret").exists()
    assert not (project / "stores" / "hello" / "secret").exists()
    assert hello.invoke("greet", json.dumps({"name": "x"}), timeout_ms=2000)["ok"]


def test_a_thousand_invocations_finish_inside_the_stated_bound(hello_component, project):
    """The reason for the whole exercise: a tool call is an instantiate and a call, not a
    process. The bound is stated so a regression is a number, not a feeling."""
    handle = runtime.Runtime.shared().load(
        hello_component, grants=runtime.grants_for(), services=_services("hello", project)
    )
    started = time.perf_counter()
    for _ in range(THROUGHPUT_CALLS):
        handle.invoke("greet", json.dumps({"name": "world"}), timeout_ms=2000)
    elapsed = time.perf_counter() - started
    assert elapsed < THROUGHPUT_BOUND_S, (
        f"{THROUGHPUT_CALLS} invocations took {elapsed:.1f}s, over the {THROUGHPUT_BOUND_S}s bound"
    )


def test_a_malformed_component_is_rejected(project, tmp_path):
    """A file that is not a component fails at load, with a message rather than a crash - and
    it fails the same way whether it is nonsense bytes or a valid module that is not a plugin."""
    import abp_wasm

    broken = tmp_path / "broken.wasm"
    broken.write_bytes(b"\x00asm\x01\x00\x00\x00this is not a component")
    engine = abp_wasm.Runtime()
    with pytest.raises(abp_wasm.WasmError) as raised:
        engine.load(broken, grants=runtime.grants_for(), services=_services("broken", project))
    assert "not a component" in str(raised.value)


def test_an_empty_file_is_rejected(project, tmp_path):
    import abp_wasm

    empty = tmp_path / "empty.wasm"
    empty.write_bytes(b"")
    engine = abp_wasm.Runtime()
    with pytest.raises(abp_wasm.WasmError):
        engine.load(empty, grants=runtime.grants_for(), services=_services("empty", project))


# ---------------------------------------------------------------- install, and the agent

def test_installing_makes_the_tools_agent_tools(hello_component, project, temp_db):
    from bot.agent_runtime import toolspec

    info = manager.install(str(hello_component))
    assert info["name"] == "hello"
    assert info["enabled"] is False, "installing must not activate a plugin on its own"

    manager.enable("hello")
    assert "hello_greet" in toolspec.registered_names()
    schema = next(s for s in toolspec.registered_schemas() if s["name"] == "hello_greet")
    assert schema["input_schema"]["required"] == ["name"]


def test_the_agent_can_call_a_plugin_tool_and_the_call_is_audited(hello_component, project, temp_db):
    from bot.agent_runtime import tools

    manager.install(str(hello_component), enabled=True)
    assert tools.all_tool_schemas()
    assert any(s["name"] == "hello_greet" for s in tools.all_tool_schemas())

    result = _run(tools.execute_tool("hello_greet", {"name": "audit"}, workspace=project, instance_id=None))
    assert json.loads(result) == {"greeting": "Hello, audit!"}

    rows = db.list_audit_log(["wasm_plugin_call"])
    assert rows, "a plugin call must leave an audit row"
    assert "hello.greet ok" in rows[0]["detail"]


def test_install_records_what_the_plugin_asked_for(probes_component, project, temp_db):
    """The capability list is read at install time and kept next to the grants, so the
    difference between the two - what a person approved - is still there to be looked at."""
    manager.install(str(probes_component))
    info = manager.describe("probes")
    assert info["requested"] == {"preopens": ["/data"], "network": ["127.0.0.1"]}
    assert info["granted"] == {"preopens": ["/data"], "network": ["127.0.0.1"]}

    # Approving less than the plugin asked for is the normal case.
    manager.uninstall("probes")
    manager.install(str(probes_component), approve_preopens=[], approve_network=[])
    info = manager.describe("probes")
    assert info["granted"] == {"preopens": [], "network": []}
    assert info["requested"] == {"preopens": ["/data"], "network": ["127.0.0.1"]}


def test_approving_something_the_plugin_never_asked_for_is_refused(hello_component, project, temp_db):
    """The hello plugin asks for nothing, so there is nothing a person can approve."""
    with pytest.raises(manager.WasmPluginError) as raised:
        manager.install(str(hello_component), approve_network=["example.com"])
    assert "did not ask for" in str(raised.value)


def test_narrowing_grants_can_only_take_them_away(hello_component, project, temp_db):
    manager.install(str(hello_component))
    assert manager.narrow_grants("hello") == {"preopens": [], "network": []}
    with pytest.raises(manager.WasmPluginError):
        manager.narrow_grants("hello", network=["example.com"])


def test_disabling_takes_the_tools_back_out_of_the_agent_list(hello_component, project, temp_db):
    from bot.agent_runtime import toolspec

    manager.install(str(hello_component), enabled=True)
    assert "hello_greet" in toolspec.registered_names()
    manager.disable("hello")
    assert "hello_greet" not in toolspec.registered_names()
    assert manager.describe("hello")["enabled"] is False


def test_uninstalling_removes_the_component_and_the_store(hello_component, project, temp_db):
    manager.install(str(hello_component), enabled=True)
    home = project / "data" / "wasm_plugins" / "hello"
    assert (home / "plugin.wasm").exists()
    assert manager.uninstall("hello") is True
    assert not home.exists()
    assert manager.list_plugins() == []


def test_installing_something_that_is_not_a_plugin_says_so(project, temp_db):
    junk = project / "notaplugin.wasm"
    junk.write_bytes(b"hello, I am a text file")
    with pytest.raises(manager.WasmPluginError) as raised:
        manager.install(str(junk))
    assert "not a plugin ABP can load" in str(raised.value)


def test_capabilities_summarise_what_a_plugin_asked_for(hello_component, project, temp_db):
    """The shape the approval prompt and the dashboard show: tools, directories, hosts.

    Read from a real installed plugin rather than a hand-written dict, because the thing being
    summarised is the manifest the plugin actually sent.
    """
    manager.install(str(hello_component))
    caps = manager.capabilities(manager._row("hello"))  # noqa: SLF001 - the registry's own record
    assert caps["tools"] == [
        {"name": "greet", "description": "Greet someone by name, optionally with a greeting of your choosing"}
    ]
    assert caps["preopens"] == []
    assert caps["network"] == []


def test_enabling_a_plugin_that_is_not_installed_says_which_are(hello_component, project, temp_db):
    with pytest.raises(manager.WasmPluginError) as raised:
        manager.enable("nothing")
    assert "no wasm plugin named" in str(raised.value)


def test_load_enabled_picks_up_what_was_left_enabled(hello_component, project, temp_db):
    manager.install(str(hello_component), enabled=True)
    manager.reset_for_tests()
    assert not manager.is_loaded("hello")
    manager.load_enabled()
    assert manager.is_loaded("hello")


# ---------------------------------------------------------------- the wasm cell

def test_a_wasm_cell_accounts_for_a_call(hello_component, project, temp_db):
    from bot.sandbox_ns import wasm as wasm_cell

    manager.install(str(hello_component), enabled=True)
    cell = wasm_cell.new_cell(plugin="hello", tool="greet", timeout_ms=1000)
    result = cell.invoke("hello", "greet", {"name": "cell"})
    assert json.loads(result) == {"greeting": "Hello, cell!"}

    status = cell.status()
    assert status["kind"] == "wasm"
    assert status["os"] == "wasm"
    assert status["pids"] == [], "a wasm call has no process, and the cell must not pretend otherwise"
    assert status["calls"] == 1
    assert cell.pids() == []
    cell.close()
    assert cell.closed is True


def test_a_wasm_cell_may_only_shorten_a_deadline(hello_component, project, temp_db):
    from bot.sandbox_ns import wasm as wasm_cell

    manager.install(str(hello_component), enabled=True)
    cell = wasm_cell.new_cell(plugin="hello", tool="greet", timeout_ms=1000)
    # A longer budget than the cell's is clamped down to the cell's.
    result = cell.invoke("hello", "greet", {"name": "x"}, timeout_ms=60_000)
    assert json.loads(result)["greeting"] == "Hello, x!"
    assert cell.status()["limits_applied"]["timeout_ms"] == 1000


def test_a_wasm_run_reports_the_failure_rather_than_raising(probes_component, project, temp_db):
    from bot.sandbox_ns import wasm as wasm_cell

    manager.install(str(probes_component), enabled=True)
    outcome = wasm_cell.run("probes", "spin", {}, timeout_ms=250)
    assert outcome["ok"] is False
    assert "deadline" in outcome["error"].lower()


def test_the_wasm_preset_says_what_it_is():
    from bot.sandbox_ns import wasm as wasm_cell

    described = wasm_cell.describe_policy()
    assert described["name"] == "wasm"
    assert described["persistent"] is False


# ---------------------------------------------------------------- the dashboard API

def _client(monkeypatch):
    from fastapi.testclient import TestClient
    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def test_the_dashboard_api_lists_installs_and_invokes(hello_component, project, temp_db, monkeypatch):
    """The four things a person does from the dashboard, against the real FastAPI app."""
    client = _client(monkeypatch)
    headers = {"X-Dashboard-Token": "test-token"}

    status = client.get("/api/wasm-plugins/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["available"] is True

    installed = client.post(
        "/api/wasm-plugins/install",
        json={"source": str(hello_component), "enable": True},
        headers=headers,
    )
    assert installed.status_code == 200, installed.text
    assert installed.json()["name"] == "hello"
    assert installed.json()["requested"] == {"preopens": [], "network": []}

    listed = client.get("/api/wasm-plugins", headers=headers)
    assert [p["name"] for p in listed.json()["plugins"]] == ["hello"]

    called = client.post(
        "/api/wasm-plugins/hello/invoke", json={"tool": "greet", "args": {"name": "dash"}}, headers=headers
    )
    assert called.status_code == 200, called.text
    assert json.loads(called.json()["result"]) == {"greeting": "Hello, dash!"}

    assert client.post("/api/wasm-plugins/hello/disable", headers=headers).status_code == 200
    assert client.post("/api/wasm-plugins/hello/uninstall", headers=headers).status_code == 200
    assert client.get("/api/wasm-plugins", headers=headers).json()["plugins"] == []


def test_the_dashboard_api_needs_the_token(hello_component, project, temp_db, monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/api/wasm-plugins").status_code == 401
    assert client.get("/api/wasm-plugins/status").status_code == 401


def test_the_dashboard_api_reports_a_deadline_as_such(probes_component, project, temp_db, monkeypatch):
    """A limit is not a bad request: 429, with the reason in the detail, because the caller
    can do something about it (a longer budget, a smaller job)."""
    client = _client(monkeypatch)
    headers = {"X-Dashboard-Token": "test-token"}
    assert client.post(
        "/api/wasm-plugins/install",
        json={"source": str(probes_component), "enable": True, "timeout_ms": 5000},
        headers=headers,
    ).status_code == 200
    response = client.post(
        "/api/wasm-plugins/probes/invoke",
        json={"tool": "spin", "args": {}, "timeout_ms": 200},
        headers=headers,
    )
    assert response.status_code == 429
    assert "deadline" in response.json()["detail"].lower()


def test_the_dashboard_api_400s_on_a_bad_install(project, temp_db, monkeypatch):
    client = _client(monkeypatch)
    headers = {"X-Dashboard-Token": "test-token"}
    assert client.post("/api/wasm-plugins/install", json={}, headers=headers).status_code == 400
    junk = project / "junk.wasm"
    junk.write_bytes(b"not a component")
    response = client.post("/api/wasm-plugins/install", json={"source": str(junk)}, headers=headers)
    assert response.status_code == 400
    assert "not a plugin" in response.json()["detail"]


# ---------------------------------------------------------------- helpers

class _Handler(BaseHTTPRequestHandler):
    """A real HTTP server on 127.0.0.1, in this process, answering one route."""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        body = b"hello from the test server"
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: ANN002, ANN201 - silence the default stderr logging
        return


class _Loopback:
    def __enter__(self) -> ThreadingHTTPServer:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *exc) -> None:
        # shutdown() stops serve_forever and closes the listening socket; server_close() then
        # releases it. Both, because a leaked listening socket is a leaked port.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _loopback_server() -> _Loopback:
    return _Loopback()
