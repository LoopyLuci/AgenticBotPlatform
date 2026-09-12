<#
.SYNOPSIS
  Lightweight "no rebuild, no reinstall" update path for BotServer's
  installed desktop app.

.DESCRIPTION
  Copies just the hot-reloadable pieces — the Python backend, config,
  the desktop UI's HTML/JS, and the standalone app icon — over the real
  installed copy at $env:LOCALAPPDATA\BotServer. Does NOT touch
  bot-server.exe, Cargo/Rust code, or the bundled .venv: a change to any
  of those still needs a real `cargo tauri build` + reinstall (see
  scripts/publish_release.py). This script covers the routine case —
  everyday feature/UI work — not that one.

  The Python side's existing hot-reload watcher (bot/hotreload.py)
  picks up the bot/config changes on its own, since it watches the
  installed bot/ directory this script just updated — no separate
  signal needed. The desktop UI's JS/HTML is served live from disk
  (bot/dashboard/server.py's /desktop-ui/ route) but the WEBVIEW WINDOW
  still needs to reload to fetch the new files — press Ctrl+R inside
  the BotServer window, or relaunch it.

  Run this yourself, directly, from a normal terminal — not through
  Claude Code or any other MSIX/Desktop-Bridge-packaged app's spawned
  process. Those get $env:LOCALAPPDATA silently redirected to a
  per-package sandbox, so a sync run from inside one silently updates a
  copy the real installed app never sees.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\sync_desktop_app.ps1
#>

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$InstallDir = Join-Path $env:LOCALAPPDATA 'BotServer'

if (-not (Test-Path $InstallDir)) {
    Write-Error "BotServer isn't installed at $InstallDir — run the NSIS installer first (see scripts/publish_release.py)."
    exit 1
}

function Sync-Dir {
    param([string]$Source, [string]$Dest)
    Write-Host "Syncing $Source -> $Dest"
    # /MIR mirrors (deletes files in $Dest that no longer exist in
    # $Source) — safe here because .env, data/, and logs/ all live
    # outside bot/ and config/, never inside them.
    robocopy $Source $Dest /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Write-Error "robocopy failed syncing $Source (exit $LASTEXITCODE)"
        exit 1
    }
}

Sync-Dir (Join-Path $RepoRoot 'bot') (Join-Path $InstallDir 'bot')
Sync-Dir (Join-Path $RepoRoot 'config') (Join-Path $InstallDir 'config')
Sync-Dir (Join-Path $RepoRoot 'desktop-app\ui') (Join-Path $InstallDir 'desktop-app\ui')
Copy-Item (Join-Path $RepoRoot 'desktop-app\src-tauri\icons\icon.ico') (Join-Path $InstallDir 'icon.ico') -Force

Write-Host ""
Write-Host "Synced."
Write-Host "  - Python backend: hot-reloads automatically (bot/hotreload.py's watcher)."
Write-Host "  - Desktop UI: reload the BotServer window (Ctrl+R) or relaunch it to fetch the new files."
Write-Host "  - Icon: the app self-heals its Start Menu/Desktop shortcut icons on every launch (fix_shortcut_icons() in lib.rs) — just relaunch BotServer. Unpin/re-pin the taskbar icon if it still doesn't refresh (Windows sometimes caches a pinned icon separately)."
Write-Host ""
Write-Host "Note: this always copies FROM $RepoRoot — any change made only inside the installed copy's bot/ or config/ would be overwritten by this sync."
