//! Owns the Python bot process end to end: spawns it, streams its stdout/
//! stderr to the frontend as "server-log" events, samples its CPU/RAM as
//! "server-resources" events, and exposes start/stop/restart/status as
//! Tauri commands so the GUI never needs a browser or a terminal outside
//! the app window.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

use serde::Serialize;
use sysinfo::{Pid, ProcessesToUpdate, System};
use tauri::{AppHandle, Emitter, Manager, State};

mod android;
use android::{
    android_env_status, build_android_apk, install_android_apk, list_adb_devices,
    pair_android_device,
};
mod network;
mod updater;
use network::{detect_lan_host, detect_tailscale_host};
use updater::{check_for_update, download_update, install_update};

/// Passed to CreateProcess on Windows so spawning a console app (python.exe,
/// taskkill.exe) never flashes its own console window on top of the GUI —
/// the app is windows_subsystem = "windows" and has no console of its own,
/// so without this every child process would pop one up.
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

/// Applies CREATE_NO_WINDOW on Windows; no-op elsewhere.
pub(crate) fn no_window(cmd: &mut Command) -> &mut Command {
    #[cfg(target_os = "windows")]
    {
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

struct ServerState {
    child: Mutex<Option<Child>>,
    // Every "server-log"/"server-status" event is also mirrored here so a
    // late-attaching frontend listener can catch up. Real gap found live:
    // spawn_internal() runs in Tauri's .setup() hook, which fires well
    // before the frontend's async boot sequence gets around to calling
    // listen('server-log', ...) — a fast-crashing python process (its
    // whole traceback, plus the final "not running" status) could emit
    // and finish well within that window, and Tauri's event system does
    // NOT replay past events to a listener that registers late. Without
    // this, that showed up as "Server process exited" with an empty log
    // panel — the exact symptom, not a hypothetical. Capped so a
    // long-running, chatty server can't grow this unboundedly.
    log_backlog: Mutex<Vec<LogLine>>,
}

const LOG_BACKLOG_CAP: usize = 2000;

fn push_backlog(state: &ServerState, line: LogLine) {
    if let Ok(mut backlog) = state.log_backlog.lock() {
        backlog.push(line);
        let len = backlog.len();
        if len > LOG_BACKLOG_CAP {
            backlog.drain(0..len - LOG_BACKLOG_CAP);
        }
    }
}

#[derive(Clone, Serialize)]
struct LogLine {
    stream: String,
    line: String,
}

#[derive(Clone, Serialize)]
struct ServerStatusPayload {
    running: bool,
    pid: Option<u32>,
}

#[derive(Clone, Serialize)]
struct ResourceSample {
    cpu_percent: f32,
    mem_mb: f64,
}

/// A venv's python.exe on Windows re-execs the real interpreter as a *new*
/// child process rather than replacing itself in place, so `Child::kill()`
/// on the pid we spawned only kills that launcher stub and leaves the real
/// interpreter (and the whole bot process) running as an orphan holding the
/// dashboard port. `taskkill /T` kills the entire process tree instead.
fn terminate_child(mut child: Child) {
    let pid = child.id();
    #[cfg(target_os = "windows")]
    {
        let mut cmd = Command::new("taskkill");
        cmd.args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let _ = no_window(&mut cmd).status();
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = child.kill();
    }
    let _ = child.wait();
}

/// Where the Python side lives: bundled next to the packaged app (resources)
/// in a release build, or the repo's own live tree in a dev build. This is
/// deliberately keyed on `debug_assertions`, not on "does a bot/ folder
/// exist under resource_dir()" — tauri-build's build script copies
/// `bundle.resources` into target/debug/ too (so `cargo tauri dev` behaves
/// like the packaged app), but that copy excludes .env on purpose (it's
/// gitignored, never meant to be bundled), so preferring it during dev
/// silently loses secrets. Debug builds always run against the live repo
/// tree; only a release build reads the bundled copy.
/// A venv's interpreter lives at `.venv/Scripts/python.exe` on Windows but
/// `.venv/bin/python` on Linux/macOS — different layout, not just a file
/// extension difference.
fn venv_python(venv_root: &std::path::Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        venv_root.join(".venv").join("Scripts").join("python.exe")
    } else {
        venv_root.join(".venv").join("bin").join("python")
    }
}

fn resolve_paths(app: &AppHandle) -> Result<(PathBuf, PathBuf), String> {
    if cfg!(debug_assertions) {
        let dev_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .ok_or_else(|| "could not resolve project root".to_string())?
            .to_path_buf();
        let python = venv_python(&dev_root);
        return Ok((dev_root, python));
    }

    let res_dir = app
        .path()
        .resource_dir()
        .map_err(|e| format!("could not resolve resource_dir: {e}"))?;
    let python = venv_python(&res_dir);
    Ok((res_dir, python))
}

fn spawn_internal(app: &AppHandle, state: &State<ServerState>) -> Result<(), String> {
    let mut guard = state
        .child
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    if guard.is_some() {
        return Ok(());
    }

    let (project_root, python) = resolve_paths(app)?;
    if !python.exists() {
        let hint = if cfg!(target_os = "windows") {
            "scripts\\run.ps1"
        } else {
            "scripts/run.sh"
        };
        return Err(format!(
            "python not found at {} — run {hint} once to create the venv",
            python.display()
        ));
    }

    let mut cmd = Command::new(&python);
    cmd.args(["-m", "bot.main"])
        .current_dir(&project_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = no_window(&mut cmd)
        .spawn()
        .map_err(|e| format!("failed to spawn bot process: {e}"))?;

    let pid = child.id();

    if let Some(out) = child.stdout.take() {
        let handle = app.clone();
        thread::spawn(move || {
            for line in BufReader::new(out).lines().map_while(Result::ok) {
                if cfg!(debug_assertions) {
                    eprintln!("[bot stdout] {line}");
                }
                let payload = LogLine {
                    stream: "stdout".into(),
                    line,
                };
                push_backlog(&handle.state::<ServerState>(), payload.clone());
                let _ = handle.emit("server-log", payload);
            }
        });
    }
    if let Some(err) = child.stderr.take() {
        let handle = app.clone();
        thread::spawn(move || {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                if cfg!(debug_assertions) {
                    eprintln!("[bot stderr] {line}");
                }
                let payload = LogLine {
                    stream: "stderr".into(),
                    line,
                };
                push_backlog(&handle.state::<ServerState>(), payload.clone());
                let _ = handle.emit("server-log", payload);
            }
        });
    }

    *guard = Some(child);
    drop(guard);

    let _ = app.emit(
        "server-status",
        ServerStatusPayload {
            running: true,
            pid: Some(pid),
        },
    );

    let handle = app.clone();
    thread::spawn(move || {
        let mut sys = System::new();
        let sys_pid = Pid::from_u32(pid);
        loop {
            thread::sleep(Duration::from_millis(1500));

            // try_wait() on the REAL Child, not just "is this pid still in
            // the OS process table" via sysinfo — that distinction matters:
            // sysinfo told us a process was gone but never why, so a
            // process that died before writing a single byte to stdout/
            // stderr (a silent native crash — antivirus-quarantined venv
            // DLL, missing runtime dependency, etc.) produced a "Server
            // process exited" status with a genuinely empty log panel, no
            // bug in the log-delivery path at all — there was simply
            // nothing captured to deliver. try_wait() always gives a real
            // exit status, so this now guarantees at least one diagnostic
            // log line exists for every exit, even a silent one.
            let state = handle.state::<ServerState>();
            let wait_result = {
                let mut guard = match state.child.lock() {
                    Ok(g) => g,
                    Err(_) => break,
                };
                match guard.as_mut() {
                    Some(child) => child.try_wait(),
                    None => break, // stopped/replaced from elsewhere (stop_server/restart_server)
                }
            };

            match wait_result {
                Ok(None) => {
                    // Still running — sample resources for the GUI's CPU/RAM readout.
                    sys.refresh_processes(ProcessesToUpdate::Some(&[sys_pid]), true);
                    if let Some(proc_) = sys.process(sys_pid) {
                        let sample = ResourceSample {
                            cpu_percent: proc_.cpu_usage(),
                            mem_mb: proc_.memory() as f64 / 1024.0 / 1024.0,
                        };
                        if handle.emit("server-resources", sample).is_err() {
                            break;
                        }
                    }
                }
                Ok(Some(status)) => {
                    if let Ok(mut guard) = state.child.lock() {
                        *guard = None;
                    }
                    let payload = LogLine {
                        stream: "stderr".into(),
                        line: format!(
                            "bot.main exited: {status} — if no error appears above, it produced \
                             no output before dying (check logs/bot.log, or run `python -m bot.main` \
                             directly from a terminal in the install directory for the full traceback)"
                        ),
                    };
                    push_backlog(&state, payload.clone());
                    let _ = handle.emit("server-log", payload);
                    let _ = handle.emit(
                        "server-status",
                        ServerStatusPayload {
                            running: false,
                            pid: None,
                        },
                    );
                    break;
                }
                Err(e) => {
                    if let Ok(mut guard) = state.child.lock() {
                        *guard = None;
                    }
                    let payload = LogLine {
                        stream: "stderr".into(),
                        line: format!("failed to check bot.main's exit status: {e}"),
                    };
                    push_backlog(&state, payload.clone());
                    let _ = handle.emit("server-log", payload);
                    let _ = handle.emit(
                        "server-status",
                        ServerStatusPayload {
                            running: false,
                            pid: None,
                        },
                    );
                    break;
                }
            }
        }
    });

    Ok(())
}

#[tauri::command]
fn start_server(app: AppHandle, state: State<ServerState>) -> Result<(), String> {
    spawn_internal(&app, &state)
}

#[tauri::command]
fn stop_server(app: AppHandle, state: State<ServerState>) -> Result<(), String> {
    {
        let mut guard = state
            .child
            .lock()
            .map_err(|_| "state poisoned".to_string())?;
        if let Some(child) = guard.take() {
            terminate_child(child);
        }
    }
    let _ = app.emit(
        "server-status",
        ServerStatusPayload {
            running: false,
            pid: None,
        },
    );
    Ok(())
}

#[tauri::command]
fn restart_server(app: AppHandle, state: State<ServerState>) -> Result<(), String> {
    {
        let mut guard = state
            .child
            .lock()
            .map_err(|_| "state poisoned".to_string())?;
        if let Some(child) = guard.take() {
            terminate_child(child);
        }
    }
    let _ = app.emit(
        "server-status",
        ServerStatusPayload {
            running: false,
            pid: None,
        },
    );
    thread::sleep(Duration::from_millis(300));
    spawn_internal(&app, &state)
}

/// Everything emitted as a "server-log" event so far, oldest first — lets
/// the frontend backfill whatever it missed by not having its listener
/// attached yet (see ServerState::log_backlog's doc comment for the real
/// race this closes).
#[tauri::command]
fn get_boot_log(state: State<ServerState>) -> Result<Vec<LogLine>, String> {
    state
        .log_backlog
        .lock()
        .map(|backlog| backlog.clone())
        .map_err(|_| "state poisoned".to_string())
}

/// Reads the resolved .env's DASHBOARD_TOKEN so the GUI can unlock itself
/// without the user pasting a token they'd have to go find in a text file
/// first. Shells out to `bot.envfile`'s own resolver (same override ->
/// project .env -> ~/.claude/.env order the running server uses) rather
/// than duplicating that logic in Rust, so this can never disagree with
/// what the server actually loaded. Local-only: this never leaves the
/// machine, and the standalone browser dashboard (a different trust
/// boundary) still requires pasting the token by hand.
#[tauri::command]
fn get_dashboard_token(app: AppHandle) -> Result<Option<String>, String> {
    let (project_root, python) = resolve_paths(&app)?;
    if !python.exists() {
        return Ok(None);
    }
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "bot.envfile", "--print-token"])
        .current_dir(&project_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let output = no_window(&mut cmd)
        .output()
        .map_err(|e| format!("failed to read dashboard token: {e}"))?;
    let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Ok(if token.is_empty() { None } else { Some(token) })
}

