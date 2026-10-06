"""The feature with the native module switched off.

ABP has to work without `abp_wasm` - on a machine where nobody built it, in a test run that
wants the Python path, and deliberately under `ABP_NATIVE=0`. So the question this file answers
is not "does it work" but "does it *say so* and stay out of the way": no import error from
somewhere deep in a call, no half-installed plugin, no tool the model cannot call.

    ABP_NATIVE=0 python -m pytest tests/test_wasm_disabled.py -q -p no:cacheprovider

The env var is set inside each test rather than by the runner, so this file tests the same
thing whether or not the module happens to be built on the machine running it.
"""

from __future__ import annotations

import importlib
import json
import logging

import pytest

from bot import envfile
from bot.wasm_plugins import manager, runtime, tools


@pytest.fixture(autouse=True)
def native_off(monkeypatch, tmp_path):
    """ABP_NATIVE=0, a throwaway data directory, and the modules reloaded around them.

    The reload is the point: `runtime` decides once at import whether the module is there and
    caches that answer in its log-once set, so a test that only set the variable would be
    testing whatever the previous import decided.
    """
    monkeypatch.setenv("ABP_NATIVE", "0")
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path / "project")
    for module in (tools, manager, runtime):
        importlib.reload(module)
    runtime._logged.clear()  # noqa: SLF001 - the log-once set, which is module state
    runtime.Runtime.reset()
    yield
    for module in (tools, manager, runtime):
        importlib.reload(module)
    runtime.Runtime.reset()


def test_the_feature_reports_itself_off(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="bot.wasm_plugins.runtime"):
        assert runtime.available() is False
        assert runtime.native_disabled() is True
        assert runtime.require() is None
    assert any("ABP_NATIVE=0" in record.getMessage() for record in caplog.records)


def test_the_runtime_refuses_rather_than_import_error():
    with pytest.raises(runtime.WasmUnavailable) as raised:
        runtime.Runtime()
    message = str(raised.value)
    assert "not available" in message
    # The message has to say how to turn it on: a feature that disables itself without saying
    # why is the same as a feature that is broken.
    assert "ABP_NATIVE=0" in message or "build.py" in message


def test_installing_says_the_runtime_is_missing(tmp_path):
    component = tmp_path / "anything.wasm"
    component.write_bytes(b"\x00asm\x01\x00\x00\x00")
    with pytest.raises(manager.WasmPluginError) as raised:
        manager.install(str(component))
    message = str(raised.value)
    assert "native wasm runtime" in message
    # Whatever the reason, the message has to say what to do about it.
    assert "ABP_NATIVE=0" in message or "build.py" in message


def test_nothing_is_installed_and_no_wasm_tools_are_registered(tmp_path):
    component = tmp_path / "anything.wasm"
    component.write_bytes(b"\x00asm\x01\x00\x00\x00")
    with pytest.raises(manager.WasmPluginError):
        manager.install(str(component))
    assert manager.list_plugins() == []
    # Scoped to this subsystem on purpose: the agent's tool list also holds the built-ins and
    # whatever other test modules registered, and those are not this test's business.
    assert tools.registered() == {}
    assert tools.plugin_for_tool("anything_greet") is None
    assert not tools.has_tool("anything_greet")


def test_loading_enabled_at_startup_is_a_no_op(tmp_path):
    """`load_enabled()` is called on every boot. With the feature off it must return, not
    raise - a boot that dies here would be ABP failing to start because an optional component
    was not built."""
    manager.load_enabled()


def test_calling_a_plugin_that_is_not_loaded_says_so(tmp_path):
    with pytest.raises(manager.WasmPluginError) as raised:
        manager.invoke("hello", "greet", {"name": "x"})
    assert "not loaded" in str(raised.value)


def test_the_allowlist_is_still_checked_without_the_runtime(tmp_path):
    """The policy check is Python, so it answers even when there is no engine to enforce it -
    which is what makes it worth having in both places."""
    assert runtime.host_allowed("https://example.com/x", ["example.com"]) is True
    assert runtime.host_allowed("https://example.com/x", []) is False
    assert runtime.host_allowed("https://evil.test/x", ["example.com"]) is False
    assert runtime.host_allowed("file:///etc/passwd", ["example.com"]) is False


def test_tool_arguments_must_be_an_object(tmp_path):
    """Every tool in the world takes an object; wrapping a list would hand the plugin
    something its own schema never described."""
    assert json.loads(runtime.json_args({"name": "x"})) == {"name": "x"}
    assert runtime.json_args(None) == "{}"
    with pytest.raises(runtime.PluginCallError):
        runtime.json_args([1, 2, 3])
    with pytest.raises(runtime.PluginCallError):
        runtime.json_args("not json at all")


def test_a_plugin_store_refuses_a_key_that_is_not_a_plain_key(tmp_path):
    """A key with a path separator is refused rather than sanitised: silently rewriting it
    would mean a plugin writing `a/b` and reading back `a_b` and never finding out why."""
    services = runtime.HostServices("p", tmp_path / "store", network=[])
    for key in ("a/b", "..", ".hidden", ""):
        with pytest.raises(runtime.PolicyDenied):
            services.store_set(key, "x")
    services.store_set("plain", "x")
    assert services.store_get("plain") == "x"
    assert services.store_keys() == ["plain"]
