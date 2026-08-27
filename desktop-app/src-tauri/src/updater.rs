//! Checks GitHub Releases for a newer version, downloads the Windows
//! installer asset, and launches it silently, then relaunches the app.
//!
//! Update mechanism, and why: Windows can't let a running .exe overwrite
//! itself, so a genuinely "seamless" self-update still has to go through
//! *some* external process replacing files while this one isn't holding
//! them open. Rather than build and maintain a separate updater binary
//! (a second thing to ship, sign, and keep working), this reuses the same
//! NSIS installer already built for every release, run with its `/S`
//! silent flag — no visible wizard, but proven, already-tested install
//! logic instead of a bespoke file-replacement routine. After the
//! installer exits, a short detached relauncher restarts the app so the
//! whole thing reads as "the app updated itself," even though under the
//! hood it's "install, then relaunch."
//!
//! Every step that actually changes anything on disk (downloading,
//! installing) is a distinct Tauri command the frontend calls only after
//! the user explicitly confirms — see the Updates panel in
//! desktop-app/ui/main.js. Nothing here runs unattended.

use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

use serde::Serialize;

use crate::no_window;

const REPO: &str = "LoopyLuci/BotServer";
const USER_AGENT: &str = "BotServer-Updater";

// A single failed GET to GitHub — a momentary DNS hiccup, a corporate
// proxy/AV product intercepting HTTPS and stalling the handshake, a
// dropped Wi-Fi packet — shouldn't surface as a hard error the user has to
// notice and retry by hand. Retried only for connection/timeout-level
// failures (an actual HTTP error response, e.g. a real 404/500, means
// GitHub answered and retrying won't change that). Mirrors the same
// retry-on-transient-failure pattern used for peer-server linking
// (bot/peers.py) for the same reason.
const RETRY_DELAYS: &[Duration] = &[Duration::from_secs(1), Duration::from_secs(3)];

fn get_with_retry(url: &str, timeout: Duration) -> Result<ureq::Response, String> {
    let mut last_err: Option<ureq::Error> = None;
    for delay in std::iter::once(Duration::ZERO).chain(RETRY_DELAYS.iter().copied()) {
        if !delay.is_zero() {
            std::thread::sleep(delay);
        }
        match ureq::get(url)
            .set("User-Agent", USER_AGENT)
            .set("Accept", "application/vnd.github+json")
            .timeout(timeout)
            .call()
        {
            Ok(resp) => return Ok(resp),
            Err(err @ ureq::Error::Transport(_)) => last_err = Some(err),
            Err(err) => return Err(format!("GitHub returned an error: {err}")),
        }
    }
    let err = last_err.expect("at least one attempt was made");
    Err(format!(
        "couldn't reach GitHub after {} attempts: {err} — check your internet connection, or a firewall/VPN/antivirus product intercepting HTTPS",
        RETRY_DELAYS.len() + 1
    ))
}

#[derive(Clone, Serialize)]
pub struct UpdateInfo {
    pub current_version: String,
    pub latest_version: String,
    pub update_available: bool,
    pub release_notes: String,
    pub download_url: Option<String>,
}

#[derive(serde::Deserialize)]
struct GithubAsset {
    name: String,
    browser_download_url: String,
}

#[derive(serde::Deserialize)]
struct GithubRelease {
    tag_name: String,
    body: Option<String>,
    assets: Vec<GithubAsset>,
}

/// Parses "1.2.3" (leading "v" already stripped by the caller) into a
/// comparable tuple. Missing/non-numeric segments read as 0 — good enough
/// for this project's own consistently-formatted release tags; not a
/// general-purpose semver parser (no pre-release/build-metadata handling).
fn parse_version(v: &str) -> (u32, u32, u32) {
    let mut parts = v.split('.').map(|p| p.parse::<u32>().unwrap_or(0));
    (
        parts.next().unwrap_or(0),
        parts.next().unwrap_or(0),
        parts.next().unwrap_or(0),
    )
}

fn is_newer(latest: &str, current: &str) -> bool {
    parse_version(latest) > parse_version(current)
}

#[tauri::command]
pub fn check_for_update() -> Result<UpdateInfo, String> {
    let current_version = env!("CARGO_PKG_VERSION").to_string();

    let url = format!("https://api.github.com/repos/{REPO}/releases/latest");
    let response = get_with_retry(&url, Duration::from_secs(15))?;

    let release: GithubRelease = serde_json::from_reader(response.into_reader())
        .map_err(|e| format!("couldn't parse GitHub's response: {e}"))?;

    let latest_version = release.tag_name.trim_start_matches('v').to_string();
    let update_available = is_newer(&latest_version, &current_version);

    // Only Windows installers are auto-updatable today (this app only
    // ships a Windows build) — match the NSIS setup asset by its own
    // naming convention (see the release workflow: "*-setup.exe").
    let download_url = if cfg!(target_os = "windows") {
        release
            .assets
            .iter()
            .find(|a| a.name.ends_with("-setup.exe"))
            .map(|a| a.browser_download_url.clone())
    } else {
        None
    };

    Ok(UpdateInfo {
        current_version,
        latest_version,
        update_available,
        release_notes: release.body.unwrap_or_default(),
        download_url,
    })
}

/// Downloads the installer to a temp file and returns its local path.
/// Blocking (this app's HTTP needs are small enough that a dedicated
/// async runtime isn't worth the dependency weight) — called from the
/// frontend as a plain `invoke()`, which already runs off the UI thread.
#[tauri::command]
pub fn download_update(url: String) -> Result<String, String> {
    let response = ureq::get(&url)
        .set("User-Agent", USER_AGENT)
        .timeout(Duration::from_secs(300))
        .call()
        .map_err(|e| format!("download failed: {e}"))?;

    let mut bytes = Vec::new();
    response
        .into_reader()
        .read_to_end(&mut bytes)
        .map_err(|e| format!("download failed while reading: {e}"))?;

    let dest = std::env::temp_dir().join("BotServer-update-setup.exe");
    std::fs::write(&dest, &bytes).map_err(|e| format!("couldn't save installer: {e}"))?;
    Ok(dest.to_string_lossy().to_string())
}

/// Launches the downloaded installer silently, schedules a short detached
/// relaunch of this app, then exits so the installer can replace files
/// this process would otherwise be holding open.
#[tauri::command]
pub fn install_update(installer_path: String) -> Result<(), String> {
    let installer = PathBuf::from(&installer_path);
    if !installer.exists() {
        return Err(format!("installer not found at {installer_path}"));
    }
    let current_exe = std::env::current_exe().map_err(|e| format!("couldn't resolve own path: {e}"))?;

    #[cfg(target_os = "windows")]
    {
        let mut installer_cmd = Command::new(&installer);
        installer_cmd
            .arg("/S")
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        no_window(&mut installer_cmd)
            .spawn()
            .map_err(|e| format!("couldn't launch installer: {e}"))?;

        // A detached helper that waits for the silent install to finish,
        // then relaunches the freshly-updated exe — this process exits
        // right after spawning it, releasing the file lock the installer
        // needs to replace this very binary.
        let relaunch_cmd = format!(
            "timeout /t 6 /nobreak >nul & start \"\" \"{}\"",
            current_exe.display()
        );
        let mut relauncher = Command::new("cmd");
        relauncher
            .args(["/C", &relaunch_cmd])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        no_window(&mut relauncher)
            .spawn()
            .map_err(|e| format!("couldn't schedule relaunch: {e}"))?;
    }
    #[cfg(not(target_os = "windows"))]
    {
        return Err("auto-update is only implemented for the Windows installer today".to_string());
    }

    std::process::exit(0);
}
