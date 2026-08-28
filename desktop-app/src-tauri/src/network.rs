//! Auto-detects addresses a phone/tablet could use to reach this machine's
//! dashboard, so pairing a device never requires the user to go find and
//! type their own LAN IP or Tailscale hostname by hand — the Mobile tab's
//! host/host2 fields pre-fill with whatever this finds, editable if wrong.

use std::net::UdpSocket;
use std::process::{Command, Stdio};

use crate::no_window;

const DASHBOARD_PORT: &str = "8787";

/// The LAN IP this machine would use to originate a connection — found via
/// the standard zero-packets-sent trick (UDP "connect" just asks the OS to
/// pick a route/local address, nothing is actually transmitted to
/// 8.8.8.8) rather than parsing `ipconfig` output or adding a networking
/// dependency for this one lookup.
#[tauri::command]
pub fn detect_lan_host() -> Option<String> {
    let socket = UdpSocket::bind("0.0.0.0:0").ok()?;
    socket.connect("8.8.8.8:80").ok()?;
    let ip = socket.local_addr().ok()?.ip();
    Some(format!("{ip}:{DASHBOARD_PORT}"))
}

/// This machine's tailnet address, if the Tailscale CLI is installed and
/// signed in — `tailscale ip -4` prints just the IPv4 address on success,
/// nothing on stderr worth surfacing if it's not installed/logged in
/// (that's a perfectly normal case, not an error to report).
#[tauri::command]
pub fn detect_tailscale_host() -> Option<String> {
    let mut cmd = Command::new("tailscale");
    cmd.args(["ip", "-4"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let output = no_window(&mut cmd).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let ip = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if ip.is_empty() {
        None
    } else {
        Some(format!("{ip}:{DASHBOARD_PORT}"))
    }
}
