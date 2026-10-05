//! Global keyboard shortcuts and the app-level actions the tray menu and
//! those shortcuts share — so ABP is reachable from anywhere, even with its
//! window minimized or hidden to the tray.
//!
//! Shortcuts come from the official Tauri v2 `global-shortcut` plugin.
//! Every press brings the window up (restoring it from minimized/tray-hidden
//! and focusing it) and emits `abp://action` with `{"action": "<id>"}`; the
//! frontend owns what those ids actually do.
//!
//! Bindings are user-configurable: `GET /api/shortcuts` is consulted when the
//! server answers it, and the built-in defaults are used for everything the
//! server doesn't (or can't) say. Anything unusable — an unknown action id, an
//! accelerator the OS parser rejects, two actions on one key, a key another
//! application already owns — is reported as a [`Conflict`] and skipped rather
//! than being fatal: a shortcut that can't be registered must never stop the
//! app from starting.

use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState as KeyState};

use crate::tray;

/// The one event every shortcut and every tray action emits. The frontend
/// listens on this and switches on `action` (docs/shortcuts.md is the
/// contract; the ids below are its source of truth).
pub const ACTION_EVENT: &str = "abp://action";

/// How often the background thread re-reads `GET /api/shortcuts`. Long
/// enough not to add noticeable traffic to a server that is already serving
/// the dashboard, short enough that a binding change takes effect while the
/// user is still looking at the app.
const POLL_INTERVAL: Duration = Duration::from_secs(30);

/// One action and the key combination it gets when nothing says otherwise.
///
/// Every default is on Ctrl+Alt rather than a bare letter or Ctrl+Shift: a
/// lone key combination that some other program already owns fails to
/// register, and the more modifiers a binding uses the less chance it has of
/// colliding with a running application. Ctrl+Alt in particular is very
/// rarely taken on Windows (Alt+Ctrl+<letter> is a Windows shortcut family,
/// but the AltGr composition Ctrl+Alt is not one it reserves for itself).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct ActionSpec {
    pub id: &'static str,
    pub label: &'static str,
    pub default_accelerator: &'static str,
}

pub const TOGGLE: &str = "app.toggle";
pub const NEW_CHAT: &str = "chat.new";
pub const PALETTE: &str = "palette.open";
pub const QUICK_SEARCH: &str = "search.quick";

pub const ACTIONS: [ActionSpec; 4] = [
    ActionSpec {
        id: TOGGLE,
        label: "Show / hide Agentic Bot Platform",
        default_accelerator: "Ctrl+Alt+A",
    },
    ActionSpec {
        id: NEW_CHAT,
        label: "New chat",
        default_accelerator: "Ctrl+Alt+N",
    },
    ActionSpec {
        id: PALETTE,
        label: "Open the command palette",
        default_accelerator: "Ctrl+Alt+K",
    },
    ActionSpec {
        id: QUICK_SEARCH,
        label: "Quick search",
        default_accelerator: "Ctrl+Alt+Space",
    },
];

pub fn spec(id: &str) -> Option<&'static ActionSpec> {
    ACTIONS.iter().find(|a| a.id == id)
}

/// A configured action → accelerator pair, exactly as it crosses the API
/// boundary and gets persisted/reported back to the frontend.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Binding {
    pub action: String,
    pub accelerator: String,
}

/// Why a binding is not active. Every one of these is survivable: the rest of
/// the set still registers and the app still starts.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Conflict {
    pub action: String,
    pub accelerator: String,
    pub reason: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct RegistrationReport {
    pub registered: Vec<Binding>,
    pub conflicts: Vec<Conflict>,
}

/// The event payload: `{"action": "<id>"}`, and nothing else the frontend
/// doesn't already know from [`spec`].
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ActionPayload {
    pub action: String,
}

impl ActionPayload {
    pub fn new(action: &str) -> Self {
        Self {
            action: action.to_string(),
        }
    }
}

