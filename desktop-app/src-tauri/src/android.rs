//! One-click Android workflow for the desktop shell: locate the Android SDK
//! and this repo's `android-app/` source next to the running exe, build a
//! debug APK with Gradle, install it on a connected device with adb, and
//! auto-pair it by firing the same `botserver://pair` deep link the manual
//! QR flow already produces — all via plain `std::process::Command`, the
//! same pattern `lib.rs` already uses to supervise the Python bot process.
//! Personal-dev-machine feature: no SDK provisioning, no bundling the
//! Android project into the installer — see docs/mobile-access.md.

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;

use serde::Serialize;
use tauri::{AppHandle, Emitter};

use crate::no_window;

#[derive(Clone, Serialize)]
struct BuildLogLine {
    stream: String,
    line: String,
}

#[derive(Clone, Serialize)]
struct BuildDonePayload {
    success: bool,
    apk_path: Option<String>,
    error: Option<String>,
}

#[derive(Serialize)]
pub struct AndroidEnvStatus {
    sdk_found: bool,
    adb_path: Option<String>,
    project_found: bool,
    project_dir: Option<String>,
}

#[derive(Serialize)]
pub struct AdbDevice {
    serial: String,
    model: String,
    state: String,
}

/// Walks up from this exe's own directory looking for a sibling
/// `android-app/gradlew.bat` — works both for `cargo tauri dev` (exe lives
/// under target/debug, repo root is a few parents up) and for this user's
/// actual deployment, where the release exe runs from inside the repo
/// checkout next to `android-app/`. Deliberately not resolved off
/// `resource_dir()`: the Android project isn't bundled into the installer
/// (see module docs), so there's nothing to find there.
fn gradlew_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "gradlew.bat"
    } else {
        "gradlew"
    }
}

fn adb_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "adb.exe"
    } else {
        "adb"
    }
}

fn find_android_project_dir() -> Option<PathBuf> {
    let mut dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    for _ in 0..8 {
        let candidate = dir.join("android-app");
        if candidate.join(gradlew_name()).is_file() {
            return Some(candidate);
        }
        if !dir.pop() {
            break;
        }
    }
    None
}

/// `ANDROID_HOME`/`ANDROID_SDK_ROOT` first, then `android-app/local.properties`'s
/// `sdk.dir=` line (already present and correct on this machine).
fn find_android_sdk(project_dir: Option<&Path>) -> Option<PathBuf> {
    for var in ["ANDROID_HOME", "ANDROID_SDK_ROOT"] {
        if let Ok(v) = std::env::var(var) {
            let p = PathBuf::from(v);
            if p.is_dir() {
                return Some(p);
            }
        }
    }
    let project_dir = project_dir?;
    let contents = fs::read_to_string(project_dir.join("local.properties")).ok()?;
    for line in contents.lines() {
        if let Some(rest) = line.strip_prefix("sdk.dir=") {
            let unescaped = rest.trim().replace("\\\\", "\\");
            let p = PathBuf::from(unescaped);
            if p.is_dir() {
                return Some(p);
            }
        }
    }
    None
}

fn adb_path_from_sdk(sdk: &Path) -> Option<PathBuf> {
    let p = sdk.join("platform-tools").join(adb_name());
    p.is_file().then_some(p)
}

#[tauri::command]
pub fn android_env_status() -> AndroidEnvStatus {
    let project_dir = find_android_project_dir();
    let sdk = find_android_sdk(project_dir.as_deref());
    let adb = sdk.as_deref().and_then(adb_path_from_sdk);
    AndroidEnvStatus {
        sdk_found: sdk.is_some(),
        adb_path: adb.map(|p| p.display().to_string()),
        project_found: project_dir.is_some(),
        project_dir: project_dir.map(|p| p.display().to_string()),
    }
}

fn require_adb() -> Result<PathBuf, String> {
    let project_dir = find_android_project_dir();
    let sdk = find_android_sdk(project_dir.as_deref())
        .ok_or_else(|| "Android SDK not found (checked ANDROID_HOME/ANDROID_SDK_ROOT and android-app/local.properties).".to_string())?;
    adb_path_from_sdk(&sdk).ok_or_else(|| {
        format!(
            "{} not found under {} — is platform-tools installed?",
            adb_name(),
            sdk.display()
        )
    })
}

