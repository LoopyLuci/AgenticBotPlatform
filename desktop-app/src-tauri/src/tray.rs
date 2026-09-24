//! System-tray integration: the app keeps running (and keeps the bot server
//! alive) with its window hidden, and the tray menu is the way back.
//!
//! Behaviour
//! - Closing the window hides it to the tray instead of quitting (setting
//!   `close_to_tray`, default on). Minimizing does the same when
//!   `minimize_to_tray` is on (default on).
//! - Tray menu: "Show", "Minimize to tray", the two toggles above, "Quit".
//!   Left-click / double-click on the icon shows the window.
//! - "Quit" is the only thing that really exits: it flags `quitting` so the
//!   close handler stops intercepting, then `app.exit(0)` runs the normal
//!   exit path, whose `RunEvent::Exit` handler stops the bot server, the
//!   Android build and the terminal (see lib.rs).
//! - A second launch (single-instance plugin) shows the existing window
//!   instead of starting a second app fighting over the dashboard port.
//! - Settings persist in `tray.json` in the app config dir; a missing or
//!   corrupt file falls back to the defaults, never an error.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Window, WindowEvent, Wry};

const MAIN: &str = "main";
const TRAY_ID: &str = "abp-tray";

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct TraySettings {
    pub close_to_tray: bool,
    pub minimize_to_tray: bool,
    pub start_minimized: bool,
}

impl Default for TraySettings {
    fn default() -> Self {
        Self {
            close_to_tray: true,
            minimize_to_tray: true,
            start_minimized: false,
        }
    }
}

#[derive(Debug, PartialEq)]
pub enum CloseAction {
    HideToTray,
    Exit,
}

/// What the window's close button should do. Quitting from the tray always wins.
pub fn close_action(settings: &TraySettings, quitting: bool) -> CloseAction {
    if !quitting && settings.close_to_tray {
        CloseAction::HideToTray
    } else {
        CloseAction::Exit
    }
}

pub fn load_settings(path: &Path) -> TraySettings {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
}

pub fn save_settings(path: &Path, settings: &TraySettings) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let raw = serde_json::to_string_pretty(settings).map_err(|e| e.to_string())?;
    // Write-then-rename so a crash mid-write can't leave a truncated file.
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, raw).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())
}

struct Items {
    show: MenuItem<Wry>,
    hide: MenuItem<Wry>,
    close_check: CheckMenuItem<Wry>,
    min_check: CheckMenuItem<Wry>,
}

pub struct TrayState {
    settings: Mutex<TraySettings>,
    items: Mutex<Option<Items>>,
    pub quitting: AtomicBool,
}

impl TrayState {
    pub fn new() -> Self {
        Self {
            settings: Mutex::new(TraySettings::default()),
            items: Mutex::new(None),
            quitting: AtomicBool::new(false),
        }
    }

    pub fn settings(&self) -> TraySettings {
        self.settings.lock().map(|s| *s).unwrap_or_default()
    }
}

fn settings_path(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|d| d.join("tray.json"))
}

/// Loads persisted settings into state. Call once at startup.
pub fn init_settings(app: &AppHandle) {
    let state = app.state::<TrayState>();
    if let Some(path) = settings_path(app) {
        if let Ok(mut guard) = state.settings.lock() {
            *guard = load_settings(&path);
        }
    }
}

pub fn apply_settings(app: &AppHandle, new: TraySettings) -> Result<TraySettings, String> {
    let state = app.state::<TrayState>();
    if let Some(path) = settings_path(app) {
        save_settings(&path, &new)?;
    }
    if let Ok(mut guard) = state.settings.lock() {
        *guard = new;
    }
    sync_menu(app);
    Ok(new)
}

fn window_hidden(app: &AppHandle) -> bool {
    match app.get_webview_window(MAIN) {
        Some(w) => !w.is_visible().unwrap_or(true) || w.is_minimized().unwrap_or(false),
        None => true,
    }
}

/// Keeps the menu honest: "Show" is only enabled when there is something to
/// show, "Minimize to tray" only when the window is up, and the two toggle
/// items mirror the saved settings.
pub fn sync_menu(app: &AppHandle) {
    let state = app.state::<TrayState>();
    let settings = state.settings();
    let hidden = window_hidden(app);
    if let Ok(guard) = state.items.lock() {
        if let Some(items) = guard.as_ref() {
            let _ = items.show.set_enabled(hidden);
            let _ = items.hide.set_enabled(!hidden);
            let _ = items.close_check.set_checked(settings.close_to_tray);
            let _ = items.min_check.set_checked(settings.minimize_to_tray);
        }
    };
}

/// Session-only (never saved): used when the tray could not be created, so
/// hiding the window can't strand the user without a way back.
pub fn disable_hiding(app: &AppHandle) {
    if let Ok(mut guard) = app.state::<TrayState>().settings.lock() {
        guard.close_to_tray = false;
        guard.minimize_to_tray = false;
    }
}

pub fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
    sync_menu(app);
}

pub fn hide_to_tray(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.hide();
    }
    sync_menu(app);
}

pub fn quit_app(app: &AppHandle) {
    app.state::<TrayState>()
        .quitting
        .store(true, Ordering::SeqCst);
    app.exit(0);
}

fn toggle(app: &AppHandle) {
    if window_hidden(app) {
        show_main(app);
    } else {
        hide_to_tray(app);
    }
}

