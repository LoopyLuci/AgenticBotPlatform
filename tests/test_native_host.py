"""bot/native_host.py (the stdio native-messaging protocol and its two operations) and scripts/install_native_host.py
(manifest building and platform-specific paths). Never touches the real Windows registry or a real browser's native-
messaging directories - registry/filesystem writes are exercised only through injected/monkeypatched targets."""
from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

import pytest

from bot import native_host as nh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import install_native_host as inst  # noqa: E402


# ------------------------------------------------------------------------------------------ the wire protocol
def test_read_write_message_round_trip():
    buf = io.BytesIO()
    nh.write_message(buf, {"op": "status", "port": 8787, "running": True})
    buf.seek(0)
    assert nh.read_message(buf) == {"op": "status", "port": 8787, "running": True}


def test_read_message_returns_none_at_a_clean_end_of_stream():
    assert nh.read_message(io.BytesIO(b"")) is None


def test_read_message_returns_none_for_a_truncated_frame():
    buf = io.BytesIO(struct.pack("<I", 100) + b"short")
    assert nh.read_message(buf) is None


def test_read_message_returns_none_for_invalid_json():
    body = b"not json"
    buf = io.BytesIO(struct.pack("<I", len(body)) + body)
    assert nh.read_message(buf) is None


def test_read_message_refuses_an_absurd_length_without_reading_it():
    buf = io.BytesIO(struct.pack("<I", 50 * 1024 * 1024))
    assert nh.read_message(buf) is None


# ------------------------------------------------------------------------------------------ status / launch
def test_status_reports_whether_abp_is_reachable(monkeypatch):
    monkeypatch.setattr(nh, "abp_reachable", lambda port, timeout=nh.HELLO_TIMEOUT_S: port == 8787)
    assert nh.handle({"op": "status", "port": 8787}) == {"op": "status", "port": 8787, "running": True}
    assert nh.handle({"op": "status", "port": 9999}) == {"op": "status", "port": 9999, "running": False}


def test_status_defaults_to_the_default_port():
    assert nh.handle({"op": "status"})["port"] == nh.DEFAULT_PORT


def test_launch_is_a_no_op_when_already_running(monkeypatch):
    monkeypatch.setattr(nh, "abp_reachable", lambda port, timeout=nh.HELLO_TIMEOUT_S: True)
    monkeypatch.setattr(nh, "launch_abp", lambda: pytest.fail("should not launch when already running"))
    assert nh.handle({"op": "launch", "port": 8787}) == {"op": "launch", "port": 8787, "running": True, "started": False}


def test_launch_starts_abp_and_waits_for_it_to_answer(monkeypatch):
    calls = {"n": 0}

    def fake_reachable(port, timeout=nh.HELLO_TIMEOUT_S):
        calls["n"] += 1
        return calls["n"] > 2                                    # not up on the first couple of checks, then it is

    monkeypatch.setattr(nh, "abp_reachable", fake_reachable)
    monkeypatch.setattr(nh, "launch_abp", lambda: (True, "started x"))
    monkeypatch.setattr(nh.time, "sleep", lambda s: None)
    assert nh.handle({"op": "launch", "port": 8787}) == {"op": "launch", "port": 8787, "running": True, "started": True}


def test_launch_reports_a_clear_error_when_there_is_nothing_to_launch(monkeypatch):
    monkeypatch.setattr(nh, "abp_reachable", lambda port, timeout=nh.HELLO_TIMEOUT_S: False)
    monkeypatch.setattr(nh, "launch_abp", lambda: (False, "no ABP install was registered"))
    r = nh.handle({"op": "launch", "port": 8787})
    assert r["running"] is False and r["started"] is False and "no ABP install" in r["error"]


def test_launch_reports_timeout_if_it_never_comes_up(monkeypatch):
    monkeypatch.setattr(nh, "abp_reachable", lambda port, timeout=nh.HELLO_TIMEOUT_S: False)
    monkeypatch.setattr(nh, "launch_abp", lambda: (True, "started"))
    monkeypatch.setattr(nh.time, "sleep", lambda s: None)
    monkeypatch.setattr(nh, "LAUNCH_WAIT_S", 0.01)
    r = nh.handle({"op": "launch", "port": 8787})
    assert r["started"] is True and r["running"] is False and "did not answer" in r["error"]


def test_unknown_op_is_reported_not_raised():
    assert nh.handle({"op": "juggle"}) == {"op": "juggle", "error": "unknown op 'juggle'"}


