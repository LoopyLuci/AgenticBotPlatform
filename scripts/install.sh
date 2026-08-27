#!/usr/bin/env bash
# Bot Server installer — Linux/macOS bootstrap.
#
# Thin entry point: its only job is to guarantee a real Python 3.11+ is on
# PATH (installing it via the native package manager if it's missing —
# apt/dnf/pacman/zypper/apk on Linux, Homebrew on macOS), then hand off to
# scripts/install.py, which does the actual hardware/software-aware work
# (Rust, Tauri CLI, Linux native GUI libs, the venv, dependencies, and the
# setup wizard). Python itself can't be relied on to already exist on a
# brand-new machine, which is why this bootstrap step is a .sh and not
# another .py file.
#
# Usage:
#   ./scripts/install.sh                  visual GUI installer (default for a plain interactive run)
#   ./scripts/install.sh --cli            text installer instead of the GUI, installs what's missing
#   ./scripts/install.sh --yes            non-interactive (assume yes to prompts) — always text mode
#   ./scripts/install.sh --check          report status only, no changes — always text mode
#   ./scripts/install.sh --no-build       skip offering a production build
#   ./scripts/install.sh --no-autostart   skip offering login autostart
#
# The GUI (scripts/install_gui.py) falls back to this same text installer
# on its own if there's no display or tkinter isn't available — headless
# servers and SSH sessions work with either invocation.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

echo "Bot Server — Linux/macOS bootstrap"

find_python() {
    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ver="$("$cmd" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")"
            major="${ver%%.*}"; minor="${ver##*.}"
            if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON_CMD="$(find_python || true)"

if [ -z "$PYTHON_CMD" ]; then
    echo "No Python 3.11+ found on PATH."
    if [ "$(uname -s)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            if [[ " $* " == *" --check "* ]]; then
                echo "Would install Python via Homebrew (skipped — --check mode)."
                exit 1
            fi
            echo "Installing Python via Homebrew..."
            brew install python@3.12
        else
            echo "Homebrew not found. Install it from https://brew.sh, or install Python"
            echo "3.11+ manually from https://python.org, then re-run this script."
            exit 1
        fi
    elif [ -f /etc/os-release ]; then
        . /etc/os-release
        ID_LIKE="${ID_LIKE:-}"
        if [[ " $* " == *" --check "* ]]; then
            echo "Would install python3 via this distro's package manager (skipped — --check mode)."
            exit 1
        fi
        case " $ID $ID_LIKE " in
            *" debian "*|*" ubuntu "*)
                sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip ;;
            *" fedora "*|*" rhel "*|*" centos "*)
                sudo dnf install -y python3 python3-pip ;;
            *" arch "*|*" manjaro "*)
                sudo pacman -S --noconfirm --needed python python-pip ;;
            *" suse "*|*" opensuse "*)
                sudo zypper --non-interactive install python3 python3-pip ;;
            *" alpine "*)
                sudo apk add python3 py3-pip ;;
            *" nixos "*)
                echo "NixOS detected — use 'nix develop' (see flake.nix) instead of this installer."
                exit 1 ;;
            *)
                echo "Unrecognized distro ($ID). Install Python 3.11+ by hand, then re-run."
                exit 1 ;;
        esac
    else
        echo "Could not detect a package manager. Install Python 3.11+ by hand, then re-run."
        exit 1
    fi
    PYTHON_CMD="$(find_python || true)"
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "Could not find or install Python 3.11+ automatically."
    exit 1
fi
echo "Using $PYTHON_CMD ($("$PYTHON_CMD" --version 2>&1))"

# Automation flags imply a scripted/CI caller, not a human watching a
# screen — always use the text installer for those, and for an explicit
# --cli. Otherwise default to the visual GUI installer (it detects a
# missing display/tkinter itself and falls back to text mode).
use_gui=1
for arg in "$@"; do
    case "$arg" in
        --cli|--yes|--check) use_gui=0 ;;
    esac
done

if [ "$use_gui" = "1" ]; then
    exec "$PYTHON_CMD" scripts/install_gui.py
else
    args=()
    for arg in "$@"; do
        [ "$arg" = "--cli" ] || args+=("$arg")
    done
    exec "$PYTHON_CMD" scripts/install.py "${args[@]}"
fi