pub fn default_bindings() -> Vec<Binding> {
    ACTIONS
        .iter()
        .map(|a| Binding {
            action: a.id.to_string(),
            accelerator: a.default_accelerator.to_string(),
        })
        .collect()
}

/// Parses one accelerator into the plugin's key combination. Rejects an empty
/// string explicitly rather than letting the parser decide what that means.
pub fn parse_accelerator(text: &str) -> Result<Shortcut, String> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Err("empty shortcut".to_string());
    }
    trimmed.parse::<Shortcut>().map_err(|e| format!("{e}"))
}

/// The canonical spelling of an accelerator ("ctrl+alt+a" and "Ctrl+Alt+A"
/// are the same key), which is what duplicate detection compares. Falls back
/// to the trimmed text for something unparseable, so a broken config still
/// gets reported rather than silently deduplicated against a valid one.
pub fn canonical(text: &str) -> String {
    match parse_accelerator(text) {
        Ok(shortcut) => shortcut.to_string(),
        Err(_) => text.trim().to_string(),
    }
}

/// Reads whatever shape `GET /api/shortcuts` happens to answer with into
/// (action, accelerator) pairs. A flat object, a `{"shortcuts": {...}}`
/// wrapper and a list of `{action, accelerator|accel|key}` objects are all
/// accepted: the endpoint is owned by the server side, which is free to pick
/// whichever of those it finds easiest to serialize, and this app must not
/// break when it picks a different one.
pub fn bindings_from_json(body: &Value) -> Vec<(String, String)> {
    fn object_entries(map: &serde_json::Map<String, Value>) -> Vec<(String, String)> {
        map.iter()
            .filter_map(|(action, value)| Some((action.clone(), value.as_str()?.to_string())))
            .collect()
    }
    let mut out = Vec::new();
    match body {
        Value::Object(map) => {
            if let Some(Value::Object(inner)) = map.get("shortcuts") {
                out.extend(object_entries(inner));
            } else if let Some(Value::Array(list)) = map.get("bindings") {
                out.extend(bindings_from_list(list));
            } else {
                out.extend(object_entries(map));
            }
        }
        Value::Array(list) => out.extend(bindings_from_list(list)),
        _ => {}
    }
    out
}

fn bindings_from_list(list: &[Value]) -> Vec<(String, String)> {
    list.iter()
        .filter_map(|entry| {
            let action = entry.get("action").or_else(|| entry.get("id"))?.as_str()?;
            let accelerator = ["accelerator", "accel", "key", "binding", "shortcut"]
                .iter()
                .find_map(|k| entry.get(k).and_then(|v| v.as_str()))?;
            Some((action.to_string(), accelerator.to_string()))
        })
        .collect()
}

/// The binding set to actually register: defaults, overlaid with whatever the
/// server supplied, with every unusable entry reported instead of applied.
///
/// Dropping a conflicting binding keeps the *first* action in [`ACTIONS`]
/// order — the default set has no duplicates, so this only ever bites on a
/// hand-edited or user-chosen config, and there the more fundamental action
/// (show/hide) winning is the more useful outcome.
pub fn resolve(configured: &[(String, String)]) -> (Vec<Binding>, Vec<Conflict>) {
    let mut conflicts = Vec::new();
    let mut chosen: Vec<Binding> = default_bindings();

    for (action, accelerator) in configured {
        let Some(action_spec) = spec(action) else {
            conflicts.push(Conflict {
                action: action.clone(),
                accelerator: accelerator.clone(),
                reason: format!("unknown action id '{action}'"),
            });
            continue;
        };
        if let Err(reason) = parse_accelerator(accelerator) {
            conflicts.push(Conflict {
                action: action.clone(),
                accelerator: accelerator.clone(),
                reason,
            });
            continue;
        }
        match chosen.iter_mut().find(|b| b.action == action_spec.id) {
            Some(slot) => slot.accelerator = accelerator.clone(),
            None => chosen.push(Binding {
                action: action.clone(),
                accelerator: accelerator.clone(),
            }),
        }
    }

    // Duplicate detection, in ACTIONS order. Walking the documented order (not
    // the order the config listed them in) is what makes the winner
    // deterministic no matter how the JSON was written.
    let mut registered: Vec<Binding> = Vec::new();
    for action in ACTIONS {
        let Some(binding) = chosen.iter().find(|b| b.action == action.id) else {
            continue;
        };
        let key = canonical(&binding.accelerator);
        if let Some(previous) = registered.iter().find(|b| canonical(&b.accelerator) == key) {
            conflicts.push(Conflict {
                action: binding.action.clone(),
                accelerator: binding.accelerator.clone(),
                reason: format!("already bound to '{}' on the same keys", previous.action),
            });
            continue;
        }
        registered.push(binding.clone());
    }
    (registered, conflicts)
}

