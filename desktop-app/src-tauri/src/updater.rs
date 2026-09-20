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
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use crate::no_window;

const REPO: &str = "LoopyLuci/AgenticBotPlatform";
const USER_AGENT: &str = "AgenticBotPlatform-Updater";

/// Ed25519 public key every update installer must be signed with. The
/// matching private key lives only on the release machine, outside this
/// repo (see scripts/update_signing.py). Without a signature check, the
/// only thing standing between a hijacked GitHub account/release and code
/// execution on every installed copy was "the download came from GitHub".
/// If this key is ever rotated, installed copies can only be updated
/// manually once (they'd reject the new signature) — keep the private key
/// backed up.
const UPDATE_PUBLIC_KEY: [u8; 32] = [
    0xb6, 0x8c, 0x84, 0x87, 0x1b, 0xdb, 0x0c, 0xc3, 0x9b, 0x56, 0x6a, 0x09, 0xa5, 0x37, 0x44, 0x96,
    0x9e, 0x5d, 0x57, 0x73, 0xcd, 0xc1, 0xf5, 0x50, 0xf2, 0xbd, 0x7c, 0xed, 0xaf, 0xaf, 0x21, 0xd2,
];

/// Nothing legitimate is anywhere near this large (the installer is ~50 MB,
/// bundled Python included); a cap keeps a hostile/broken response from
/// filling memory, since the whole file is buffered to verify it.
const MAX_INSTALLER_BYTES: u64 = 400 * 1024 * 1024;
const MAX_SIGNATURE_BYTES: u64 = 1024;

/// The ONLY place an update installer is ever written and run from. The
/// frontend used to pass any path to install_update(); now that path is
/// only compared against this one, so a compromised UI can't point the
/// updater at an arbitrary executable.
fn update_installer_path() -> PathBuf {
    std::env::temp_dir().join("AgenticBotPlatform-update-setup.exe")
}

/// Only this project's own GitHub release assets, over HTTPS.
fn is_trusted_download_url(url: &str) -> bool {
    let prefix = format!("https://github.com/{REPO}/releases/download/");
    url.starts_with(&prefix)
        && !url.contains("..")
        && !url.contains('\\')
        && !url.chars().any(|c| c.is_control() || c.is_whitespace())
}

