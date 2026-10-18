"""Swarm leaf workers always run their shell in the native container sandbox, whatever the global setting says.

The Windows tests run for real (AppContainer + Job Object). The wiring tests run the real run_batch with a fake model
backend and record the sandbox config a worker's turn sees.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from bot import bot_instances
from bot.agent_runtime import sandbox, subagents, toolspec, tools
from bot.backends.base import BackendResult
from bot.config import config
from tests.test_sandbox_offline import CURL, WINDOWS, _Listener

windows_only = pytest.mark.skipif(not WINDOWS, reason="AppContainers only exist on Windows")


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("leaf-confinement-test")
    from bot.agent_runtime import shell
    shell._jobs.clear()
    yield
    toolspec.session_var.reset(token)
    shell._jobs.clear()


@pytest.fixture
def global_cfg(monkeypatch):
    """The conductor's global sandbox config: deliberately unconfined."""
    values: dict = {"backend": "local", "network": "allow"}
    monkeypatch.setattr(sandbox, "_global_config", lambda: values)
    return values


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def run_shell(ws, command):
    return asyncio.run(tools.execute_tool("run_shell", {"command": command}, workspace=ws))


# ---- the override itself ------------------------------------------------------------
def test_without_the_override_the_global_config_is_untouched(global_cfg):
    assert sandbox.backend() == "local" and sandbox.network() == "allow"
    assert sandbox._config() == global_cfg


def test_the_override_confines_and_is_undone_on_exit(global_cfg):
    with sandbox.confined_override():
        cfg = sandbox._config()
        assert cfg["backend"] == "windows_job" and cfg["network"] == "none" and cfg["env"] == {"mode": "secrets"}
        assert sandbox.network() == "none"
        if WINDOWS:
            assert sandbox.backend() == "windows_job"
    assert sandbox.backend() == "local" and sandbox.network() == "allow"


def test_the_override_keeps_other_global_keys_but_wins_on_conflicts(global_cfg):
    global_cfg.update(backend="docker", network="allow", windows_job={"active_process_limit": 7})
    with sandbox.confined_override():
        cfg = sandbox._config()
        assert cfg["windows_job"] == {"active_process_limit": 7}
        assert cfg["backend"] == "windows_job" and cfg["network"] == "none"
    assert sandbox._config()["backend"] == "docker"


def test_the_override_does_not_leak_into_a_sibling_context(global_cfg):
    async def main():
        async def confined():
            with sandbox.confined_override():
                await asyncio.sleep(0.05)
                return sandbox.network()

        async def plain():
            await asyncio.sleep(0.02)
            return sandbox.network()

        return await asyncio.gather(confined(), plain())

    assert asyncio.run(main()) == ["none", "allow"]


# ---- real confinement (Windows) -----------------------------------------------------
@windows_only
class TestConfinedShell:
    @pytest.fixture(autouse=True)
    def _confined(self, global_cfg):
        with sandbox.confined_override():
            yield

    def test_a_write_in_the_workspace_succeeds(self, ws):
        out = run_shell(ws, "echo inside-ok> note.txt")
        assert "[exit code 0]" in out
        assert (ws / "note.txt").read_text().strip() == "inside-ok"

    def test_a_write_into_the_user_profile_is_denied(self, ws):
        name = f"PWNED-{uuid.uuid4().hex[:8]}"
        target = os.path.join(os.environ["USERPROFILE"], ".ssh", name)
        try:
            run_shell(ws, f'echo pwned> "{target}"')
            assert not os.path.exists(target), "a confined worker wrote into the user profile"
        finally:
            if os.path.exists(target):
                os.remove(target)

    def test_a_write_to_a_path_outside_the_workspace_is_denied(self, ws, tmp_path):
        target = tmp_path / "outside-write.txt"
        run_shell(ws, f'echo pwned> "{target}"')
        assert not target.exists()

    def test_a_secret_outside_the_workspace_cannot_be_read(self, ws, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("top-secret-contents")
        assert "top-secret-contents" not in run_shell(ws, f'type "{secret}"')

    @pytest.mark.skipif(not CURL, reason="curl.exe is not on this Windows")
    def test_loopback_is_unreachable(self, ws):
        lst = _Listener()
        try:
            out = run_shell(ws, f"curl.exe -sS -m 5 http://127.0.0.1:{lst.port}/")
            assert lst.connections == 0, f"a confined worker reached a loopback server: {out!r}"
            assert "[exit code 0]" not in out
        finally:
            lst.close()

    @pytest.mark.skipif(not CURL, reason="curl.exe is not on this Windows")
    def test_the_internet_is_unreachable(self, ws):
        assert "[exit code 0]" not in run_shell(ws, "curl.exe -sS -m 5 -o NUL https://example.com")


# ---- the wiring in run_batch ----------------------------------------------------------
class _Probe:
    """A fake model backend that records the sandbox config in force while its turn runs."""

    name, model = "native_agent", "fake"

    def __init__(self):
        self.seen: list[dict] = []

    async def ask(self, prompt, *, context=None, timeout_s=30):
        await asyncio.sleep(0)                                  # a real turn awaits; the context must survive it
        self.seen.append(dict(sandbox._config()))
        return BackendResult(text="done", tokens=None, raw=None)


@pytest.fixture
def parent(temp_db, monkeypatch):
    monkeypatch.setattr(config, "_data", {"agent_runtime": {}, "native_agent": {}})
    return bot_instances.create_instance(
        name="manager", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[111], enabled=False)


def _batch(monkeypatch, parent, probe, tmp_path, **kw):
    monkeypatch.setattr(subagents, "_resolve_inherited_backend", lambda pid: probe)
    return asyncio.run(subagents.run_batch(
        [{"goal": "one"}, {"goal": "two"}], parent_instance_id=parent, workspace=tmp_path, **kw))


def test_a_leaf_worker_runs_confined_whatever_the_global_default(parent, monkeypatch, tmp_path, global_cfg):
    probe = _Probe()
    _batch(monkeypatch, parent, probe, tmp_path)                # role defaults to leaf
    assert len(probe.seen) == 2
    for cfg in probe.seen:
        assert cfg["backend"] == "windows_job" and cfg["network"] == "none" and cfg["env"] == {"mode": "secrets"}
    assert sandbox._config() == global_cfg                      # reset once the workers finished


def test_an_orchestrator_worker_keeps_the_global_default(parent, monkeypatch, tmp_path, global_cfg):
    probe = _Probe()
    _batch(monkeypatch, parent, probe, tmp_path, role="orchestrator")
    assert len(probe.seen) == 2
    for cfg in probe.seen:
        assert cfg == global_cfg


def test_the_override_is_reset_even_when_a_leaf_fails(parent, monkeypatch, tmp_path, global_cfg):
    class Boom(_Probe):
        async def ask(self, prompt, *, context=None, timeout_s=30):
            self.seen.append(dict(sandbox._config()))
            raise RuntimeError("model fell over")

    probe = Boom()
    _batch(monkeypatch, parent, probe, tmp_path)
    assert probe.seen and all(c["network"] == "none" for c in probe.seen)
    assert sandbox._config() == global_cfg