/// Builds the tray icon and menu. Failure is reported, not fatal: on a
/// desktop with no tray host the app must still open its window and close
/// normally, so `close_to_tray` is switched off for the session in that case
/// (otherwise the user could hide a window they can never get back).
pub fn build(app: &AppHandle) -> Result<(), String> {
    let settings = app.state::<TrayState>().settings();
    let show = MenuItem::with_id(app, "show", "Show Agentic Bot Platform", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let hide = MenuItem::with_id(app, "hide", "Minimize to tray", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let close_check = CheckMenuItem::with_id(
        app,
        "close_to_tray",
        "Close button minimizes to tray",
        true,
        settings.close_to_tray,
        None::<&str>,
    )
    .map_err(|e| e.to_string())?;
    let min_check = CheckMenuItem::with_id(
        app,
        "minimize_to_tray",
        "Minimize button goes to tray",
        true,
        settings.minimize_to_tray,
        None::<&str>,
    )
    .map_err(|e| e.to_string())?;
    let quit =
        MenuItem::with_id(app, "quit", "Quit", true, None::<&str>).map_err(|e| e.to_string())?;
    let sep1 = PredefinedMenuItem::separator(app).map_err(|e| e.to_string())?;
    let sep2 = PredefinedMenuItem::separator(app).map_err(|e| e.to_string())?;
    let menu = Menu::with_items(
        app,
        &[&show, &hide, &sep1, &close_check, &min_check, &sep2, &quit],
    )
    .map_err(|e| e.to_string())?;

    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip("Agentic Bot Platform")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_main(app),
            "hide" => hide_to_tray(app),
            "quit" => quit_app(app),
            "close_to_tray" | "minimize_to_tray" => {
                let mut s = app.state::<TrayState>().settings();
                if event.id().as_ref() == "close_to_tray" {
                    s.close_to_tray = !s.close_to_tray;
                } else {
                    s.minimize_to_tray = !s.minimize_to_tray;
                }
                let _ = apply_settings(app, s);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } => toggle(tray.app_handle()),
            TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } => show_main(tray.app_handle()),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app).map_err(|e| e.to_string())?;

    *app.state::<TrayState>()
        .items
        .lock()
        .map_err(|_| "poisoned")? = Some(Items {
        show,
        hide,
        close_check,
        min_check,
    });
    sync_menu(app);
    Ok(())
}

/// Window-event hook. Returns nothing: closing is intercepted here, and the
/// real shutdown work happens on `RunEvent::Exit`.
pub fn handle_window_event(window: &Window, event: &WindowEvent) {
    let app = window.app_handle();
    if window.label() != MAIN {
        return;
    }
    match event {
        WindowEvent::CloseRequested { api, .. } => {
            let state = app.state::<TrayState>();
            let quitting = state.quitting.load(Ordering::SeqCst);
            if close_action(&state.settings(), quitting) == CloseAction::HideToTray {
                api.prevent_close();
                hide_to_tray(app);
            }
        }
        WindowEvent::Resized(_) => {
            let settings = app.state::<TrayState>().settings();
            if settings.minimize_to_tray && window.is_minimized().unwrap_or(false) {
                let _ = window.unminimize();
                hide_to_tray(app);
            } else {
                sync_menu(app);
            }
        }
        WindowEvent::Focused(_) => sync_menu(app),
        _ => {}
    }
}

/// True when the app was asked to start hidden (saved setting or `--minimized`,
/// which is what an autostart shortcut should pass).
pub fn should_start_hidden(app: &AppHandle) -> bool {
    app.state::<TrayState>().settings().start_minimized
        || std::env::args().any(|a| a == "--minimized")
}

#[tauri::command]
pub fn get_tray_settings(state: tauri::State<TrayState>) -> TraySettings {
    state.settings()
}

#[tauri::command]
pub fn set_tray_settings(app: AppHandle, settings: TraySettings) -> Result<TraySettings, String> {
    apply_settings(&app, settings)
}

#[tauri::command]
pub fn hide_main_window(app: AppHandle) {
    hide_to_tray(&app);
}

#[tauri::command]
pub fn show_main_window(app: AppHandle) {
    show_main(&app);
}

#[tauri::command]
pub fn quit_app_command(app: AppHandle) {
    quit_app(&app);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("abp-tray-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d.join("tray.json")
    }

    #[test]
    fn defaults_hide_on_close_and_minimize_but_do_not_start_hidden() {
        let s = TraySettings::default();
        assert!(s.close_to_tray && s.minimize_to_tray && !s.start_minimized);
    }

    #[test]
    fn closing_hides_to_tray_unless_the_user_quit_or_turned_it_off() {
        let on = TraySettings::default();
        assert_eq!(close_action(&on, false), CloseAction::HideToTray);
        assert_eq!(close_action(&on, true), CloseAction::Exit);
        let off = TraySettings {
            close_to_tray: false,
            ..on
        };
        assert_eq!(close_action(&off, false), CloseAction::Exit);
    }

    #[test]
    fn settings_round_trip_and_survive_a_missing_or_corrupt_file() {
        let path = temp("roundtrip");
        assert_eq!(load_settings(&path), TraySettings::default());
        let s = TraySettings {
            close_to_tray: false,
            minimize_to_tray: true,
            start_minimized: true,
        };
        save_settings(&path, &s).unwrap();
        assert_eq!(load_settings(&path), s);
        std::fs::write(&path, "{ not json").unwrap();
        assert_eq!(load_settings(&path), TraySettings::default());
    }

    #[test]
    fn a_file_from_an_older_version_missing_new_keys_still_loads() {
        let path = temp("partial");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"close_to_tray": false}"#).unwrap();
        let s = load_settings(&path);
        assert!(!s.close_to_tray && s.minimize_to_tray);
    }
}
