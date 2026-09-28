"""The ABP Agents page's Editors tab in a real browser: it shows the VS Code extension's state and installs it
with one click. VS Code itself is a stand-in `code` program here (test_editor_integrations.py runs the real
one). Skipped when Playwright or an installed Edge/Chrome is not available."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import zipfile

import pytest

pytestmark = pytest.mark.xdist_group("real-browser")
pytest.importorskip("playwright")

import uvicorn  # noqa: E402

from bot import editor_integrations as ed  # noqa: E402
from bot import envfile  # noqa: E402
from bot.dashboard.server import build_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stand_in_code(tmp_path) -> str:
    """Lists the extension once `--install-extension` has been run."""
    marker = tmp_path / "installed"
    script = tmp_path / "fake_code.py"
    script.write_text(
        "import pathlib, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if '--install-extension' in sys.argv: marker.write_text('x')\n"
        "elif marker.exists(): print('agenticbotplatform.abp-vscode@0.3.0')\n")
    if os.name == "nt":
        launcher = tmp_path / "code.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        launcher = tmp_path / "code"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    return str(launcher)


@pytest.fixture
def dashboard(monkeypatch, temp_db, tmp_path):
    monkeypatch.setenv("DASHBOARD_TOKEN", "unused-dashboard-token")
    from bot import providers
    from bot.config import ConfigManager

    empty = tmp_path / "providers.yaml"
    empty.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=empty))
    root = tmp_path / "abp"
    (root / "integrations").mkdir(parents=True)
    with zipfile.ZipFile(root / "integrations" / "abp-vscode.vsix", "w") as z:
        z.writestr("extension/package.json", '{"version": "0.3.0"}')
    monkeypatch.setattr(envfile, "CODE_ROOT", root)
    monkeypatch.setattr(ed, "code_cli", lambda: _stand_in_code(tmp_path))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}", tmp_path
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        for channel in ("msedge", "chrome", ""):
            try:
                b = p.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
                break
            except Exception:  # noqa: BLE001 - try the next browser
                continue
        else:
            pytest.skip("no Edge, Chrome or Playwright Chromium available")
        yield b
        b.close()


# text_content, not inner_text: the dashboard skips painting sections off-screen (content-visibility), and
# innerText is empty for unpainted content.
def test_the_editors_tab_installs_the_vscode_extension(dashboard, browser):
    url, tmp_path = dashboard
    page = browser.new_page(bypass_csp=True)
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(url + "/", wait_until="load")
    page.wait_for_function("window.abpAgents && window.abpAgents.state.loaded", timeout=30_000)
    page.locator('#sidenav a[href="#agents"]').click()
    page.locator('#agents-tabs [data-tab="editors"]').click(timeout=30_000)
    install = page.locator("#ag-vscode-install")
    install.wait_for(timeout=30_000)
    assert "Install in VS Code" in install.text_content()
    assert "Not installed. Version 0.3.0 is ready to install." in page.locator("#agents-body").text_content()
    assert "-m abp_acp --model auto" in page.locator("#ag-acp-command").text_content()
    install.click()
    page.locator("#agents-body .chip.good").wait_for(timeout=30_000)
    body = page.locator("#agents-body").text_content()
    assert "Installed" in body and "version 0.3.0" in body
    assert (tmp_path / "installed").exists(), "the button ran VS Code's installer"
    assert page.locator("#ag-vscode-install").text_content() == "Reinstall"
    assert not errors, errors
