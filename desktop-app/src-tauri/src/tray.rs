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
use tauri::{AppHandle, Emitter, Manager, Window, WindowEvent, Wry};

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

/// Only the items whose enabled/checked state has to track the window or the
/// saved settings are kept here; the plain action items (new chat, quick
/// search, emergency stop, restart, open data folder) are owned by the menu
/// for as long as it lives and are never mutated.
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

pub fn window_hidden(app: &AppHandle) -> bool {
    match app.get_webview_window(MAIN) {
        Some(w) => !w.is_visible().unwrap_or(true) || w.is_minimized().unwrap_or(false),
        None => true,
    }
}

/// Keeps the menu honest: "Show" is only enabled when there is something to
/// show, "Minimize to tray" only when the window is up, and the two toggle
/// items mirror the saved settings. The action items (new chat, quick search)
/// are always live: they bring the window up themselves, so they are the way
/// back in when nothing else on the menu is.
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

/// The tray's server-backed items report their outcome the same way every
/// other part of this app does: a `server-log` line, which the frontend's log
/// panel already shows whether or not a window is up. Never a dialog, and
/// never a panic — a tray click must never be able to take the app down.
pub(crate) fn log_line(app: &AppHandle, line: String) {
    let state = app.state::<crate::ServerState>();
    crate::push_backlog(
        state.inner(),
        crate::LogLine {
            stream: "stderr".into(),
            line: line.clone(),
        },
    );
    let _ = app.emit(
        "server-log",
        crate::LogLine {
            stream: "stderr".into(),
            line,
        },
    );
}

/// Emergency stop, straight from the tray: kills every non-persistent sandbox
/// cell and leaves the daemons and the dashboard alone (the server's own
/// POST /api/sandbox/estop semantics). Needs the dashboard token, resolved
/// through the same `bot.envfile --print-token` path lib.rs's
/// `get_dashboard_token` command uses rather than any second copy of the
/// secret-handling logic.
fn emergency_stop(app: &AppHandle) {
    let handle = app.clone();
    // Shelling out to python for the token takes a moment; keep it off the
    // menu's own thread so the tray doesn't freeze while it happens.
    std::thread::spawn(move || {
        let token = crate::dashboard_token(&handle).ok().flatten();
        match crate::shortcuts::engage_emergency_stop(crate::DASHBOARD_PORT, token.as_deref()) {
            Ok(0) => log_line(
                &handle,
                "emergency stop: nothing was running to stop.".into(),
            ),
            Ok(count) => log_line(
                &handle,
                format!("emergency stop: stopped {count} running cell(s); services left alone."),
            ),
            Err(e) => log_line(&handle, format!("emergency stop failed: {e}")),
        }
    });
}

/// Restart the managed bot.main. Same code path as the UI's restart button —
/// deliberately: two implementations of "restart the server" is exactly how
/// they drift.
fn restart_server(app: &AppHandle) {
    let handle = app.clone();
    std::thread::spawn(move || {
        let state = handle.state::<crate::ServerState>();
        if let Err(e) = crate::restart_server_command(&handle, &state) {
            log_line(&handle, format!("restart failed: {e}"));
        }
    });
}

