"""bot/modelmistress/guard.py and the supervisor/conductor checks built on it: the edge cases found bringing up the 0.3.0 hub
(2026-10-19) - a packaged process's virtualized view of the MM home, a second hub started for a home that already has one, a
hub built without its engine, and a model served with too little context per request."""
from __future__ import annotations

import pytest

from bot.modelmistress import client, guard, supervisor

CODER = "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    client.forget()
    saved = dict(supervisor._state)
    yield
    supervisor._state.clear()
    supervisor._state.update(saved)
    client.forget()


# ---- a packaged process's view of AppData ------------------------------------------------------------------------------------
@pytest.fixture
def appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(guard.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    (tmp_path / "Local" / "Packages").mkdir(parents=True)
    monkeypatch.setattr(guard, "_probe_cache", {})
    return tmp_path


def test_a_packaged_process_is_told_its_view_of_the_mm_home_is_virtualized(monkeypatch, appdata):
    monkeypatch.setattr(guard, "packaged_identity", lambda: "Claude_1.0.0.0_x64__abc")
    why = guard.virtualized(appdata / "Local" / "ModelMistress")
    assert why and "Claude" in why and "redirects" in why
    assert guard.virtualized(appdata / "elsewhere" / "ModelMistress") is None


def test_the_redirect_is_found_by_experiment_when_the_api_sees_no_package(monkeypatch, appdata):
    """The case found: a tool started from the packaged Claude app had GetCurrentPackageFullName say "no package" while its
    writes under LOCALAPPDATA landed in Packages/Claude_.../LocalCache/Local."""
    monkeypatch.setattr(guard, "packaged_identity", lambda: None)
    store = appdata / "Local" / "Packages" / "Claude_pzs" / "LocalCache" / "Local"
    store.mkdir(parents=True)
    real_write = guard.Path.write_bytes

    def redirected(self, data):                 # what Windows does to this process's write
        return real_write(store / self.name, data) if self.parent == appdata / "Local" else real_write(self, data)

    monkeypatch.setattr(guard.Path, "write_bytes", redirected)
    assert "Claude" in guard.virtualized(appdata / "Local" / "ModelMistress")
    assert not list(store.iterdir()) or all(not p.name.startswith(".abp-vprobe") for p in (appdata / "Local").iterdir())


def test_an_unredirected_process_is_not_virtualized(monkeypatch, appdata):
    monkeypatch.setattr(guard, "packaged_identity", lambda: None)
    assert guard.virtualized(appdata / "Local" / "ModelMistress") is None
    assert [p.name for p in (appdata / "Local").iterdir()] == ["Packages"]          # the probe cleaned up after itself


def test_packaged_identity_never_raises():
    assert guard.packaged_identity() is None or isinstance(guard.packaged_identity(), str)


def test_a_packaged_process_never_starts_the_shared_hub(monkeypatch):
    monkeypatch.setattr(guard, "virtualized", lambda path: "virtualized view")
    with pytest.raises(RuntimeError, match="not starting a hub from here: virtualized view"):
        supervisor._start()


# ---- one hub per home ----------------------------------------------------------------------------------------------------------
class _Proc:
    def __init__(self, pid, name, cmdline):
        self.info = {"pid": pid, "name": name}       # what process_iter(["pid", "name"]) gives
        self._cmd = cmdline

    def cmdline(self):
        if not self.info["name"].lower().startswith("model-mistress"):
            raise AssertionError("only a model-mistress process's command line is read")
        return self._cmd


def test_hub_processes_finds_a_hub_for_this_home_only(monkeypatch, tmp_path):
    import psutil

    home = tmp_path / "MM"
    procs = [_Proc(11, "model-mistress.exe", ["x/model-mistress.exe", "--home", str(home).upper() if guard.os.name == "nt" else str(home), "serve"]),
             _Proc(12, "model-mistress.exe", ["x/model-mistress.exe", "--home", str(tmp_path / "other"), "serve"]),
             _Proc(13, "model-mistress.exe", ["x/model-mistress.exe", "--home", str(home), "mcp"]),
             _Proc(14, "python.exe", ["python", "--home", str(home), "serve"])]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    assert guard.hub_processes(home) == [11]


def test_a_second_hub_is_never_started_while_one_runs_for_the_home(monkeypatch):
    """The case found: a hub this process could not reach (startup race, or a virtualized control file) got a twin."""
    monkeypatch.setitem(supervisor._state, "unreachable", None)
    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    monkeypatch.delenv("ABP_NO_MODELMISTRESS", raising=False)
    monkeypatch.setattr(client, "_cfg", lambda: {"autostart": True})
    monkeypatch.setattr(supervisor, "_sandboxed", lambda: None)
    monkeypatch.setattr(client, "find", lambda *a, **k: None)
    monkeypatch.setattr(guard, "hub_processes", lambda home: [4242])
    monkeypatch.setattr(guard, "virtualized", lambda path: None)
    monkeypatch.setattr(supervisor, "_start", lambda: pytest.fail("a second hub must not be started"))
    assert supervisor.ensure(wait_s=0.2) is None
    assert "pid 4242" in supervisor._state["last_error"] and "cannot reach it" in supervisor._state["last_error"]


def test_a_starting_hub_is_waited_for_and_reused(monkeypatch):
    monkeypatch.setitem(supervisor._state, "unreachable", None)
    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    monkeypatch.delenv("ABP_NO_MODELMISTRESS", raising=False)
    monkeypatch.setattr(client, "_cfg", lambda: {"autostart": True})
    monkeypatch.setattr(supervisor, "_sandboxed", lambda: None)
    monkeypatch.setattr(guard, "hub_processes", lambda home: [4242])
    monkeypatch.setattr(guard, "virtualized", lambda path: None)
    monkeypatch.setattr(supervisor, "_start", lambda: pytest.fail("a second hub must not be started"))
    answers = iter([None, None, client.Hub("http://127.0.0.1:1", "t", "0.3.0")])
    monkeypatch.setattr(client, "find", lambda *a, **k: next(answers))
    monkeypatch.setattr(supervisor.time, "sleep", lambda s: None)
    hub = supervisor.ensure(wait_s=30)
    assert hub is not None and hub.version == "0.3.0"


def test_an_unreachable_hub_is_not_waited_for_again_or_at_all_when_the_view_is_redirected(monkeypatch):
    # live 2026-10-20: every chat send from a process that could not reach the running hub stalled 45 s, so the page timed out
    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    monkeypatch.delenv("ABP_NO_MODELMISTRESS", raising=False)
    monkeypatch.setattr(client, "_cfg", lambda: {"autostart": True})
    monkeypatch.setattr(supervisor, "_sandboxed", lambda: None)
    monkeypatch.setattr(supervisor, "_start", lambda: pytest.fail("a second hub must not be started"))
    monkeypatch.setattr(guard, "hub_processes", lambda home: [4242])
    monkeypatch.setitem(supervisor._state, "unreachable", None)
    finds = []
    monkeypatch.setattr(client, "find", lambda *a, **k: finds.append(1))
    monkeypatch.setattr(supervisor.time, "sleep", lambda s: None)
    monkeypatch.setattr(guard, "virtualized", lambda path: "redirected (test)")
    assert supervisor.ensure(wait_s=30) is None and len(finds) == 1              # the first look only: no wait
    assert "redirected (test)" in supervisor._state["last_error"]
    monkeypatch.setattr(guard, "virtualized", lambda path: None)
    monkeypatch.setitem(supervisor._state, "unreachable", None)
    assert supervisor.ensure(wait_s=0.05) is None                                  # not redirected: waits once ...
    finds.clear()
    assert supervisor.ensure(wait_s=30) is None and len(finds) == 1              # ... and not again for the same hub
    monkeypatch.setattr(guard, "hub_processes", lambda home: [5151])             # another hub process: waited for again
    finds.clear()
    assert supervisor.ensure(wait_s=0.05) is None and len(finds) > 1


# ---- a hub without its engine ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,bad", [({"llama_server": None}, True), ({"llama_server": ""}, True),
                                        ({"llama_server": "/opt/mm/llama-server"}, False), ({"version": "0.1.0"}, False)])
