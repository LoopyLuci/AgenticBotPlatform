# Registers a Windows Task Scheduler task that launches the desktop app at
# logon and restarts it automatically if it exits. Run this once, elevated
# is not required for a per-user logon task.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "desktop-app\src-tauri\target\release\bot-server.exe"
$runScript = Join-Path $root "scripts\run.ps1"

if (Test-Path $exe) {
    $action = New-ScheduledTaskAction -Execute $exe
    Write-Host "Using built app: $exe" -ForegroundColor Cyan
} else {
    Write-Host "No release build found at $exe — falling back to run.ps1 (headless, no GUI)." -ForegroundColor Yellow
    Write-Host "Run 'cargo tauri build' in desktop-app\src-tauri first, then re-run this script, to launch the GUI at logon instead." -ForegroundColor Yellow
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runScript`""
}

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "BotServer" -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Registered scheduled task 'BotServer' — runs at logon, restarts on failure." -ForegroundColor Green