fn verify_signature(public_key: &[u8; 32], data: &[u8], signature: &[u8]) -> Result<(), String> {
    let key = VerifyingKey::from_bytes(public_key)
        .map_err(|e| format!("built-in update key is invalid: {e}"))?;
    let sig_bytes: [u8; 64] = signature
        .try_into()
        .map_err(|_| "update signature file is not a 64-byte Ed25519 signature".to_string())?;
    key.verify(data, &Signature::from_bytes(&sig_bytes))
        .map_err(|_| {
            "update signature does not match this installer — refusing to install it".to_string()
        })
}

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
    // An installer with no matching `<name>.sig` asset is never offered:
    // download_update() would refuse it anyway, and a dead "Download"
    // button is worse than a plain "no auto-update for this release".
    let download_url = if cfg!(target_os = "windows") {
        release
            .assets
            .iter()
            .find(|a| a.name.ends_with("-setup.exe"))
            .filter(|installer| {
                let sig_name = format!("{}.sig", installer.name);
                release.assets.iter().any(|a| a.name == sig_name)
            })
            .map(|a| a.browser_download_url.clone())
            .filter(|url| is_trusted_download_url(url))
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

#[derive(Clone, Serialize)]
struct DownloadProgress {
    downloaded_bytes: u64,
    total_bytes: Option<u64>,
    percent: Option<f32>,
}

/// Downloads the installer to a temp file and returns its local path.
/// Blocking (this app's HTTP needs are small enough that a dedicated
/// async runtime isn't worth the dependency weight) — called from the
/// frontend as a plain `invoke()`, which already runs off the UI thread.
/// Emits "update-download-progress" events as it goes so the frontend can
/// show a real, live progress bar instead of an indefinite spinner for
/// what can otherwise be a multi-minute wait on a slow connection.
#[tauri::command]
pub fn download_update(app: AppHandle, url: String) -> Result<String, String> {
    if !is_trusted_download_url(&url) {
        return Err(format!(
            "refusing to download an update from outside https://github.com/{REPO}/releases/"
        ));
    }
    let response = ureq::get(&url)
        .set("User-Agent", USER_AGENT)
        .timeout(Duration::from_secs(300))
        .call()
        .map_err(|e| format!("download failed: {e}"))?;

    let total_bytes: Option<u64> = response
        .header("Content-Length")
        .and_then(|v| v.parse::<u64>().ok());

    if let Some(t) = total_bytes {
        if t > MAX_INSTALLER_BYTES {
            return Err(format!(
                "update is {t} bytes, larger than the {MAX_INSTALLER_BYTES}-byte limit"
            ));
        }
    }
    let mut reader = response.into_reader();
    let mut bytes = Vec::new();
    let mut buf = [0u8; 64 * 1024];
    let mut downloaded: u64 = 0;
    let mut last_emit_kb = 0u64;
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| format!("download failed while reading: {e}"))?;
        if n == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..n]);
        downloaded += n as u64;
        if downloaded > MAX_INSTALLER_BYTES {
            return Err("update download exceeded the size limit — aborting".to_string());
        }
        // Emitting on every 64KB chunk would flood the frontend with
        // events for a large file; coalesce to roughly once per 256KB.
        if downloaded / (256 * 1024) != last_emit_kb {
            last_emit_kb = downloaded / (256 * 1024);
            let _ = app.emit(
                "update-download-progress",
                DownloadProgress {
                    downloaded_bytes: downloaded,
                    total_bytes,
                    percent: total_bytes.map(|t| (downloaded as f32 / t as f32) * 100.0),
                },
            );
        }
    }
    let _ = app.emit(
        "update-download-progress",
        DownloadProgress {
            downloaded_bytes: downloaded,
            total_bytes,
            percent: Some(100.0),
        },
    );

    // A connection drop mid-download can surface as a clean EOF rather than
    // an io::Error, which the read loop above would otherwise treat as
    // "done" — silently handing install_update() a truncated installer
    // that passes its own exists()-only check and gets run with /S. Content-
    // Length isn't always present (some CDNs omit it), so this only rejects
    // the case it can actually detect rather than requiring a value that
    // isn't guaranteed to exist.
    if let Some(expected) = total_bytes {
        if downloaded != expected {
            return Err(format!(
                "download incomplete: got {downloaded} of {expected} bytes — try again"
            ));
        }
    }

    // Verify BEFORE anything touches the disk: fetch the detached signature
    // published next to the installer and check it against the public key
    // built into this binary. Any failure — missing .sig, wrong key,
    // tampered bytes — means nothing is written and nothing can be run.
    let sig_url = format!("{url}.sig");
    let sig_response = ureq::get(&sig_url)
        .set("User-Agent", USER_AGENT)
        .timeout(Duration::from_secs(30))
        .call()
        .map_err(|e| {
            format!("couldn't fetch the update's signature ({e}) — refusing to install an unsigned update")
        })?;
    let mut sig_bytes = Vec::new();
    sig_response
        .into_reader()
        .take(MAX_SIGNATURE_BYTES + 1)
        .read_to_end(&mut sig_bytes)
        .map_err(|e| format!("couldn't read the update's signature: {e}"))?;
    if sig_bytes.len() as u64 > MAX_SIGNATURE_BYTES {
        return Err("update signature file is unexpectedly large".to_string());
    }
    verify_signature(&UPDATE_PUBLIC_KEY, &bytes, &sig_bytes)?;

    let dest = update_installer_path();
    std::fs::write(&dest, &bytes).map_err(|e| format!("couldn't save installer: {e}"))?;
    Ok(dest.to_string_lossy().to_string())
}