#[tauri::command]
pub fn list_adb_devices() -> Result<Vec<AdbDevice>, String> {
    let adb = require_adb()?;
    let mut cmd = Command::new(&adb);
    cmd.args(["devices", "-l"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = no_window(&mut cmd)
        .output()
        .map_err(|e| format!("failed to run adb: {e}"))?;
    let text = String::from_utf8_lossy(&output.stdout);
    let mut devices = Vec::new();
    for line in text.lines().skip(1) {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let mut parts = line.split_whitespace();
        let serial = match parts.next() {
            Some(s) => s.to_string(),
            None => continue,
        };
        let state = parts.next().unwrap_or("unknown").to_string();
        let model = parts
            .find_map(|p| p.strip_prefix("model:"))
            .unwrap_or("unknown model")
            .replace('_', " ");
        devices.push(AdbDevice {
            serial,
            model,
            state,
        });
    }
    Ok(devices)
}

#[tauri::command]
pub fn build_android_apk(app: AppHandle) -> Result<(), String> {
    let project_dir = find_android_project_dir()
        .ok_or_else(|| "android-app/ source tree not found next to this app — this feature only works on the machine used for development.".to_string())?;

    let mut cmd = Command::new(project_dir.join(gradlew_name()));
    cmd.args(["assembleDebug", "--console=plain"])
        .current_dir(&project_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = no_window(&mut cmd)
        .spawn()
        .map_err(|e| format!("failed to start gradlew: {e}"))?;

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    if let Some(out) = stdout {
        let handle = app.clone();
        thread::spawn(move || {
            for line in BufReader::new(out).lines().map_while(Result::ok) {
                let _ = handle.emit(
                    "android-build-log",
                    BuildLogLine {
                        stream: "stdout".into(),
                        line,
                    },
                );
            }
        });
    }
    if let Some(err) = stderr {
        let handle = app.clone();
        thread::spawn(move || {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                let _ = handle.emit(
                    "android-build-log",
                    BuildLogLine {
                        stream: "stderr".into(),
                        line,
                    },
                );
            }
        });
    }

    let handle = app.clone();
    thread::spawn(move || {
        let status = child.wait();
        let apk_path = project_dir
            .join("app")
            .join("build")
            .join("outputs")
            .join("apk")
            .join("debug")
            .join("app-debug.apk");
        let payload = match status {
            Ok(s) if s.success() && apk_path.is_file() => BuildDonePayload {
                success: true,
                apk_path: Some(apk_path.display().to_string()),
                error: None,
            },
            Ok(s) => BuildDonePayload {
                success: false,
                apk_path: None,
                error: Some(format!(
                    "gradlew exited with {s} — see the log above for the failing task."
                )),
            },
            Err(e) => BuildDonePayload {
                success: false,
                apk_path: None,
                error: Some(format!("failed to wait for gradlew: {e}")),
            },
        };
        let _ = handle.emit("android-build-done", payload);
    });

    Ok(())
}

#[tauri::command]
pub fn install_android_apk(serial: String, apk_path: String) -> Result<(), String> {
    let adb = require_adb()?;
    let mut cmd = Command::new(&adb);
    cmd.args(["-s", &serial, "install", "-r", &apk_path])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = no_window(&mut cmd)
        .output()
        .map_err(|e| format!("failed to run adb install: {e}"))?;
    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        Err(format!(
            "adb install failed: {}",
            if stderr.trim().is_empty() {
                stdout.trim()
            } else {
                stderr.trim()
            }
        ))
    }
}

/// Fires the botserver://pair deep link on the device via adb, exactly as
/// if the user had tapped a shared link. `adb shell` joins every arg after
/// "shell" into one string forwarded verbatim to the device's shell, so the
/// whole `am start ...` command — including the URI's `&` query separators —
/// is built as a single Rust-side arg with the URI single-quoted inside it,
/// rather than left as separate argv entries where `&` would be
/// (mis)interpreted as the device shell's background operator.
#[tauri::command]
pub fn pair_android_device(
    serial: String,
    host: String,
    key: String,
    host2: Option<String>,
) -> Result<(), String> {
    if host.trim().is_empty() {
        return Err(
            "Host is required — fill in the host:port field above (e.g. your-tailnet-host:8787)."
                .to_string(),
        );
    }
    let adb = require_adb()?;
    let mut uri = format!("botserver://pair?host={}&key={}", host.trim(), key.trim());
    if let Some(h2) = host2.as_deref().map(str::trim).filter(|h| !h.is_empty()) {
        uri.push_str(&format!("&host2={h2}"));
    }
    let shell_cmd = format!("am start -a android.intent.action.VIEW -d '{uri}'");

    let mut cmd = Command::new(&adb);
    cmd.args(["-s", &serial, "shell", &shell_cmd])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = no_window(&mut cmd)
        .output()
        .map_err(|e| format!("failed to run adb shell: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    if !output.status.success() || stdout.contains("Error") {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "Couldn't trigger pairing on the device: {}",
            if stderr.trim().is_empty() {
                stdout.trim()
            } else {
                stderr.trim()
            }
        ));
    }
    Ok(())
}