def test_find_launcher_reads_the_installer_marker(tmp_path, monkeypatch):
    fake_file = tmp_path / "bot" / "native_host.py"
    fake_file.parent.mkdir()
    fake_file.write_text("", encoding="utf-8")
    launcher = tmp_path / "abp.exe"
    launcher.write_text("", encoding="utf-8")
    (tmp_path / "native_host_install.json").write_text(json.dumps({"launcher": str(launcher)}), encoding="utf-8")
    monkeypatch.setattr(nh, "__file__", str(fake_file))
    assert nh.find_launcher() == launcher


def test_find_launcher_is_none_without_a_marker(tmp_path, monkeypatch):
    fake_file = tmp_path / "bot" / "native_host.py"
    fake_file.parent.mkdir()
    monkeypatch.setattr(nh, "__file__", str(fake_file))
    assert nh.find_launcher() is None


# ------------------------------------------------------------------------------------------ the installer's pure logic
def test_build_manifest_for_chromium_uses_allowed_origins():
    m = inst.build_manifest(path=Path("/x/wrapper.bat"), chrome_ids=["abc123"], firefox_ids=[])
    assert m["allowed_origins"] == ["chrome-extension://abc123/"]
    assert "allowed_extensions" not in m
    assert m["type"] == "stdio" and m["name"] == inst.HOST_NAME


def test_build_manifest_for_firefox_uses_allowed_extensions():
    m = inst.build_manifest(path=Path("/x/wrapper.sh"), chrome_ids=[], firefox_ids=["abp@example.com"])
    assert m["allowed_extensions"] == ["abp@example.com"]
    assert "allowed_origins" not in m


def test_manifest_paths_differ_by_platform_and_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(inst.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(inst.sys, "platform", "linux")
    linux_paths = {b: inst.manifest_paths(b) for b in ("chrome", "edge", "firefox")}
    assert linux_paths["chrome"][0] == tmp_path / ".config/google-chrome/NativeMessagingHosts" / f"{inst.HOST_NAME}.json"
    assert linux_paths["firefox"][0] == tmp_path / ".mozilla/native-messaging-hosts" / f"{inst.HOST_NAME}.json"
    assert linux_paths["chrome"][0] != linux_paths["edge"][0]

    monkeypatch.setattr(inst.sys, "platform", "darwin")
    mac_paths = inst.manifest_paths("firefox")
    assert mac_paths[0] == tmp_path / "Library/Application Support/Mozilla/NativeMessagingHosts" / f"{inst.HOST_NAME}.json"


def test_install_writes_a_wrapper_and_manifests_without_touching_the_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "ROOT", tmp_path)
    monkeypatch.setattr(inst.sys, "platform", "linux")                    # skips the Windows registry branch entirely
    launcher = tmp_path / "abp.exe"
    launcher.write_text("", encoding="utf-8")
    written = inst.install(chrome_ids=["abc123"], firefox_ids=[], launcher=launcher, browsers=["chrome", "edge"])
    assert len(written) == 2
    for p in written:
        assert p.exists()
        assert json.loads(p.read_text())["allowed_origins"] == ["chrome-extension://abc123/"]
    marker = json.loads((tmp_path / "native_host_install.json").read_text())
    assert marker["launcher"] == str(launcher)
    wrapper = tmp_path / "native_host" / "abp_native_host.sh"
    assert wrapper.exists() and sys.executable in wrapper.read_text()


def test_install_skips_firefox_without_a_firefox_id(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "ROOT", tmp_path)
    monkeypatch.setattr(inst.sys, "platform", "linux")
    launcher = tmp_path / "abp.exe"
    launcher.write_text("", encoding="utf-8")
    written = inst.install(chrome_ids=["abc123"], firefox_ids=[], launcher=launcher, browsers=["chrome", "firefox"])
    assert len(written) == 1                                              # firefox skipped: no firefox id was given


def test_uninstall_removes_what_install_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "ROOT", tmp_path)
    monkeypatch.setattr(inst.sys, "platform", "linux")
    launcher = tmp_path / "abp.exe"
    launcher.write_text("", encoding="utf-8")
    inst.install(chrome_ids=["abc123"], firefox_ids=["abp@example.com"], launcher=launcher, browsers=["chrome", "firefox"])
    inst.uninstall(["chrome", "firefox"])
    assert not (tmp_path / "native_host").exists()
    assert not (tmp_path / "native_host_install.json").exists()
    for p in inst.manifest_paths("chrome") + inst.manifest_paths("firefox"):
        assert not p.exists()
