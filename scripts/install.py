#!/usr/bin/env python
"""Bot Server installer — hardware/software-environment-aware.

Detects the OS, Linux distro family, and CPU architecture, then installs
everything Bot Server needs to run and build on *this* machine specifically
(not a generic checklist): Rust + Cargo, the Tauri CLI, the native GUI
libraries Tauri needs on Linux (skipped entirely on Windows/macOS, where
they don't apply), the Python virtualenv + `requirements.txt`, and finally
walks the interactive setup wizard. Optionally builds the production app
and registers it to start at login.

Usually invoked through the platform bootstrap script, which guarantees a
usable Python exists before handing off here:

    scripts\\install.ps1          (Windows)
    ./scripts/install.sh          (Linux/macOS)

Can also be run directly once a venv-capable Python 3.9+ is already on
PATH:

    python scripts/install.py [options]

Options:
    --check          report what's missing/present and exit (exit 1 if
                      anything required is missing) — makes no changes
    --yes, -y        don't ask for confirmation before installing anything
                      (needed for unattended/CI use)
    --no-system-deps skip installing OS-level packages (Rust, Tauri CLI,
                      Linux native libs) — assumes they're already present
    --no-build       skip the offer to run `cargo tauri build`
    --no-autostart   skip the offer to register a login autostart entry
    --dev            build for `cargo tauri dev` only, never offers a
                      production build

Safe to re-run any time — every step checks what's already present/valid
before doing anything, mirroring scripts/setup.py's own re-run safety.

Pass --json to emit one JSON object per line on stdout instead of the
human-readable text above (one line per Step.* call, plus "env" and
"summary" events) — this is what scripts/install_gui.py consumes to drive
a live visual installer. The text output is unchanged when --json isn't
passed, so this is purely additive.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# Total number of Step.head() sections a full run produces, in order —
# lets a consumer (scripts/install_gui.py) render a determinate progress
# bar instead of a spinner, without hardcoding this list a second time.
SECTIONS = [
    "Detected environment", "Python", "Rust + Cargo", "Tauri CLI",
    "Linux native GUI libraries (WebKitGTK, GTK3, AppIndicator, ...)",
    "Python virtual environment + dependencies",
    "Bot Server configuration (.env, at least one bot instance)",
    "Production build", "Start at login", "Summary", "Done",
]

JSON_MODE = False


def _emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


class Step:
    """One line of installer output — human-readable text, or one JSON
    object per line when JSON_MODE is on (see module docstring)."""

    @staticmethod
    def head(text: str) -> None:
        if JSON_MODE:
            _emit({"type": "head", "text": text})
        else:
            print(f"\n=== {text} ===")

    @staticmethod
    def ok(text: str) -> None:
        if JSON_MODE:
            _emit({"type": "ok", "text": text})
        else:
            print(f"  [ok]   {text}")

    @staticmethod
    def missing(text: str) -> None:
        if JSON_MODE:
            _emit({"type": "missing", "text": text})
        else:
            print(f"  [--]   {text}")

    @staticmethod
    def doing(text: str) -> None:
        if JSON_MODE:
            _emit({"type": "doing", "text": text})
        else:
            print(f"  ->     {text}")

    @staticmethod
    def warn(text: str) -> None:
        if JSON_MODE:
            _emit({"type": "warn", "text": text})
        else:
            print(f"  [!]    {text}")

    @staticmethod
    def err(text: str) -> None:
        if JSON_MODE:
            _emit({"type": "err", "text": text})
        else:
            print(f"  [ERR]  {text}", file=sys.stderr)


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        Step.doing(f"{prompt} [auto-yes]")
        return True
    if JSON_MODE:
        # The GUI never runs interactively — it shows --check results up
        # front and only invokes a real run with --yes once the user has
        # already confirmed via the GUI's own "Start" button.
        Step.warn(f"{prompt} [no TTY in --json mode — treating as no; pass --yes]")
        return False
    try:
        return input(f"  {prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def run(cmd: list[str], *, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        Step.doing(" ".join(cmd))
    return subprocess.run(cmd, check=check, cwd=str(ROOT))


def which(name: str) -> Optional[str]:
    return shutil.which(name)


# --------------------------------------------------------------------------
# Hardware / OS environment detection
# --------------------------------------------------------------------------

def read_os_release() -> dict:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip().strip('"')
    return values


# (family, [package manager candidates in preference order], install cmd builder, update cmd)
_LINUX_FAMILIES = {
    "debian": {
        "id_like": {"debian", "ubuntu"},
        "pm": "apt-get",
        "update": ["sudo", "apt-get", "update"],
        "install": lambda pkgs: ["sudo", "apt-get", "install", "-y", *pkgs],
        "packages": [
            "libwebkit2gtk-4.1-dev", "libgtk-3-dev", "librsvg2-dev",
            "libayatana-appindicator3-dev", "libssl-dev", "patchelf",
            "build-essential", "curl", "pkg-config",
        ],
    },
    "fedora": {
        "id_like": {"fedora", "rhel", "centos"},
        "pm": "dnf",
        "update": None,
        "install": lambda pkgs: ["sudo", "dnf", "install", "-y", *pkgs],
        "packages": [
            "webkit2gtk4.1-devel", "gtk3-devel", "librsvg2-devel",
            "libappindicator-gtk3-devel", "openssl-devel", "patchelf",
            "@development-tools", "curl", "pkgconf-pkg-config",
        ],
    },
    "arch": {
        "id_like": {"arch", "manjaro"},
        "pm": "pacman",
        "update": None,
        "install": lambda pkgs: ["sudo", "pacman", "-S", "--noconfirm", "--needed", *pkgs],
        "packages": [
            "webkit2gtk-4.1", "gtk3", "librsvg", "libappindicator-gtk3",
            "openssl", "patchelf", "base-devel", "curl", "pkgconf",
        ],
    },
    "suse": {
        "id_like": {"suse", "opensuse"},
        "pm": "zypper",
        "update": None,
        "install": lambda pkgs: ["sudo", "zypper", "--non-interactive", "install", *pkgs],
        "packages": [
            "webkit2gtk3-soup2-devel", "gtk3-devel", "librsvg-devel",
            "libappindicator3-devel", "libopenssl-devel", "patchelf",
            "patterns-devel-base-devel_basis", "curl", "pkg-config",
        ],
    },
    "alpine": {
        "id_like": {"alpine"},
        "pm": "apk",
        "update": ["sudo", "apk", "update"],
        "install": lambda pkgs: ["sudo", "apk", "add", *pkgs],
        "packages": [
            "webkit2gtk-dev", "gtk+3.0-dev", "librsvg-dev",
            "libappindicator-dev", "openssl-dev", "patchelf",
            "build-base", "curl", "pkgconf",
        ],
    },
}


def detect_linux_family() -> Optional[dict]:
    info = read_os_release()
    ident = {info.get("ID", "")} | set(info.get("ID_LIKE", "").split())
    for family in _LINUX_FAMILIES.values():
        if ident & family["id_like"]:
            return family
    return None


def is_nixos() -> bool:
    return Path("/etc/NIXOS").exists() or "nixos" in read_os_release().get("ID", "")


def is_wsl() -> bool:
    if not IS_LINUX:
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def print_environment_report() -> dict:
    Step.head("Detected environment")
    arch = platform.machine() or "unknown"
    system = platform.system()
    report = {"system": system, "arch": arch}
    if not JSON_MODE:
        print(f"  OS:           {system} ({platform.platform()})")
        print(f"  Architecture: {arch}")
        print(f"  Python:       {sys.version.split()[0]} at {sys.executable}")
    if IS_LINUX:
        info = read_os_release()
        distro = info.get("PRETTY_NAME", "unknown Linux")
        if not JSON_MODE:
            print(f"  Distro:       {distro}")
        report["nixos"] = is_nixos()
        report["wsl"] = is_wsl()
        if report["nixos"] and not JSON_MODE:
            print("  Note:         NixOS detected — system package steps are skipped;")
            print("                use `nix develop` (see flake.nix) instead.")
        if report["wsl"] and not JSON_MODE:
            print("  Note:         Running under WSL.")
    report["python_version"] = sys.version.split()[0]
    report["python_executable"] = sys.executable
    report["platform"] = platform.platform()
    if IS_LINUX:
        report["distro"] = read_os_release().get("PRETTY_NAME", "unknown Linux")
    if JSON_MODE:
        _emit({"type": "env", "report": report})
    return report


# --------------------------------------------------------------------------
# Dependency checks / installs
# --------------------------------------------------------------------------

def ensure_python_version() -> bool:
    Step.head("Python")
    ok = sys.version_info >= (3, 11)
    if ok:
        Step.ok(f"Python {sys.version.split()[0]} (>= 3.11 required)")
    else:
        Step.missing(f"Python {sys.version.split()[0]} found, but 3.11+ is required")
    return ok


def ensure_rust(args) -> bool:
    Step.head("Rust + Cargo")
    if which("cargo"):
        version = subprocess.run(["cargo", "--version"], capture_output=True, text=True).stdout.strip()
        Step.ok(version or "cargo present")
        return True

    Step.missing("cargo not found on PATH")
    if args.check or args.no_system_deps:
        return False
    if not confirm("Install Rust via rustup now?", args.yes):
        return False

    if IS_WINDOWS:
        if which("winget"):
            run(["winget", "install", "-e", "--id", "Rustlang.Rustup", "--accept-source-agreements", "--accept-package-agreements"])
        else:
            Step.err("winget not found — install Rust manually from https://rustup.rs and re-run")
            return False
    else:
        # Official rustup install path — the same command https://rustup.rs itself
        # publishes; -y makes it non-interactive with rustup's own defaults.
        sh = which("sh") or "/bin/sh"
        curl = which("curl")
        if not curl:
            Step.err("curl not found — install curl (or Rust manually) and re-run")
            return False
        Step.doing("curl https://sh.rustup.rs | sh -s -- -y")
        curl_proc = subprocess.run([curl, "--proto", "=https", "--tlsv1.2", "-sSf", "https://sh.rustup.rs"], capture_output=True)
        if curl_proc.returncode != 0:
            Step.err("failed to download the rustup installer")
            return False
        subprocess.run([sh, "-s", "--", "-y"], input=curl_proc.stdout, check=True)
        cargo_env = Path.home() / ".cargo" / "env"
        if cargo_env.exists():
            os.environ["PATH"] = str(Path.home() / ".cargo" / "bin") + os.pathsep + os.environ.get("PATH", "")

    if which("cargo"):
        Step.ok("Rust installed")
        return True
    Step.warn("cargo still not on PATH — you may need to open a new shell")
    return False


def ensure_tauri_cli(args) -> bool:
    Step.head("Tauri CLI")
    check = subprocess.run(["cargo", "tauri", "--version"], capture_output=True, text=True) if which("cargo") else None
    if check and check.returncode == 0:
        Step.ok(check.stdout.strip() or "cargo-tauri present")
        return True
    Step.missing("cargo-tauri not found")
    if args.check or args.no_system_deps:
        return False
    if not which("cargo"):
        Step.warn("cargo isn't available yet — install Rust first")
        return False
    if not confirm('Install it now (cargo install tauri-cli --version "^2")?', args.yes):
        return False
    run(["cargo", "install", "tauri-cli", "--version", "^2", "--locked"])
    check = subprocess.run(["cargo", "tauri", "--version"], capture_output=True, text=True)
    if check.returncode == 0:
        Step.ok(check.stdout.strip())
        return True
    Step.err("cargo-tauri install did not succeed")
    return False


def ensure_linux_native_libs(args) -> bool:
    if not IS_LINUX:
        return True
    Step.head("Linux native GUI libraries (WebKitGTK, GTK3, AppIndicator, ...)")
    if is_nixos():
        Step.ok("NixOS detected — handled by `nix develop` / flake.nix instead, skipping")
        return True

    family = detect_linux_family()
    if not family:
        Step.warn("Unrecognized distro — couldn't map to apt/dnf/pacman/zypper/apk.")
        Step.warn("Install the WebKitGTK/GTK3/AppIndicator/librsvg/openssl dev packages")
        Step.warn("for your distro by hand (see README.md's Linux section), then re-run.")
        return False

    Step.ok(f"Package manager: {family['pm']}")
    if args.check or args.no_system_deps:
        return True

    if not confirm(f"Install required system packages via {family['pm']} now (uses sudo)?", args.yes):
        return False
    if family["update"]:
        run(family["update"], check=False)
    run(family["install"](family["packages"]))
    Step.ok("System packages installed")
    return True


def ensure_venv_and_requirements(args) -> bool:
    Step.head("Python virtual environment + dependencies")
    venv_dir = ROOT / ".venv"
    py = venv_dir / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

    if venv_dir.exists() and py.exists():
        Step.ok(f".venv already exists at {venv_dir}")
    else:
        if args.check:
            Step.missing(".venv not yet created")
            return False
        Step.doing(f"creating venv at {venv_dir}")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    if args.check:
        Step.ok("(skipping pip install in --check mode)")
        return True

    # `python -m pip`, not pip.exe directly — on Windows, pip.exe upgrading
    # itself while it's the running process can fail (it can't replace its
    # own locked executable), whereas `python -m pip` doesn't have that
    # problem since python.exe isn't the thing being replaced.
    Step.doing("pip install -r requirements.txt")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=False)
    subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")], check=True)
    Step.ok("Python dependencies installed")
    return True


def run_setup_wizard(args) -> bool:
    Step.head("Bot Server configuration (.env, at least one bot instance)")
    venv_dir = ROOT / ".venv"
    py = venv_dir / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    if not py.exists():
        Step.warn("no venv yet — run without --check first")
        return False

    check = subprocess.run([str(py), str(ROOT / "scripts" / "setup.py"), "--check"])
    if check.returncode == 0:
        Step.ok("Already configured and ready to run")
        return True
    if args.check:
        Step.missing("Setup is incomplete")
        return False
    if not confirm("Run the interactive setup wizard now?", args.yes):
        Step.warn("Skipped — run `scripts\\setup.py` (or `scripts/setup.py`) later")
        return False
    subprocess.run([str(py), str(ROOT / "scripts" / "setup.py")], check=False)
    return True


def offer_build(args) -> None:
    if args.check or args.no_build or args.dev:
        return
    Step.head("Production build")
    if not confirm("Build the standalone desktop app now (cargo tauri build)? "
                    "This bundles the venv + code into a single installer/executable. "
                    "Can take several minutes.", args.yes):
        Step.doing("Skipped. Run later with:")
        print(f"    cd {ROOT / 'desktop-app' / 'src-tauri'}")
        print("    cargo tauri build")
        return
    Step.doing("cargo tauri build")
    subprocess.run(["cargo", "tauri", "build"], cwd=str(ROOT / "desktop-app" / "src-tauri"), check=True)
    Step.ok("Build complete — see desktop-app/src-tauri/target/release/ and .../bundle/")


def offer_autostart(args) -> None:
    if args.check or args.no_autostart:
        return
    Step.head("Start at login")
    label = "Windows Task Scheduler" if IS_WINDOWS else ("a launchd agent" if IS_MACOS else "a systemd --user service")
    if not confirm(f"Register Bot Server to start automatically at login via {label}?", args.yes):
        return
    if IS_WINDOWS:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / "scripts" / "install_task.ps1")],
            check=False,
        )
    elif IS_MACOS:
        subprocess.run(["/bin/bash", str(ROOT / "scripts" / "install_service_macos.sh")], check=False)
    else:
        subprocess.run(["/bin/bash", str(ROOT / "scripts" / "install_service.sh")], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report status only, make no changes (exit 1 if incomplete)")
    parser.add_argument("--yes", "-y", action="store_true", help="don't prompt for confirmation")
    parser.add_argument("--no-system-deps", action="store_true", help="skip installing Rust/Tauri CLI/native libs")
    parser.add_argument("--no-build", action="store_true", help="skip offering a production build")
    parser.add_argument("--no-autostart", action="store_true", help="skip offering login autostart")
    parser.add_argument("--dev", action="store_true", help="dev-only: never offer a production build")
    parser.add_argument("--json", action="store_true", help="emit one JSON event per line on stdout instead of formatted text (for scripts/install_gui.py)")
    args = parser.parse_args()

    global JSON_MODE
    JSON_MODE = args.json

    if not JSON_MODE:
        print("Bot Server — installer")
    print_environment_report()

    results = {
        "python": ensure_python_version(),
        "rust": ensure_rust(args),
        "tauri_cli": ensure_tauri_cli(args),
        "linux_libs": ensure_linux_native_libs(args),
        "venv": ensure_venv_and_requirements(args),
    }
    results["configured"] = run_setup_wizard(args)

    if args.check:
        Step.head("Summary")
        for name, ok in results.items():
            (Step.ok if ok else Step.missing)(name)
        if JSON_MODE:
            _emit({"type": "summary", "results": results, "all_ok": all(results.values())})
        sys.exit(0 if all(results.values()) else 1)

    if not results["venv"]:
        Step.err("Python environment setup failed — fix the error above and re-run.")
        if JSON_MODE:
            _emit({"type": "summary", "results": results, "all_ok": False})
        sys.exit(1)

    offer_build(args)
    offer_autostart(args)

    Step.head("Done")
    next_step = (
        "Next: .\\scripts\\run.ps1  (dev mode) or the built .exe under desktop-app\\src-tauri\\target\\release\\"
        if IS_WINDOWS else
        "Next: ./scripts/run.sh  (dev mode) or the built binary under desktop-app/src-tauri/target/release/"
    )
    if JSON_MODE:
        _emit({"type": "summary", "results": results, "all_ok": all(results.values()), "next_step": next_step})
    else:
        print(f"  {next_step}")


if __name__ == "__main__":
    main()
