"""Build `abp-rules`, the sandboxed rule language ABP's routines, hook filters and router run on.

    python native/abp-rules/build.py --venv <python.exe>   build the release wheel and install it there
    python native/abp-rules/build.py --dest <dir>          build the release wheel and copy it there
    python native/abp-rules/build.py --check               fmt, clippy and the Rust tests

The crate is self-contained: its own Cargo.toml, its own pyproject.toml, nothing shared with any other
crate under native/. There is deliberately no workspace here - each native crate is its own island.

`--check` is the gate this crate answers to, and it runs the *sandbox* tests (`cargo test`) rather
than the extension's own, because the sandbox is the part that decides what a rule may do. PyO3 sits
behind the crate's `python` feature so those tests link no interpreter; the `python` feature is
checked too, with clippy, so the bridge cannot rot unnoticed.

Every child process on Windows gets CREATE_NO_WINDOW: this is run by installers and pipelines that
have no console of their own, and a cargo.exe console window appearing on someone's desktop mid-
install is exactly the kind of thing that makes an app feel haunted.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CRATE = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"
# cargo/maturin inherit this console when there is one and are invisible when there is not.
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
# The Python bridge is behind this feature; the sandbox it wraps is not.
PYTHON_FEATURE = ["--features", "python"]


def run(cmd: list[str], *, cwd: Path = CRATE, timeout: int = 3600, env: dict | None = None) -> str:
    """Run one build step and return its combined output. Raises on failure - a build step that
    failed must not look like a build step that worked."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=merged, creationflags=NO_WINDOW,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed with exit code {proc.returncode}:\n{out[-8000:]}")
    return out


def cargo(*args: str, **kwargs) -> str:
    return run(["cargo", *args], **kwargs)


def _maturin_cmd(python: str | None) -> list[str]:
    """`python -m maturin` when the target interpreter has maturin, the plain command otherwise."""
    if python:
        try:
            probe = subprocess.run(
                [python, "-c", "import maturin"], capture_output=True, timeout=60, creationflags=NO_WINDOW,
            )
            if probe.returncode == 0:
                return [python, "-m", "maturin"]
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("maturin"):
        return ["maturin"]
    raise SystemExit("maturin is not installed: pip install maturin into the interpreter you passed")


def build_wheel(out_dir: Path, python: str | None = None) -> Path:
    """Build the release wheel with the `python` feature on, and return the wheel that came out."""
    run([*_maturin_cmd(python), "build", "--release", "--out", str(out_dir), *PYTHON_FEATURE],
        timeout=3600)
    wheels = sorted(out_dir.glob("abp_rules-*.whl"))
    if not wheels:
        raise SystemExit(f"maturin reported success but produced no wheel in {out_dir}")
    return wheels[-1]


def check() -> None:
    """The crate's gate: formatting, lints, the sandbox's tests, and that the extension itself builds."""
    steps = [
        ["fmt", "--check"],
        ["clippy", "--all-targets", *PYTHON_FEATURE, "--", "-D", "warnings"],
        ["test"],
        ["build", "--release", *PYTHON_FEATURE],
    ]
    for step in steps:
        out = cargo(*step)
        print(f"cargo {' '.join(step)}: ok")
        for line in out.splitlines():
            if line.startswith("test result:"):
                print(f"  {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venv", help="the python.exe of the venv to install the extension into")
    parser.add_argument("--dest", help="a directory to copy the built wheel into")
    parser.add_argument("--check", action="store_true", help="run fmt, clippy and the Rust tests, then stop")
    args = parser.parse_args()

    if args.check:
        check()
        return
    if not args.venv and not args.dest:
        parser.error("pass --venv <python.exe>, --dest <dir> or --check")

    out_dir = Path(args.dest) if args.dest else Path(tempfile.mkdtemp(prefix="abp-rules-build-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = build_wheel(out_dir, args.venv)
    if args.venv:
        run([args.venv, "-m", "pip", "install", "--no-index", "--force-reinstall", "--no-deps", str(wheel)],
            timeout=900)
        print(f"installed {wheel.name} into {args.venv}")
    else:
        print(f"built {wheel}")


if __name__ == "__main__":
    sys.exit(main())