"""Editor integrations (bot/editor_integrations.py): the install pointer, the VS Code extension's status and
one-click install. A stand-in `code` program is used for the logic; a live test installs the real packaged
extension with the real VS Code CLI into a throwaway extensions folder."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from bot import editor_integrations as ed
from bot import envfile

ROOT = Path(__file__).resolve().parent.parent


def _vsix(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("extension/package.json", json.dumps({"name": "abp-vscode", "version": version}))
    return path


def _fake_code(tmp_path: Path, listing: str, exit_code: int = 0) -> tuple[str, Path]:
    """A stand-in `code` that records its arguments and prints `listing`."""
    log = tmp_path / "code-calls.jsonl"
    script = tmp_path / "fake_code.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"print({listing!r})\n"
        f"sys.exit({exit_code})\n")
    if os.name == "nt":
        launcher = tmp_path / "code.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        launcher = tmp_path / "code"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    return str(launcher), log


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def code_root(tmp_path, monkeypatch):
    root = tmp_path / "abp"
    (root / "abp_acp").mkdir(parents=True)
    (root / "abp_acp" / "__main__.py").write_text("")
    monkeypatch.setattr(envfile, "CODE_ROOT", root)
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path / "state")
    return root


# ---- the pointer editors read -------------------------------------------------------------------
def test_every_start_records_where_this_abp_is(code_root, tmp_path):
    written = ed.register_install()
    assert written == Path(os.environ["ABP_INSTALL_POINTER"])
    data = json.loads(written.read_text())
    assert data["code_root"] == str(code_root) and data["state_root"] == str(tmp_path / "state")
    assert data["python"] == sys.executable and data["pid"] == os.getpid()


def test_no_pointer_for_a_copy_editors_cannot_start(code_root):
    shutil.rmtree(code_root / "abp_acp")
    assert ed.register_install() is None
    assert not Path(os.environ["ABP_INSTALL_POINTER"]).exists()


def test_an_unwritable_pointer_never_stops_the_server(code_root, monkeypatch, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    monkeypatch.setenv("ABP_INSTALL_POINTER", str(blocker / "install.json"))     # its parent is a file
    assert ed.register_install() is None


def test_the_tests_never_write_the_real_pointer():
    assert Path(os.environ["ABP_INSTALL_POINTER"]) != Path.home() / ".abp" / "install.json"


# ---- versions and status ------------------------------------------------------------------------
def test_the_bundled_package_and_its_version(code_root):
    assert ed.bundled_vsix() is None
    dev = code_root / "integrations" / "vscode" / "dist"
    dev.mkdir(parents=True)
    _vsix(dev / "abp-vscode.vsix", "0.1.0")
    assert ed.bundled_vsix() == dev / "abp-vscode.vsix"
    shipped = _vsix(code_root / "integrations" / "abp-vscode.vsix", "0.2.0")
    assert ed.bundled_vsix() == shipped, "the installer's copy wins over a dev build"
    assert ed.vsix_version(shipped) == "0.2.0"
    (code_root / "broken.vsix").write_text("not a zip")
    assert ed.vsix_version(code_root / "broken.vsix") is None


def test_version_comparison():
    assert ed._newer("0.10.0", "0.9.9") and ed._newer("1.0.0", "0.99.0")
    assert not ed._newer("0.1.0", "0.1.0") and not ed._newer("garbage", "0.1.0")


def test_status_reports_an_available_update(code_root, tmp_path, monkeypatch):
    _vsix(code_root / "integrations" / "abp-vscode.vsix", "0.2.0")
    cli, _log = _fake_code(tmp_path, "ms-python.python@2026.1.0\nAgenticBotPlatform.abp-vscode@0.1.0")
    monkeypatch.setattr(ed, "code_cli", lambda: cli)
    st = ed.status()
    assert st["vscode"]["installed"] == "0.1.0" and st["vscode"]["bundled"] == "0.2.0"
    assert st["vscode"]["update_available"] is True
    assert st["acp_command"] == [sys.executable, "-m", "abp_acp", "--model", "auto"]


def test_status_without_vscode(code_root, monkeypatch):
    monkeypatch.setattr(ed, "code_cli", lambda: None)
    st = ed.status()
    assert st["vscode"]["cli"] is None and st["vscode"]["installed"] is None and not st["vscode"]["update_available"]


# ---- install ------------------------------------------------------------------------------------
def test_install_runs_vscode_with_the_bundled_package_and_records_the_pointer(code_root, tmp_path, monkeypatch):
    vsix = _vsix(code_root / "integrations" / "abp-vscode.vsix", "0.1.0")
    cli, log = _fake_code(tmp_path, "agenticbotplatform.abp-vscode@0.1.0")
    monkeypatch.setattr(ed, "code_cli", lambda: cli)
    st = ed.install_vscode()
    assert ["--install-extension", str(vsix), "--force"] in _calls(log)
    assert st["vscode"]["installed"] == "0.1.0"
    assert Path(os.environ["ABP_INSTALL_POINTER"]).is_file()


def test_install_explains_what_is_missing(code_root, tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "code_cli", lambda: None)
    with pytest.raises(ed.EditorError, match="Install 'code' command in PATH"):
        ed.install_vscode()
    cli, _log = _fake_code(tmp_path, "")
    monkeypatch.setattr(ed, "code_cli", lambda: cli)
    with pytest.raises(ed.EditorError, match="npm run package"):
        ed.install_vscode()


def test_a_failed_install_is_reported(code_root, tmp_path, monkeypatch):
    _vsix(code_root / "integrations" / "abp-vscode.vsix", "0.1.0")
    cli, _log = _fake_code(tmp_path, "Failed Installing Extensions", exit_code=1)
    monkeypatch.setattr(ed, "code_cli", lambda: cli)
    with pytest.raises(ed.EditorError, match="Failed Installing Extensions"):
        ed.install_vscode()


def test_the_api(code_root, tmp_path, monkeypatch, temp_db):
    from fastapi.testclient import TestClient

    from bot.dashboard.server import build_app

    _vsix(code_root / "integrations" / "abp-vscode.vsix", "0.1.0")
    cli, log = _fake_code(tmp_path, "")
    monkeypatch.setattr(ed, "code_cli", lambda: cli)
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    client = TestClient(build_app())
    h = {"X-Dashboard-Token": "t" * 48}
    assert client.get("/api/editors/status").status_code == 401
    assert client.post("/api/editors/vscode/install").status_code == 401
    st = client.get("/api/editors/status", headers=h).json()
    assert st["vscode"]["bundled"] == "0.1.0" and st["vscode"]["installed"] is None
    assert client.post("/api/editors/vscode/install", headers=h).status_code == 200
    assert any("--install-extension" in c for c in _calls(log))
    monkeypatch.setattr(ed, "code_cli", lambda: None)
    r = client.post("/api/editors/vscode/install", headers=h)
    assert r.status_code == 409 and "code" in r.json()["detail"]


# ---- live: the real VS Code CLI, into a throwaway extensions folder -------------------------------
_REAL_CLI = ed.code_cli()
_REAL_VSIX = ROOT / "integrations" / "vscode" / "dist" / "abp-vscode.vsix"


@pytest.mark.skipif(not _REAL_CLI or not _REAL_VSIX.is_file(),
                    reason="needs VS Code's code CLI and a packaged extension (npm run package in integrations/vscode)")
def test_the_real_vscode_installs_the_real_package(tmp_path, monkeypatch):
    extensions = tmp_path / "extensions"
    extensions.mkdir()
    if os.name == "nt":
        wrapper = tmp_path / "code-isolated.cmd"
        wrapper.write_text(f'@call "{_REAL_CLI}" %* --extensions-dir "{extensions}"\r\n')
    else:
        wrapper = tmp_path / "code-isolated"
        wrapper.write_text(f'#!/bin/sh\nexec "{_REAL_CLI}" "$@" --extensions-dir "{extensions}"\n')
        wrapper.chmod(0o755)
    monkeypatch.setattr(ed, "code_cli", lambda: str(wrapper))
    monkeypatch.setattr(envfile, "CODE_ROOT", ROOT)
    assert ed.status()["vscode"]["installed"] is None
    st = ed.install_vscode()
    expected = json.loads((ROOT / "integrations" / "vscode" / "package.json").read_text())["version"]
    assert st["vscode"]["installed"] == expected == st["vscode"]["bundled"]
    assert any(p.name.startswith(ed.EXTENSION_ID) for p in extensions.iterdir())
    r = subprocess.run([str(wrapper), "--list-extensions"], capture_output=True, text=True, timeout=90)
    assert ed.EXTENSION_ID in r.stdout.lower()