/// Brings the window up for an action and tells the frontend about it.
///
/// `app.toggle` is the one action that can *hide* the window again — every
/// other one means "I want ABP right now", so they always show and focus it.
/// The event is emitted either way: the frontend needs to know the toggle
/// happened even when the window it was listening in just went away.
pub fn dispatch(app: &AppHandle, action: &str) {
    if action == TOGGLE {
        if tray::window_hidden(app) {
            tray::show_main(app);
        } else {
            tray::hide_to_tray(app);
        }
    } else {
        tray::show_main(app);
    }
    let _ = app.emit(ACTION_EVENT, ActionPayload::new(action));
}

/// The same thing the tray menu does, callable from a `#[tauri::command]` —
/// so the frontend's own shortcut-recording UI and the tray can never drift.
pub fn trigger(app: &AppHandle, action: &str) -> Result<(), String> {
    if spec(action).is_none() {
        return Err(format!("unknown action id '{action}'"));
    }
    dispatch(app, action);
    Ok(())
}

/// Registers one already-resolved binding. The plugin marshals onto the main
/// thread and *waits* for it, so this must never be called from the main
/// thread — every caller below goes through `register_all`, which hops to a
/// worker first.
fn register_one(app: &AppHandle, binding: &Binding) -> Result<(), Conflict> {
    let shortcut =
        parse_accelerator(&binding.accelerator).map_err(|reason| conflict_for(binding, reason))?;
    let action = binding.action.clone();
    app.global_shortcut()
        .on_shortcut(shortcut, move |app, _shortcut, event| {
            if event.state() == KeyState::Pressed {
                dispatch(app, &action);
            }
        })
        .map_err(|e| conflict_for(binding, format!("the system refused this shortcut ({e})")))
}

fn conflict_for(binding: &Binding, reason: String) -> Conflict {
    Conflict {
        action: binding.action.clone(),
        accelerator: binding.accelerator.clone(),
        reason,
    }
}

/// Replaces the whole registered set with `bindings` and reports what
/// happened. Off the main thread by construction, and safe to call repeatedly
/// (that's the re-register-on-change path).
pub fn register_all(app: &AppHandle, bindings: Vec<Binding>) -> RegistrationReport {
    let handle = app.clone();
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let manager = handle.global_shortcut();
        let _ = manager.unregister_all();
        let mut report = RegistrationReport::default();
        for binding in bindings {
            match register_one(&handle, &binding) {
                Ok(()) => report.registered.push(binding),
                Err(conflict) => report.conflicts.push(conflict),
            }
        }
        let _ = tx.send(report);
    });
    rx.recv().unwrap_or_default()
}

/// What the app currently has registered, plus everything it had to skip.
/// Named `ShortcutRegistry` rather than `ShortcutState` because the plugin
/// already exports a `ShortcutState` (key-down vs key-up) that this file also
/// uses.
#[derive(Debug, Default)]
pub struct ShortcutRegistry {
    report: std::sync::Mutex<RegistrationReport>,
}

