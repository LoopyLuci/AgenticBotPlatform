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
use android::{android_env_status, build_android_apk, install_android_apk, list_adb_devices, pair_android_device};
mod network;
use network::{detect_lan_host, detect_tailscale_host};

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
    let mut guard = state.child.lock().map_err(|_| "state poisoned".to_string())?;
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
                let _ = handle.emit(
                    "server-log",
                    LogLine { stream: "stdout".into(), line },
                );
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
                let _ = handle.emit(
                    "server-log",
                    LogLine { stream: "stderr".into(), line },
                );
            }
        });
    }

    *guard = Some(child);
    drop(guard);

    let _ = app.emit(
        "server-status",
        ServerStatusPayload { running: true, pid: Some(pid) },
    );

    let handle = app.clone();
    thread::spawn(move || {
        let mut sys = System::new();
        let sys_pid = Pid::from_u32(pid);
        loop {
            thread::sleep(Duration::from_millis(1500));
            sys.refresh_processes(ProcessesToUpdate::Some(&[sys_pid]), true);
            match sys.process(sys_pid) {
                Some(proc_) => {
                    let sample = ResourceSample {
                        cpu_percent: proc_.cpu_usage(),
                        mem_mb: proc_.memory() as f64 / 1024.0 / 1024.0,
                    };
                    if handle.emit("server-resources", sample).is_err() {
                        break;
                    }
                }
                None => {
                    let _ = handle.emit(
                        "server-status",
                        ServerStatusPayload { running: false, pid: None },
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
        let mut guard = state.child.lock().map_err(|_| "state poisoned".to_string())?;
        if let Some(child) = guard.take() {
            terminate_child(child);
        }
    }
    let _ = app.emit(
        "server-status",
        ServerStatusPayload { running: false, pid: None },
    );
    Ok(())
}

#[tauri::command]
fn restart_server(app: AppHandle, state: State<ServerState>) -> Result<(), String> {
    {
        let mut guard = state.child.lock().map_err(|_| "state poisoned".to_string())?;
        if let Some(child) = guard.take() {
            terminate_child(child);
        }
    }
    let _ = app.emit(
        "server-status",
        ServerStatusPayload { running: false, pid: None },
    );
    thread::sleep(Duration::from_millis(300));
    spawn_internal(&app, &state)
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

#[tauri::command]
fn server_status(state: State<ServerState>) -> Result<ServerStatusPayload, String> {
    let guard = state.child.lock().map_err(|_| "state poisoned".to_string())?;
    Ok(match guard.as_ref() {
        Some(c) => ServerStatusPayload { running: true, pid: Some(c.id()) },
        None => ServerStatusPayload { running: false, pid: None },
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(ServerState { child: Mutex::new(None) })
        .invoke_handler(tauri::generate_handler![
            start_server,
            stop_server,
            restart_server,
            server_status,
            get_dashboard_token,
            android_env_status,
            list_adb_devices,
            build_android_apk,
            install_android_apk,
            pair_android_device,
            detect_lan_host,
            detect_tailscale_host
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            let state = handle.state::<ServerState>();
            if let Err(e) = spawn_internal(&handle, &state) {
                if cfg!(debug_assertions) {
                    eprintln!("[bot-server] spawn_internal failed: {e}");
                }
                let _ = handle.emit(
                    "server-log",
                    LogLine { stream: "stderr".into(), line: format!("startup error: {e}") },
                );
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
