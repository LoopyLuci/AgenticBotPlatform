"""abp_native as ABP actually uses it: the fast paths behind the modules that call them,
and the switch that turns the compiled extension off.

The property tests in tests/test_native_parity.py hold Rust and Python to the same results;
this file is the other half - that ABP's own code paths still produce the right answers
through whichever implementation is live, and that `ABP_NATIVE=0` really does turn the
extension off (checked in a subprocess, because the choice is made once at import).

Run it both ways: `pytest tests/test_native_integration.py` and
`ABP_NATIVE=0 pytest tests/test_native_integration.py`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import abp_native

REPO = Path(__file__).resolve().parent.parent


def _child(env_extra: dict[str, str], snippet: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(REPO), **env_extra}
    done = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO), env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_the_package_says_which_implementation_is_live():
    expected = "python" if os.environ.get("ABP_NATIVE", "").strip().lower() in ("0", "off", "false", "no") else "rust"
    if not abp_native.rust:
        expected = "python"
    assert abp_native.active() == expected
    assert set(abp_native.describe()) == {
        f"{name}: {abp_native.active()}" for name in
        ("hash_vectors", "normalise_words", "memory_duplicate", "is_secret_name",
         "scrub_credentials", "scrub_pii")}


def test_the_switch_is_read_from_the_environment():
    assert _child({}, "import abp_native; print(abp_native.active())") in ("rust", "python")
    for off in ("0", "off", "false", "NO"):
        assert _child({"ABP_NATIVE": off}, "import abp_native; print(abp_native.active())") == "python"


def test_the_module_can_be_asked_from_the_command_line():
    out = _child({}, "import runpy, sys; sys.argv=['abp_native']; runpy.run_module('abp_native', run_name='__main__')")
    assert out.splitlines()[0] in ("abp_native: rust", "abp_native: python")


def test_the_compiled_extension_is_importable_and_complete():
    import abp_native_core

    assert abp_native_core.vector_dim() == 1024
    assert abp_native_core.build_info()[0]


# ---- through ABP's own code ---------------------------------------------------------------------------- #

def test_the_file_server_vectors_are_the_shape_its_callers_expect():
    import numpy as np

    from bot.fileserver import index

    vectors = index.hash_vectors(["The home server is called Server.", "", "Deploys go through staging."])
    assert vectors.shape == (3, index.DIM)
    assert vectors.dtype == np.float32
    assert index.hash_vectors([]).shape == (0, index.DIM)
    # L2 normalised, so a cosine similarity against them is a dot product.
    norms = np.linalg.norm(vectors, axis=1)
    assert norms[0] == pytest.approx(1.0, abs=1e-6)
    assert norms[1] == pytest.approx(0.0, abs=1e-6)
    # The same sentence, differently cased and punctuated, has to land in the same place.
    near = index.hash_vectors(["the home server is called server!"])[0]
    assert float(vectors[0] @ near) > 0.9


def test_a_memory_is_recognised_as_one_already_stored(temp_db):
    from bot import bot_instances, memory

    instance = bot_instances.create_instance(name="native-perf", platform="app", backend="api",
                                            credentials={}, allowed_user_ids=[], actor="test")
    first = memory.remember_full(instance, "The home server is called Server.")
    again = memory.remember_full(instance, "the home server is called server!")
    assert again["duplicate"] and again["id"] == first["id"]
    different = memory.remember_full(instance, "The home server is called Server 2.")
    assert not different["duplicate"] and different["id"] != first["id"]


def test_reach_the_secret_scan_through_the_secrets_guard():
    from bot.agent_runtime import secrets_guard

    environ = {"DASHBOARD_TOKEN": "a-real-looking-value", "PATH": "not-a-secret", "API_KEY": "42"}
    known = secrets_guard.known_secrets(environ)
    assert known == {"DASHBOARD_TOKEN": "a-real-looking-value"}
    assert secrets_guard.is_secret_name("MY_TOKEN") and not secrets_guard.is_secret_name("PATH")


def test_redaction_end_to_end_through_the_export_path(monkeypatch):
    from bot.agent_runtime import secrets_guard, session_export, trajectory

    monkeypatch.setenv("MY_SERVER_TOKEN", "server-secret-value-123")
    assert "server-secret-value-123" not in secrets_guard.redact("here it is: server-secret-value-123")
    cleaned = session_export._clean("the key sk-abcdefghijklmnopqrstu and the value server-secret-value-123")
    assert "sk-abcdefghij" not in cleaned and "server-secret-value-123" not in cleaned
    assert "[key removed]" in cleaned and "[secret:MY_SERVER_TOKEN]" in cleaned
    masked = trajectory.scrub_pii("ada@example.test, 10.0.0.12, +1 555 123 4567")
    assert masked == "[email], [ip], [phone]"


def test_an_export_of_a_real_session_has_no_secrets_in_it(tmp_path, monkeypatch):
    """The whole export path, not just its pieces: a session rendered to Markdown."""
    from abp_agenteval.scripted import ScriptedTransport
    from abp_agenteval.task import Call, Say
    from bot.agent_runtime import session_export
    from bot.backends.native_backend import NativeAgentBackend

    monkeypatch.setenv("MY_SERVER_TOKEN", "server-secret-value-123")
    (tmp_path / "config.json").write_text('{"owner": "ada@example.test", "key": "server-secret-value-123"}')
    backend = NativeAgentBackend(ScriptedTransport([Call("read_file", {"path": "config.json"}),
                                                    Say("The owner is ada@example.test.")]),
                                 model="m", name="t")
    import asyncio

    asyncio.run(backend.ask("who owns it?", context={"cwd": str(tmp_path), "desktop_session_key": "native-perf",
                                                    "source": "test"}))
    rendered = session_export.export_session("native-perf", "md")
    assert "server-secret-value-123" not in rendered
    # The credential shapes run before the known values, so the shape is what wins here.\n    assert "[removed]" in rendered