impl ShortcutRegistry {
    pub fn report(&self) -> RegistrationReport {
        self.report.lock().map(|r| r.clone()).unwrap_or_default()
    }
    fn store(&self, report: RegistrationReport) {
        if let Ok(mut guard) = self.report.lock() {
            *guard = report;
        }
    }
}

#[tauri::command]
pub fn get_shortcut_bindings(state: tauri::State<ShortcutRegistry>) -> RegistrationReport {
    state.report()
}

/// The actions this build supports, with their labels and default bindings —
/// so a frontend settings screen renders exactly what the app can act on
/// rather than its own hardcoded copy that can drift from this list.
#[tauri::command]
pub fn get_shortcut_actions() -> Vec<ActionSpec> {
    ACTIONS.to_vec()
}

/// Applies a new set of bindings from the frontend and re-registers. Only
/// entries for actions this build knows about are honoured; the rest come back
/// as conflicts rather than as a silent no-op.
#[tauri::command]
pub fn set_shortcut_bindings(
    app: AppHandle,
    state: tauri::State<ShortcutRegistry>,
    bindings: Vec<Binding>,
) -> RegistrationReport {
    let (resolved, conflicts) = resolve(
        &bindings
            .iter()
            .map(|b| (b.action.clone(), b.accelerator.clone()))
            .collect::<Vec<_>>(),
    );
    let mut report = register_all(&app, resolved);
    report.conflicts.extend(conflicts);
    publish(&app, &report);
    state.store(report.clone());
    report
}

/// Surfaces registration problems: on stderr for the log file the app already
/// writes, and as an event so a frontend that asks for the current bindings
/// (or listens for this) can tell the user which one is dead and why.
fn publish(app: &AppHandle, report: &RegistrationReport) {
    for conflict in &report.conflicts {
        eprintln!(
            "[agentic-bot-platform] shortcut {} ({}) is not active: {}",
            conflict.action, conflict.accelerator, conflict.reason
        );
    }
    let _ = app.emit("shortcut-conflicts", report.conflicts.clone());
}

/// Where ABP keeps its data (the SQLite database, attachments, logs) — the
/// "Open data folder" target. Same root the Python side uses
/// (bot/db.py's `PROJECT_ROOT / "data"`), created on demand so the very first
/// click before anything has written there still lands somewhere real.
pub fn data_dir(project_root: &Path) -> std::path::PathBuf {
    let dir = project_root.join("data");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// Opens `dir` in the platform's file manager. Every spawn goes through
/// `no_window()`: this app has no console of its own, and an Explorer/Finder
/// launch without that flag would flash one on the user's desktop.
pub fn open_in_file_manager(dir: &Path) -> Result<(), String> {
    if !dir.is_dir() {
        return Err(format!("{} is not a folder", dir.display()));
    }
    let mut cmd = if cfg!(target_os = "windows") {
        let mut cmd = Command::new("explorer");
        cmd.arg(dir);
        cmd
    } else if cfg!(target_os = "macos") {
        let mut cmd = Command::new("open");
        cmd.arg(dir);
        cmd
    } else {
        let mut cmd = Command::new("xdg-open");
        cmd.arg(dir);
        cmd
    };
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    crate::no_window(&mut cmd)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("could not open {}: {e}", dir.display()))
}