/// The 4 selectable app icons, embedded at compile time (`include_bytes!`)
/// rather than loaded from a bundled resource path — avoids any
/// dev-vs-release resource-directory resolution difference (see
/// `resolve_paths` above for how much that distinction already matters
/// elsewhere in this file) for what's otherwise a tiny, fixed set of
/// files. Only changes the *running* window's icon (title bar + taskbar
/// while open, via `WebviewWindow::set_icon`) — the installed .exe's own
/// embedded icon and any pinned taskbar/desktop shortcut are baked in at
/// build time and need a real reinstall to change; the frontend's Settings
/// UI says so rather than implying a full icon swap.
const ICON_CUTE: &[u8] = include_bytes!("../icons/variants/cute/icon.png");
const ICON_VAPORWAVE: &[u8] = include_bytes!("../icons/variants/vaporwave/icon.png");
const ICON_HOLO: &[u8] = include_bytes!("../icons/variants/holo/icon.png");
const ICON_CYBERPUNK: &[u8] = include_bytes!("../icons/variants/cyberpunk/icon.png");

#[tauri::command]
fn set_app_icon(app: AppHandle, icon_name: String) -> Result<(), String> {
    let bytes: &[u8] = match icon_name.as_str() {
        "cute" => ICON_CUTE,
        "vaporwave" => ICON_VAPORWAVE,
        "holo" => ICON_HOLO,
        "cyberpunk" => ICON_CYBERPUNK,
        other => return Err(format!("unknown icon '{other}'")),
    };
    let image = tauri::image::Image::from_bytes(bytes).map_err(|e| e.to_string())?;
    if let Some(window) = app.get_webview_window("main") {
        window.set_icon(image).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn server_status(state: State<ServerState>) -> Result<ServerStatusPayload, String> {
    let guard = state
        .child
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    Ok(match guard.as_ref() {
        Some(c) => ServerStatusPayload {
            running: true,
            pid: Some(c.id()),
        },
        None => ServerStatusPayload {
            running: false,
            pid: None,
        },
    })
}

/// Re-points the Start Menu/Desktop shortcuts (if they exist) at the
/// standalone $INSTDIR\icon.ico instead of whatever they currently
/// reference, so a future icon change never needs a rebuild — just
/// overwrite icon.ico (see scripts/sync_desktop_app.ps1) and the next
/// launch fixes the shortcuts up.
///
/// Deliberately done here, at every startup, rather than only once in
/// the NSIS installer's own postinstall hook: Tauri's default installer
/// template only creates the Desktop shortcut immediately for silent/
/// passive installs — for a normal interactive install it's created
/// later, from the FINISH PAGE's "create desktop shortcut" checkbox
/// callback (MUI_FINISHPAGE_SHOWREADME_FUNCTION), which runs AFTER
/// NSIS_HOOK_POSTINSTALL. A hook-only fix would silently miss that
/// shortcut on the single most common install path. Running this at
/// every launch instead is timing-independent and self-healing (it also
/// fixes a shortcut the user recreates or that Windows regenerates
/// later) at the cost of one cheap, idempotent PowerShell call per
/// shortcut per startup. Only touches a shortcut that already exists —
/// never creates one the user didn't already have.
#[cfg(target_os = "windows")]
fn fix_shortcut_icons(app: &AppHandle) {
    let Ok((project_root, _)) = resolve_paths(app) else {
        return;
    };
    // project_root is the install dir in release mode (where icon.ico
    // lands per tauri.conf.json's resources mapping) and the dev repo
    // root in debug mode (where icons/icon.ico lives directly).
    let icon_path = if cfg!(debug_assertions) {
        project_root
            .join("desktop-app")
            .join("src-tauri")
            .join("icons")
            .join("icon.ico")
    } else {
        project_root.join("icon.ico")
    };
    if !icon_path.exists() {
        return;
    }
    let icon_path_str = icon_path.display().to_string();

    let start_menu = dirs_next_start_menu();
    let desktop = dirs_next_desktop();
    for dir in [start_menu, desktop].into_iter().flatten() {
        let lnk = dir.join("BotServer.lnk");
        if !lnk.exists() {
            continue;
        }
        let ps = format!(
            "$sh = New-Object -ComObject WScript.Shell; \
             $lnk = $sh.CreateShortcut('{}'); \
             $wanted = '{},0'; \
             if ($lnk.IconLocation -ne $wanted) {{ $lnk.IconLocation = $wanted; $lnk.Save() }}",
            lnk.display(),
            icon_path_str.replace('\'', "''"),
        );
        let mut cmd = Command::new("powershell");
        cmd.args(["-NoProfile", "-NonInteractive", "-Command", &ps]);
        let _ = no_window(&mut cmd).output();
    }
}

#[cfg(target_os = "windows")]
fn dirs_next_start_menu() -> Option<PathBuf> {
    std::env::var_os("APPDATA").map(PathBuf::from).map(|p| {
        p.join("Microsoft")
            .join("Windows")
            .join("Start Menu")
            .join("Programs")
    })
}

#[cfg(target_os = "windows")]
fn dirs_next_desktop() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .map(|p| p.join("Desktop"))
}

#[cfg(not(target_os = "windows"))]
fn fix_shortcut_icons(_app: &AppHandle) {}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(ServerState {
            child: Mutex::new(None),
            log_backlog: Mutex::new(Vec::new()),
        })
        .invoke_handler(tauri::generate_handler![
            start_server,
            stop_server,
            restart_server,
            server_status,
            set_app_icon,
            get_dashboard_token,
            get_boot_log,
            android_env_status,
            list_adb_devices,
            build_android_apk,
            install_android_apk,
            pair_android_device,
            detect_lan_host,
            detect_tailscale_host,
            check_for_update,
            download_update,
            install_update
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            let icon_fix_handle = handle.clone();
            thread::spawn(move || fix_shortcut_icons(&icon_fix_handle));
            let state = handle.state::<ServerState>();
            if let Err(e) = spawn_internal(&handle, &state) {
                if cfg!(debug_assertions) {
                    eprintln!("[bot-server] spawn_internal failed: {e}");
                }
                let payload = LogLine {
                    stream: "stderr".into(),
                    line: format!("startup error: {e}"),
                };
                push_backlog(&state, payload.clone());
                let _ = handle.emit("server-log", payload);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let state = window.state::<ServerState>();
                let mut guard = match state.child.lock() {
                    Ok(g) => g,
                    Err(_) => return,
                };
                if let Some(child) = guard.take() {
                    terminate_child(child);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
