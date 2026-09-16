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

pub(crate) mod android;
use android::{
    android_env_status, build_android_apk, install_android_apk, list_adb_devices,
    pair_android_device,
};
mod network;
mod terminal;
mod updater;
use network::{detect_lan_host, detect_tailscale_host};
use terminal::{
    stop_terminal, terminal_resize, terminal_start, terminal_stop, terminal_write, TerminalState,
};
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

pub(crate) struct ServerState {
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
pub(crate) fn terminate_child(mut child: Child) {
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

/// Stops the running bot.main child, if any — the one place this needs to
/// happen from two call sites that used to duplicate it: the window's own
/// CloseRequested handler, and install_update()'s std::process::exit(0)
/// path. That second path used to skip this entirely (a hard process exit
/// never fires CloseRequested), leaving the Python server running as an
/// orphan holding the dashboard port after every update — confirmed as a
/// real gap, not hypothetical: the freshly-installed new version's own
/// spawn_internal() would then either fail to bind the port or end up
/// running alongside a competing leftover instance.
pub(crate) fn stop_bot_server(state: &ServerState) {
    let mut guard = match state.child.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    if let Some(child) = guard.take() {
        terminate_child(child);
    }
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
    let (root, python) = if cfg!(debug_assertions) {
        let dev_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .ok_or_else(|| "could not resolve project root".to_string())?
            .to_path_buf();
        let python = venv_python(&dev_root);
        (dev_root, python)
    } else {
        let res_dir = app
            .path()
            .resource_dir()
            .map_err(|e| format!("could not resolve resource_dir: {e}"))?;
        let python = venv_python(&res_dir);
        (res_dir, python)
    };

    // The bundled .venv (tauri.conf.json's resources: "../../.venv") is
    // whatever `python -m venv` produced on the machine that built this
    // release — on Windows that's a small redirector stub at
    // .venv/Scripts/python.exe, NOT a portable copy of the interpreter. It
    // embeds the exact base-install path it was created against in
    // pyvenv.cfg's `home`/`executable` fields, and refuses to run at all
    // if that exact path doesn't exist on the machine it's launched on —
    // confirmed live: a second machine without Python at that literal
    // path failed immediately with "No Python at '<path>'", well before
    // bot.main could even start. The bundled venv's site-packages
    // (compiled cp311 extensions included) work fine against ANY
    // compatible 3.11.x interpreter — only this recorded pointer is
    // wrong — so self-heal it in place here, the same "fix it at every
    // launch, not just at install time" pattern windows/hooks.nsh's own
    // comment already documents for the shortcut-icon problem, since an
    // install-time-only fix can't cover every install path either.
    if cfg!(target_os = "windows") && !python_actually_works(&python) {
        repair_bundled_venv(&root, &python)?;
    }
    Ok((root, python))
}

fn python_actually_works(python: &std::path::Path) -> bool {
    if !python.is_file() {
        return false;
    }
    let mut cmd = Command::new(python);
    cmd.arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    no_window(&mut cmd)
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn python_reports_311(python: &std::path::Path) -> bool {
    let mut cmd = Command::new(python);
    cmd.arg("--version");
    match no_window(&mut cmd).output() {
        Ok(output) => {
            let text = format!(
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            text.contains("3.11")
        }
        Err(_) => false,
    }
}

/// Searches this machine for a working Python 3.11 (the bundled venv's
/// compiled extensions are built for the cp311 ABI specifically, so a
/// 3.12+/3.10- interpreter would load but likely crash importing them) —
/// the Python Launcher (py.exe, on PATH with any official python.org
/// install regardless of what "python" itself resolves to), then the
/// standard fixed install locations a python.org installer or `winget
/// install Python.Python.3.11` would use, then whatever "python" resolves
/// to on PATH as a last resort.
fn find_compatible_system_python() -> Option<PathBuf> {
    if let Ok(output) = Command::new("py")
        .args(["-3.11", "-c", "import sys; print(sys.executable)"])
        .output()
    {
        if output.status.success() {
            let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !path.is_empty() {
                let candidate = PathBuf::from(path);
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
    }

    let mut candidates: Vec<PathBuf> = vec![
        PathBuf::from(r"C:\Program Files\Python311\python.exe"),
        PathBuf::from(r"C:\Python311\python.exe"),
    ];
    if let Ok(local_app_data) = std::env::var("LOCALAPPDATA") {
        candidates.push(
            PathBuf::from(local_app_data)
                .join("Programs")
                .join("Python")
                .join("Python311")
                .join("python.exe"),
        );
    }
    for candidate in &candidates {
        if candidate.is_file() && python_reports_311(candidate) {
            return Some(candidate.clone());
        }
    }

    if let Ok(output) = Command::new("where").arg("python").output() {
        if output.status.success() {
            for line in String::from_utf8_lossy(&output.stdout).lines() {
                let candidate = PathBuf::from(line.trim());
                if candidate.is_file() && python_reports_311(&candidate) {
                    return Some(candidate);
                }
            }
        }
    }

    None
}

/// Rewrites `<venv_root>/.venv/pyvenv.cfg`'s `home`/`executable` fields to
/// point at `python` in place — see resolve_paths()'s own comment for why
/// this needs to exist at all. Preserves every other line (version,
/// command, include-system-site-packages, …) exactly as the venv's own
/// creation recorded them; those are purely informational to a reader,
/// never consulted by the Windows launcher stub at run time.
fn rewrite_pyvenv_cfg(
    pyvenv_cfg: &std::path::Path,
    python: &std::path::Path,
) -> Result<(), String> {
    let home = python
        .parent()
        .ok_or_else(|| "resolved python path has no parent directory".to_string())?;
    let existing = std::fs::read_to_string(pyvenv_cfg)
        .map_err(|e| format!("couldn't read {}: {e}", pyvenv_cfg.display()))?;

    let mut wrote_home = false;
    let mut wrote_executable = false;
    let mut lines: Vec<String> = existing
        .lines()
        .map(|line| {
            let trimmed = line.trim_start();
            if trimmed.starts_with("home ") || trimmed.starts_with("home=") {
                wrote_home = true;
                format!("home = {}", home.display())
            } else if trimmed.starts_with("executable ") || trimmed.starts_with("executable=") {
                wrote_executable = true;
                format!("executable = {}", python.display())
            } else {
                line.to_string()
            }
        })
        .collect();
    if !wrote_home {
        lines.push(format!("home = {}", home.display()));
    }
    if !wrote_executable {
        lines.push(format!("executable = {}", python.display()));
    }
    std::fs::write(pyvenv_cfg, lines.join("\n") + "\n")
        .map_err(|e| format!("couldn't write {}: {e}", pyvenv_cfg.display()))
}

/// Last resort when no compatible Python is already on this machine —
/// silently installs one via winget (the same mechanism scripts/install.ps1
/// already uses for its own, separate from-source install path, except
/// pinned to 3.11 specifically here: this repair exists to satisfy the
/// bundled venv's cp311-compiled extensions, and a 3.12+ install wouldn't
/// do that). winget ships by default on any Windows 10/11 machine current
/// enough to have App Installer, which covers the overwhelming majority of
/// real targets; a machine without even winget available still gets the
/// same clear "install Python 3.11 yourself" error as before.
fn winget_install_python_311() -> bool {
    let mut cmd = Command::new("winget");
    cmd.args([
        "install",
        "-e",
        "--id",
        "Python.Python.3.11",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--silent",
    ]);
    no_window(&mut cmd)
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn repair_bundled_venv(root: &std::path::Path, python: &std::path::Path) -> Result<(), String> {
    let mut replacement = find_compatible_system_python();
    if replacement.is_none() && winget_install_python_311() {
        replacement = find_compatible_system_python();
    }
    let replacement = replacement.ok_or_else(|| {
        "The bundled Python runtime can't start on this machine (its recorded base install \
         is missing), no compatible Python 3.11 install was found on this machine, and an \
         automatic install via winget didn't succeed either. Install Python 3.11 from \
         https://python.org (check \"Add to PATH\" during setup), then restart \
         AgenticBotPlatform."
            .to_string()
    })?;
    let pyvenv_cfg = root.join(".venv").join("pyvenv.cfg");
    rewrite_pyvenv_cfg(&pyvenv_cfg, &replacement)?;
    if python_actually_works(python) {
        Ok(())
    } else {
        Err(format!(
            "found a Python install at {} but the bundled venv still won't start after \
             repairing {} — its installed packages may be incompatible with that \
             interpreter's exact version",
            replacement.display(),
            pyvenv_cfg.display()
        ))
    }
}

#[cfg(test)]
mod pyvenv_repair_tests {
    use super::rewrite_pyvenv_cfg;
    use std::path::PathBuf;

    #[test]
    fn rewrites_home_and_executable_in_place() {
        let dir =
            std::env::temp_dir().join(format!("abp_pyvenv_repair_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let cfg_path = dir.join("pyvenv.cfg");
        std::fs::write(
            &cfg_path,
            "home = C:\\Program Files\\Python311\n\
             include-system-site-packages = false\n\
             version = 3.11.9\n\
             executable = C:\\Program Files\\Python311\\python.exe\n\
             command = C:\\Program Files\\Python311\\python.exe -m venv Z:\\old\\.venv\n",
        )
        .unwrap();

        let replacement =
            PathBuf::from(r"C:\Users\someone\AppData\Local\Programs\Python\Python311\python.exe");
        rewrite_pyvenv_cfg(&cfg_path, &replacement).unwrap();

        let rewritten = std::fs::read_to_string(&cfg_path).unwrap();
        assert!(
            rewritten.contains(r"home = C:\Users\someone\AppData\Local\Programs\Python\Python311")
        );
        assert!(rewritten.contains(
            r"executable = C:\Users\someone\AppData\Local\Programs\Python\Python311\python.exe"
        ));
        // Untouched, purely informational lines survive as-is.
        assert!(rewritten.contains("include-system-site-packages = false"));
        assert!(rewritten.contains("version = 3.11.9"));
        assert!(rewritten
            .contains(r"command = C:\Program Files\Python311\python.exe -m venv Z:\old\.venv"));

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn adds_missing_keys_instead_of_erroring() {
        let dir = std::env::temp_dir().join(format!(
            "abp_pyvenv_repair_test_missing_keys_{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let cfg_path = dir.join("pyvenv.cfg");
        std::fs::write(&cfg_path, "version = 3.11.9\n").unwrap();

        let replacement = PathBuf::from(r"C:\Python311\python.exe");
        rewrite_pyvenv_cfg(&cfg_path, &replacement).unwrap();

        let rewritten = std::fs::read_to_string(&cfg_path).unwrap();
        assert!(rewritten.contains(r"home = C:\Python311"));
        assert!(rewritten.contains(r"executable = C:\Python311\python.exe"));

        std::fs::remove_dir_all(&dir).ok();
    }
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
                    // Still running — sample resources for the GUI's CPU/RAM
                    // readout. A failed emit here (e.g. the window was mid-
                    // reload for a moment) must NOT break out of this loop —
                    // this same loop is also this process's only crash
                    // detector (the try_wait() call above). Breaking here
                    // used to silently disable crash detection for the rest
                    // of the app's lifetime after one transient emit
                    // failure, leaving server-status stuck reporting
                    // "running" forever even after a real crash.
                    sys.refresh_processes(ProcessesToUpdate::Some(&[sys_pid]), true);
                    if let Some(proc_) = sys.process(sys_pid) {
                        let sample = ResourceSample {
                            cpu_percent: proc_.cpu_usage(),
                            mem_mb: proc_.memory() as f64 / 1024.0 / 1024.0,
                        };
                        let _ = handle.emit("server-resources", sample);
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

/// Reads (generating on first call) the resolved .env's DASHBOARD_TOKEN so
/// the GUI can unlock itself without the user ever pasting a token they'd
/// have to go find in a text file first — `bot.envfile --print-token`
/// itself calls the same idempotent ensure_dashboard_token() bot.main does
/// at its own startup, so whichever of the two processes gets there first
/// on a brand-new install wins and the other just reads back that value;
/// there is no longer a race where this can legitimately come back empty.
/// Shells out to `bot.envfile`'s own resolver (same override ->
/// project .env -> ~/.claude/.env order the running server uses) rather
/// than duplicating that logic in Rust, so this can never disagree with
/// what the server actually loaded. Local-only: this never leaves the
/// machine, and the standalone browser dashboard (a different trust
/// boundary) still requires pasting the token by hand.
#[tauri::command]
fn get_dashboard_token(app: AppHandle) -> Result<Option<String>, String> {
    let (project_root, python) = resolve_paths(&app)?;
    if !python.exists() {
        return Err(format!("python not found at {}", python.display()));
    }
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "bot.envfile", "--print-token"])
        .current_dir(&project_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = no_window(&mut cmd)
        .output()
        .map_err(|e| format!("failed to spawn {}: {e}", python.display()))?;
    let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if token.is_empty() {
        // Surface whatever Python actually said instead of silently
        // falling back to the manual-entry dialog with no trail at all —
        // this exact silent-failure shape (a working `-m bot.envfile
        // --print-token` invocation from a shell, but an empty result
        // from here) is what made a real DASHBOARD_TOKEN-not-found bug
        // undebuggable the first time it happened.
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if !output.status.success() || !stderr.is_empty() {
            return Err(format!(
                "bot.envfile --print-token exited {} in {}: {}",
                output.status,
                project_root.display(),
                if stderr.is_empty() {
                    "(no stderr)"
                } else {
                    &stderr
                }
            ));
        }
        return Ok(None); // exited 0, printed nothing — genuinely no token in .env yet
    }
    Ok(Some(token))
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
        let lnk = dir.join("AgenticBotPlatform.lnk");
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
        .manage(android::AndroidBuildState::default())
        .manage(TerminalState::default())
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
            install_update,
            terminal_start,
            terminal_write,
            terminal_resize,
            terminal_stop
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            let icon_fix_handle = handle.clone();
            thread::spawn(move || fix_shortcut_icons(&icon_fix_handle));
            let state = handle.state::<ServerState>();
            if let Err(e) = spawn_internal(&handle, &state) {
                if cfg!(debug_assertions) {
                    eprintln!("[agentic-bot-platform] spawn_internal failed: {e}");
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
                stop_bot_server(window.state::<ServerState>().inner());
                android::stop_android_build(window.state::<android::AndroidBuildState>().inner());
                stop_terminal(window.state::<TerminalState>().inner());
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