/// Opens ABP's data folder (the SQLite database, attachments and logs live
/// there) in Explorer/Finder — the quickest route to "show me the file" when
/// somebody is reporting a bug.
fn open_data_folder(app: &AppHandle) {
    match crate::data_dir(app) {
        Ok(dir) => {
            if let Err(e) = crate::shortcuts::open_in_file_manager(&dir) {
                log_line(app, format!("could not open the data folder: {e}"));
            }
        }
        Err(e) => log_line(app, format!("could not find the data folder: {e}")),
    }
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
    let new_chat = MenuItem::with_id(app, "new_chat", "New chat", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let quick_search = MenuItem::with_id(app, "quick_search", "Quick search", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let estop = MenuItem::with_id(
        app,
        "emergency_stop",
        "Emergency stop (stop running work)",
        true,
        None::<&str>,
    )
    .map_err(|e| e.to_string())?;
    let restart = MenuItem::with_id(app, "restart_server", "Restart server", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    let open_data = MenuItem::with_id(
        app,
        "open_data_folder",
        "Open data folder",
        true,
        None::<&str>,
    )
    .map_err(|e| e.to_string())?;
    let quit =
        MenuItem::with_id(app, "quit", "Quit", true, None::<&str>).map_err(|e| e.to_string())?;
    let sep1 = PredefinedMenuItem::separator(app).map_err(|e| e.to_string())?;
    let sep2 = PredefinedMenuItem::separator(app).map_err(|e| e.to_string())?;
    let sep3 = PredefinedMenuItem::separator(app).map_err(|e| e.to_string())?;
    let menu = Menu::with_items(
        app,
        &[
            &show,
            &hide,
            &sep1,
            &new_chat,
            &quick_search,
            &sep2,
            &estop,
            &restart,
            &open_data,
            &sep3,
            &close_check,
            &min_check,
            &quit,
        ],
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
            // These go through the same dispatcher the global shortcuts use,
            // so a tray click and a keypress are literally the same action.
            "new_chat" => crate::shortcuts::dispatch(app, crate::shortcuts::NEW_CHAT),
            "quick_search" => crate::shortcuts::dispatch(app, crate::shortcuts::QUICK_SEARCH),
            "emergency_stop" => emergency_stop(app),
            "restart_server" => restart_server(app),
            "open_data_folder" => open_data_folder(app),
            "close_to_tray" | "minimize_to_tray" => {
                let mut s = app.state::<TrayState>().settings();
                if event.id().as_ref() == "close_to_tray" {
                    s.close_to_tray = !s.close_to_tray;
                } else {
                    s.minimize_to_tray = !s.minimize_to_tray;
                }
                let _ = apply_settings(app, s);
            }
            other => {
                // The menu is built from a fixed list, so nothing else should
                // arrive here; routing it anyway means a future item added to
                // the menu cannot end up as a click that does nothing.
                if let Err(e) = trigger_tray_action(app.clone(), other.to_string()) {
                    log_line(app, e);
                }
            }
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

/// Same actions the tray menu offers, callable from the frontend so the
/// Settings UI can offer the same buttons without reimplementing them.
#[tauri::command]
pub fn trigger_tray_action(app: AppHandle, action: String) -> Result<(), String> {
    match TrayAction::parse(&action)? {
        TrayAction::Show => show_main(&app),
        TrayAction::Hide => hide_to_tray(&app),
        TrayAction::Quit => quit_app(&app),
        TrayAction::EmergencyStop => emergency_stop(&app),
        TrayAction::RestartServer => restart_server(&app),
        TrayAction::OpenDataFolder => open_data_folder(&app),
        TrayAction::Shortcut(id) => crate::shortcuts::trigger(&app, &id)?,
    }
    Ok(())
}

/// What a tray menu id means, kept apart from the side effects so the id
/// vocabulary is one list instead of two `match`es that can drift. Ids the
/// tray owns itself come first; anything else is a global-shortcut action id
/// and is handed to the shortcuts module.
#[derive(Debug, PartialEq, Eq)]
pub enum TrayAction {
    Show,
    Hide,
    Quit,
    EmergencyStop,
    RestartServer,
    OpenDataFolder,
    Shortcut(String),
}

impl TrayAction {
    pub fn parse(id: &str) -> Result<Self, String> {
        Ok(match id {
            "show" => Self::Show,
            "hide" => Self::Hide,
            "quit" => Self::Quit,
            "emergency_stop" => Self::EmergencyStop,
            "restart_server" => Self::RestartServer,
            "open_data_folder" => Self::OpenDataFolder,
            other => {
                // An id this build has never heard of is an error, not a
                // silent no-op: a Settings UI offering a button for an action
                // a newer frontend knows about must hear that it did nothing.
                if crate::shortcuts::spec(other).is_none() {
                    return Err(format!("unknown action id '{other}'"));
                }
                Self::Shortcut(other.to_string())
            }
        })
    }
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
    fn every_tray_menu_item_resolves_and_a_global_shortcut_id_falls_through() {
        assert_eq!(TrayAction::parse("show").unwrap(), TrayAction::Show);
        assert_eq!(TrayAction::parse("hide").unwrap(), TrayAction::Hide);
        assert_eq!(TrayAction::parse("quit").unwrap(), TrayAction::Quit);
        assert_eq!(
            TrayAction::parse("emergency_stop").unwrap(),
            TrayAction::EmergencyStop
        );
        assert_eq!(
            TrayAction::parse("restart_server").unwrap(),
            TrayAction::RestartServer
        );
        assert_eq!(
            TrayAction::parse("open_data_folder").unwrap(),
            TrayAction::OpenDataFolder
        );
        // "New chat" and "Quick search" are the same actions the shortcuts
        // fire, so they resolve to the shortcut ids rather than to a second
        // implementation of either.
        assert_eq!(
            TrayAction::parse("chat.new").unwrap(),
            TrayAction::Shortcut("chat.new".into())
        );
        assert_eq!(
            TrayAction::parse("search.quick").unwrap(),
            TrayAction::Shortcut("search.quick".into())
        );
    }

    #[test]
    fn an_action_nobody_knows_is_refused_rather_than_silently_ignored() {
        assert!(TrayAction::parse("app.explode").is_err());
        assert!(TrayAction::parse("").is_err());
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
