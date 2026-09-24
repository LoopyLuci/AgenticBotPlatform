"""The desktop app's tray + single-instance wiring stays in place (Rust behaviour itself is covered by
cargo tests in src/tray.rs and was verified live: five simultaneous extra launches all exit, one instance stays)."""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "desktop-app"
LIB = (ROOT / "src-tauri/src/lib.rs").read_text(encoding="utf-8")


def test_single_instance_plugin_is_registered_before_anything_else():
    run = LIB[LIB.index("pub fn run()"):]
    assert "tauri_plugin_single_instance::init" in run
    assert run.index("tauri_plugin_single_instance::init") < run.index(".manage(")
    assert "tauri-plugin-single-instance" in (ROOT / "src-tauri/Cargo.toml").read_text(encoding="utf-8")


def test_only_a_real_exit_stops_the_bot_server():
    # Closing hides to the tray (the server must keep running); shutdown work lives on RunEvent::Exit.
    assert "RunEvent::Exit" in LIB and "stop_bot_server" in LIB[LIB.index("RunEvent::Exit"):]
    assert "CloseRequested" not in LIB[LIB.index("pub fn run()"):]


def test_tray_commands_are_permitted_and_used_by_the_ui():
    caps = json.loads((ROOT / "src-tauri/capabilities/default.json").read_text(encoding="utf-8"))["permissions"]
    toml = (ROOT / "src-tauri/permissions/app-commands.toml").read_text(encoding="utf-8")
    js = (ROOT / "ui/main.js").read_text(encoding="utf-8")
    for cmd, perm in (("get_tray_settings", "allow-get-tray-settings"), ("set_tray_settings", "allow-set-tray-settings"),
                      ("hide_main_window", "allow-hide-main-window"), ("quit_app_command", "allow-quit-app-command"),
                      ("show_main_window", "allow-show-main-window")):
        assert perm in caps and f'"{cmd}"' in toml
    assert "get_tray_settings" in js and "quit_app_command" in js
    assert 'id="tray-card"' in (ROOT / "ui/index.html").read_text(encoding="utf-8")