/// Launches the downloaded installer silently, schedules a short detached
/// relaunch of this app, then exits so the installer can replace files
/// this process would otherwise be holding open.
#[tauri::command]
pub fn install_update(app: AppHandle, installer_path: String) -> Result<(), String> {
    // The frontend's path is only sanity-checked, never trusted: the one
    // file this will ever run is the one download_update() verified.
    let installer = update_installer_path();
    if Path::new(&installer_path) != installer.as_path() {
        return Err("refusing to run an installer from an unexpected location".to_string());
    }
    if !installer.exists() {
        return Err(format!("installer not found at {}", installer.display()));
    }
    let current_exe =
        std::env::current_exe().map_err(|e| format!("couldn't resolve own path: {e}"))?;
    let _ = app.emit("update-phase", "installing");

    // std::process::exit() below is a hard process exit, not a window
    // close — it never fires WindowEvent::CloseRequested, so the bot.main
    // child this app spawned would otherwise keep running as an orphan
    // holding the dashboard port straight through the update, competing
    // with (or blocking) the freshly-installed version's own attempt to
    // start it. Stop it explicitly here, the same way a real window close
    // does.
    crate::stop_bot_server(app.state::<crate::ServerState>().inner());
    crate::android::stop_android_build(app.state::<crate::android::AndroidBuildState>().inner());

    #[cfg(target_os = "windows")]
    {
        let mut installer_cmd = Command::new(&installer);
        installer_cmd
            .arg("/S")
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let installer_child = no_window(&mut installer_cmd)
            .spawn()
            .map_err(|e| format!("couldn't launch installer: {e}"))?;

        // A detached helper that waits for the silent install to ACTUALLY
        // finish, then relaunches the freshly-updated exe — this process
        // exits right after spawning it, releasing the file lock the
        // installer needs to replace this very binary. Previously this
        // used a blind `timeout /t 6`, which had no relationship to how
        // long the install genuinely takes (AV scanning the installer, a
        // slow disk, machine under load) — too short either relaunches the
        // OLD exe while the installer still holds it open, or races a
        // partially-written new one. Wait-Process can wait on an arbitrary
        // PID it didn't itself spawn, so it waits for the real installer
        // process specifically, however long that takes, with no guess
        // involved.
        let relaunch_cmd = format!(
            "Wait-Process -Id {} -ErrorAction SilentlyContinue; Start-Process -FilePath '{}'",
            installer_child.id(),
            current_exe.display().to_string().replace('\'', "''"),
        );
        let mut relauncher = Command::new("powershell");
        relauncher
            .args(["-NoProfile", "-NonInteractive", "-Command", &relaunch_cmd])
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

#[cfg(test)]
mod signature_tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    fn keypair() -> (SigningKey, [u8; 32]) {
        let sk = SigningKey::from_bytes(&[7u8; 32]);
        let pk = sk.verifying_key().to_bytes();
        (sk, pk)
    }

    #[test]
    fn accepts_a_valid_signature() {
        let (sk, pk) = keypair();
        let sig = sk.sign(b"installer bytes").to_bytes();
        assert!(verify_signature(&pk, b"installer bytes", &sig).is_ok());
    }

    #[test]
    fn rejects_tampered_installer_bytes() {
        let (sk, pk) = keypair();
        let sig = sk.sign(b"installer bytes").to_bytes();
        assert!(verify_signature(&pk, b"installer bytez", &sig).is_err());
    }

    #[test]
    fn rejects_a_signature_from_a_different_key() {
        let (_sk, pk) = keypair();
        let attacker = SigningKey::from_bytes(&[9u8; 32]);
        let sig = attacker.sign(b"installer bytes").to_bytes();
        assert!(verify_signature(&pk, b"installer bytes", &sig).is_err());
    }

    #[test]
    fn rejects_a_malformed_signature_file() {
        let (_sk, pk) = keypair();
        assert!(verify_signature(&pk, b"x", &[0u8; 10]).is_err());
        assert!(verify_signature(&pk, b"x", &[]).is_err());
    }

    #[test]
    fn the_embedded_public_key_is_a_valid_ed25519_point() {
        assert!(VerifyingKey::from_bytes(&UPDATE_PUBLIC_KEY).is_ok());
    }

    #[test]
    fn only_this_projects_https_release_assets_are_trusted() {
        let ok =
            "https://github.com/LoopyLuci/AgenticBotPlatform/releases/download/v1.0.0/x-setup.exe";
        assert!(is_trusted_download_url(ok));
        for bad in [
            "http://github.com/LoopyLuci/AgenticBotPlatform/releases/download/v1/x.exe",
            "https://evil.example/LoopyLuci/AgenticBotPlatform/releases/download/v1/x.exe",
            "https://github.com/someone-else/AgenticBotPlatform/releases/download/v1/x.exe",
            "https://github.com.evil.example/LoopyLuci/AgenticBotPlatform/releases/download/v1/x.exe",
            "https://github.com/LoopyLuci/AgenticBotPlatform/releases/download/../../x.exe",
            "https://github.com/LoopyLuci/AgenticBotPlatform/releases/download/v1/x .exe",
            "file:///C:/Windows/System32/cmd.exe",
        ] {
            assert!(!is_trusted_download_url(bad), "should reject {bad}");
        }
    }
}
