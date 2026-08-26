# Bot Server installer — Windows bootstrap.
#
# Thin entry point: its only job is to guarantee a real Python 3.11+ is on
# PATH (installing it via winget if it's missing), then hand off to
# scripts\install.py, which does the actual hardware/software-aware work
# (Rust, Tauri CLI, the venv, dependencies, and the setup wizard). Python
# itself can't be relied on to already exist on a brand-new machine, which
# is why this bootstrap step is a .ps1 and not another .py file.
#
# Usage:
#   .\scripts\install.ps1                 interactive, installs what's missing
#   .\scripts\install.ps1 -Yes            non-interactive (assume yes to prompts)
#   .\scripts\install.ps1 -Check          report status only, no changes
#   .\scripts\install.ps1 -NoBuild        skip offering a production build
#   .\scripts\install.ps1 -NoAutostart    skip offering login autostart
param(
    [switch]$Yes,
    [switch]$Check,
    [switch]$NoSystemDeps,
    [switch]$NoBuild,
    [switch]$NoAutostart,
    [switch]$Dev
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Bot Server — Windows bootstrap" -ForegroundColor Cyan

function Find-Python {
    foreach ($cmd in @("py", "python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            try {
                $verOut = & $cmd --version 2>&1
                if ($verOut -match "Python (\d+)\.(\d+)") {
                    $maj = [int]$matches[1]; $min = [int]$matches[2]
                    if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
                        return $cmd
                    }
                }
            } catch {}
        }
    }
    return $null
}

$pythonCmd = Find-Python
if (-not $pythonCmd) {
    Write-Host "No Python 3.11+ found on PATH." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if ($Check) {
            Write-Host "Would install Python via winget (skipped — -Check mode)." -ForegroundColor Yellow
            exit 1
        }
        Write-Host "Installing Python via winget..." -ForegroundColor Cyan
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        # winget updates the machine PATH but not this running process's — refresh it.
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $pythonCmd = Find-Python
    }
    if (-not $pythonCmd) {
        Write-Host "Could not find or install Python automatically." -ForegroundColor Red
        Write-Host "Install Python 3.11+ from https://python.org (check 'Add to PATH'), then re-run this script." -ForegroundColor Red
        exit 1
    }
}
Write-Host "Using $pythonCmd ($(& $pythonCmd --version))" -ForegroundColor Green

$pyArgs = @("scripts\install.py")
if ($Yes) { $pyArgs += "--yes" }
if ($Check) { $pyArgs += "--check" }
if ($NoSystemDeps) { $pyArgs += "--no-system-deps" }
if ($NoBuild) { $pyArgs += "--no-build" }
if ($NoAutostart) { $pyArgs += "--no-autostart" }
if ($Dev) { $pyArgs += "--dev" }

& $pythonCmd @pyArgs
exit $LASTEXITCODE