/// ABP's emergency stop, called straight against the server's own endpoint
/// (POST /api/sandbox/estop) so it works from the tray with no window and no
/// frontend involved. Returns how many sandbox cells died.
pub fn engage_emergency_stop(port: u16, token: Option<&str>) -> Result<usize, String> {
    let url = format!("http://127.0.0.1:{port}/api/sandbox/estop");
    // The body is written and the reply parsed by hand rather than with
    // ureq's own send_json/into_json: those are behind this crate's `json`
    // feature, which is deliberately off (see Cargo.toml) — this app makes
    // two small calls, not a general HTTP client.
    let mut request = ureq::post(&url)
        .timeout(Duration::from_secs(5))
        .set("Content-Type", "application/json");
    if let Some(token) = token.filter(|t| !t.is_empty()) {
        request = request.set("X-Dashboard-Token", token);
    }
    let response = request
        .send_string(r#"{"reason": "the tray menu's emergency stop"}"#)
        .map_err(|e| format!("emergency stop request failed: {e}"))?;
    let text = response
        .into_string()
        .map_err(|e| format!("could not read the emergency stop response: {e}"))?;
    let body: Value = serde_json::from_str(&text)
        .map_err(|e| format!("emergency stop returned unreadable JSON: {e}"))?;
    Ok(body.get("count").and_then(|c| c.as_u64()).unwrap_or(0) as usize)
}

/// Reads the user's bindings from `GET /api/shortcuts`. Returns None when the
/// server isn't up or doesn't have that route — the caller then uses the
/// defaults, which is also what a fresh install gets.
fn fetch_configured(port: u16, token: Option<&str>) -> Option<Value> {
    let url = format!("http://127.0.0.1:{port}/api/shortcuts");
    let mut request = ureq::get(&url).timeout(Duration::from_secs(2));
    if let Some(token) = token.filter(|t| !t.is_empty()) {
        request = request.set("X-Dashboard-Token", token);
    }
    let text = request.call().ok()?.into_string().ok()?;
    serde_json::from_str(&text).ok()
}

/// Registers `bindings`, tells the app about it, and returns the report.
pub fn apply(app: &AppHandle, bindings: Vec<Binding>) -> RegistrationReport {
    let report = register_all(app, bindings);
    publish(app, &report);
    if let Some(state) = app.try_state::<ShortcutRegistry>() {
        state.store(report.clone());
    }
    report
}

/// Applies the defaults, then keeps them in step with the server: the first
/// successful read that differs from what is registered re-registers. Called
/// once from `setup`.
///
/// The whole body runs on a worker thread, not inline: `setup()` itself runs
/// on the main thread, and registering a shortcut has to be marshalled ONTO
/// the main thread (see `register_all`) — so doing it inline would block the
/// one thread that has to service it, and the app would hang at startup.
///
/// `resolve_token` is called at most once, on that same worker, and only
/// before the first poll: `GET /api/shortcuts` is behind the same
/// `X-Dashboard-Token` check as the rest of the API, so without it a server
/// that HAS the route would answer 401 forever and the user's own bindings
/// would silently never apply. It is a callback rather than a value because
/// reading the token means a python round trip, which has no business on the
/// startup path at all — the defaults are what a fresh install has anyway.
pub fn init<F>(app: &AppHandle, port: u16, resolve_token: F)
where
    F: Fn() -> Option<String> + Send + 'static,
{
    let handle = app.clone();
    thread::spawn(move || {
        apply(&handle, default_bindings());
        let token = resolve_token();
        loop {
            thread::sleep(POLL_INTERVAL);
            let Some(body) = fetch_configured(port, token.as_deref()) else {
                continue;
            };
            let (bindings, _) = resolve(&bindings_from_json(&body));
            let changed = match handle.try_state::<ShortcutRegistry>() {
                Some(state) => state.report().registered != bindings,
                None => true,
            };
            if changed {
                apply(&handle, bindings);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_default_set_is_the_four_documented_actions() {
        let defaults = default_bindings();
        let ids: Vec<&str> = defaults.iter().map(|b| b.action.as_str()).collect();
        assert_eq!(
            ids,
            vec!["app.toggle", "chat.new", "palette.open", "search.quick"]
        );
        let accelerators: Vec<&str> = defaults.iter().map(|b| b.accelerator.as_str()).collect();
        assert_eq!(
            accelerators,
            vec!["Ctrl+Alt+A", "Ctrl+Alt+N", "Ctrl+Alt+K", "Ctrl+Alt+Space"]
        );
    }

    #[test]
    fn every_default_accelerator_is_something_the_os_parser_accepts() {
        for binding in default_bindings() {
            assert!(
                parse_accelerator(&binding.accelerator).is_ok(),
                "{}",
                binding.accelerator
            );
        }
    }

    #[test]
    fn the_defaults_have_no_conflicts_among_themselves() {
        let (bindings, conflicts) = resolve(&[]);
        assert_eq!(bindings, default_bindings());
        assert!(conflicts.is_empty(), "{conflicts:?}");
    }

    #[test]
    fn accelerators_parse_case_insensitively_and_tolerate_spacing() {
        for spelling in ["Ctrl+Alt+A", "ctrl+alt+a", " Ctrl + Alt + A "] {
            assert_eq!(canonical(spelling), canonical("Ctrl+Alt+A"), "{spelling}");
        }
    }

    #[test]
    fn a_bare_key_is_accepted_but_an_empty_or_nonsense_one_is_not() {
        assert!(parse_accelerator("F9").is_ok());
        assert_eq!(
            parse_accelerator("   ").unwrap_err(),
            "empty shortcut".to_string()
        );
        assert!(parse_accelerator("Ctrl+Alt+Nonsense").is_err());
        // Modifiers must come first and there can be only one main key.
        assert!(parse_accelerator("A+Ctrl").is_err());
        assert!(parse_accelerator("Ctrl+A+B").is_err());
    }

    #[test]
    fn a_configured_binding_replaces_the_default_for_that_action_only() {
        let (bindings, conflicts) = resolve(&[("chat.new".into(), "Ctrl+Shift+N".into())]);
        assert!(conflicts.is_empty());
        assert_eq!(
            bindings,
            vec![
                Binding {
                    action: "app.toggle".into(),
                    accelerator: "Ctrl+Alt+A".into()
                },
                Binding {
                    action: "chat.new".into(),
                    accelerator: "Ctrl+Shift+N".into()
                },
                Binding {
                    action: "palette.open".into(),
                    accelerator: "Ctrl+Alt+K".into()
                },
                Binding {
                    action: "search.quick".into(),
                    accelerator: "Ctrl+Alt+Space".into()
                },
            ]
        );
    }

    #[test]
    fn an_unknown_action_or_an_unparseable_accelerator_is_reported_not_applied() {
        let (bindings, conflicts) = resolve(&[
            ("app.explode".into(), "Ctrl+Alt+X".into()),
            ("palette.open".into(), "Ctrl+Alt+Nonsense".into()),
        ]);
        // Both bad entries leave the defaults in place.
        assert_eq!(bindings, default_bindings());
        assert_eq!(conflicts.len(), 2);
        assert!(conflicts[0].reason.contains("unknown action id"));
        assert_eq!(conflicts[1].action, "palette.open");
        assert!(!conflicts[1].reason.is_empty());
    }

    #[test]
    fn two_actions_on_the_same_keys_are_a_conflict_and_the_first_one_keeps_them() {
        let (bindings, conflicts) = resolve(&[
            ("chat.new".into(), "Ctrl+Alt+A".into()),
            ("search.quick".into(), "ctrl+alt+a".into()),
        ]);
        // app.toggle already owns Ctrl+Alt+A and comes first in ACTIONS
        // order, so it keeps the chord. Both configured actions lose theirs
        // (case-insensitively — "ctrl+alt+a" is the same keys) and are
        // reported rather than silently stealing the winner's shortcut.
        let toggle = bindings
            .iter()
            .find(|b| b.action == "app.toggle")
            .expect("app.toggle");
        assert_eq!(toggle.accelerator, "Ctrl+Alt+A");
        let ids: Vec<&str> = bindings.iter().map(|b| b.action.as_str()).collect();
        assert_eq!(ids, vec!["app.toggle", "palette.open"], "{bindings:?}");
        assert_eq!(conflicts.len(), 2);
        for conflict in &conflicts {
            assert!(
                conflict.reason.contains("already bound to 'app.toggle'"),
                "{conflict:?}"
            );
        }
    }

    #[test]
    fn the_action_payload_is_just_the_action_id() {
        let json = serde_json::to_value(ActionPayload::new("search.quick")).unwrap();
        assert_eq!(json, serde_json::json!({"action": "search.quick"}));
    }

    #[test]
    fn every_documented_action_id_is_known_and_an_invented_one_is_not() {
        for action in ACTIONS {
            assert_eq!(spec(action.id).map(|s| s.id), Some(action.id));
        }
        assert!(spec("app.explode").is_none());
        assert!(spec("").is_none());
    }

    #[test]
    fn the_shortcuts_endpoint_is_read_in_whichever_shape_it_answers() {
        let expected = vec![("app.toggle".to_string(), "Ctrl+Alt+J".to_string())];
        // A flat {action: accelerator} object.
        assert_eq!(
            bindings_from_json(&serde_json::json!({"app.toggle": "Ctrl+Alt+J"})),
            expected
        );
        // The same under a "shortcuts" wrapper.
        assert_eq!(
            bindings_from_json(&serde_json::json!({"shortcuts": {"app.toggle": "Ctrl+Alt+J"}})),
            expected
        );
        // A list of objects.
        assert_eq!(
            bindings_from_json(
                &serde_json::json!([{"action": "app.toggle", "accelerator": "Ctrl+Alt+J"}])
            ),
            expected
        );
        assert_eq!(
            bindings_from_json(&serde_json::json!([{"id": "app.toggle", "key": "Ctrl+Alt+J"}])),
            expected
        );
    }

    #[test]
    fn an_unexpected_shortcuts_endpoint_shape_yields_the_defaults_rather_than_nothing() {
        for body in [
            serde_json::json!(null),
            serde_json::json!("Ctrl+Alt+A"),
            serde_json::json!({"app.toggle": 7}),
        ] {
            let (bindings, conflicts) = resolve(&bindings_from_json(&body));
            assert_eq!(bindings, default_bindings(), "{body}");
            assert!(conflicts.is_empty(), "{body}");
        }
    }

    #[test]
    fn data_dir_lives_under_the_project_root_and_is_created() {
        let root = std::env::temp_dir().join(format!("abp-shortcut-data-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let dir = data_dir(&root);
        assert_eq!(dir, root.join("data"));
        // Created on demand, so the very first tray click before anything has
        // written there still lands in a real folder.
        assert!(dir.is_dir());
        // Only the refusal is exercised here: actually opening Explorer would
        // put a window on the user's desktop in the middle of a test run.
        let missing = open_in_file_manager(&root.join("nope")).unwrap_err();
        assert!(missing.contains("is not a folder"), "{missing}");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_action_list_a_settings_screen_renders_carries_its_labels() {
        let json = serde_json::to_value(ACTIONS.to_vec()).unwrap();
        let rows = json.as_array().unwrap();
        assert_eq!(rows.len(), 4);
        assert_eq!(
            rows[0],
            serde_json::json!({
                "id": "app.toggle",
                "label": "Show / hide Agentic Bot Platform",
                "default_accelerator": "Ctrl+Alt+A",
            })
        );
        for row in rows {
            assert!(!row["label"].as_str().unwrap().is_empty());
        }
    }

    #[test]
    fn bindings_survive_the_json_round_trip_the_frontend_uses() {
        let original = Binding {
            action: "search.quick".into(),
            accelerator: "Ctrl+Alt+Space".into(),
        };
        let json = serde_json::to_string(&original).unwrap();
        assert_eq!(
            json,
            r#"{"action":"search.quick","accelerator":"Ctrl+Alt+Space"}"#
        );
        assert_eq!(serde_json::from_str::<Binding>(&json).unwrap(), original);
    }

    #[test]
    fn a_state_with_nothing_registered_yet_reports_an_empty_set() {
        let state = ShortcutRegistry::default();
        assert_eq!(state.report(), RegistrationReport::default());
    }
}
