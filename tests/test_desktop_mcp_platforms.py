"""bot/desktop.py: where Claude Desktop's MCP config lives on each platform, and what happens where there is none.
A Linux run of the suite found `abp mcp list` failing outright there (Claude Desktop does not exist on Linux)."""
from __future__ import annotations

import platform
from pathlib import Path

import pytest

from bot import desktop


def test_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert desktop._mcp_config_path() == tmp_path / "Claude" / "claude_desktop_config.json"


def test_macos_uses_application_support(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert desktop._mcp_config_path() == tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def test_without_claude_desktop_there_are_no_servers_and_writes_say_why(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.delenv("APPDATA", raising=False)
    assert desktop.list_mcp_servers() == []
    with pytest.raises(RuntimeError, match="isn't available on this platform"):
        desktop._save_mcp_config({"mcpServers": {}})


def test_a_real_config_is_listed(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "Claude").mkdir()
    (tmp_path / "Claude" / "claude_desktop_config.json").write_text(
        '{"mcpServers": {"a": {"command": "node"}}, "mcpServers_disabled": {"b": {"command": "uvx"}}}', encoding="utf-8")
    assert desktop.list_mcp_servers() == [{"name": "a", "enabled": True, "command": "node"},
                                          {"name": "b", "enabled": False, "command": "uvx"}]