def test_engine_problem(monkeypatch, status, bad):
    monkeypatch.setattr(client, "call", lambda op, args=None, **k: status if op == "service.status" else pytest.fail(op))
    problem = guard.engine_problem(client.Hub("http://h", "t", "0.1.0"))
    assert bool(problem) is bad
    if bad:
        assert "bundled-engine" in problem


def test_the_supervisor_reports_an_engineless_hub_as_degraded_once_per_hub(monkeypatch):
    hub = client.Hub("http://127.0.0.1:2", "t", "0.1.0")
    monkeypatch.setattr(client, "find", lambda *a, **k: hub)
    calls = []
    monkeypatch.setattr(guard, "engine_problem", lambda h: calls.append(h) or "no engine")
    supervisor._state.pop("engine_checked", None)
    supervisor.tick()
    supervisor.tick()
    assert len(calls) == 1 and supervisor.status()["degraded"] == "no engine"


# ---- too little context per request ---------------------------------------------------------------------------------------------
def test_context_problem():
    small = [{"id": CODER, "serving": {"n_ctx": 8192, "slots": 2}}]           # the live hub on 2026-10-19: 4096 a request
    assert "4096" in guard.context_problem(small, CODER, 16384)
    assert guard.context_problem([{"id": CODER, "serving": {"n_ctx_per_slot": 32768, "slots": 1}}], CODER, 16384) is None
    assert guard.context_problem([{"id": CODER}], CODER, 16384) is None              # not reported: no claim
    assert guard.context_problem(small, "another-model", 16384) is None


def test_the_conductor_refuses_a_model_with_too_little_context_before_the_worker_starts(monkeypatch, tmp_path):
    from bot.conductor import llm, run

    worker = run.AgentLoopWorker(tmp_path, drive=lambda *a: "", context_cfg={"min_context": 16384})
    monkeypatch.setattr(client, "models", lambda hub=None: [{"id": CODER, "serving": {"n_ctx": 8192, "slots": 2}}])
    with pytest.raises(llm.WorkerModelError, match="4096 context tokens per request"):
        worker._check_context("modelmistress/" + CODER, CODER)
    assert llm.WorkerModelError.release_task is True                 # handed back, not failed
    monkeypatch.setattr(client, "models", lambda hub=None: [{"id": CODER, "serving": {"n_ctx": 32768, "slots": 1}}])
    worker._check_context("modelmistress/" + CODER, CODER)
    monkeypatch.setattr(client, "models", lambda hub=None: pytest.fail("checked once per model"))
    worker._check_context("modelmistress/" + CODER, CODER)
    worker._check_context("ollama/qwen3:8b", "qwen3:8b")              # not a hub model: nothing to check


def test_nothing_here_changes_anything():
    """Each guard only answers; none starts, stops or writes (but the redirect probe, which removes its own empty file)."""
    import inspect

    code = open(guard.__file__, encoding="utf-8").read().split('"""', 2)[2].replace(inspect.getsource(guard._probe), "")
    for word in ("subprocess", ".kill(", ".terminate(", "write_text", "unlink(", "config.set"):
        assert word not in code, word
