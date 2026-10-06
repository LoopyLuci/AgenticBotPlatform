"""Build the abp-native extension.

    python native/abp-native/build.py --venv <python.exe>    build the wheel and install it into that venv
    python native/abp-native/build.py --dest <dir>          build the release artifact and copy it to <dir>
    python native/abp-native/build.py --check               fmt, clippy and the cargo tests

`--venv` is how ABP installs it (maturin, from the tree, into the interpreter that will
import it). `--dest` is for a binary: it copies the compiled module next to whatever needs
it. Nothing here needs the network once the crates are in the local cargo registry, and
nothing writes outside the crate, the target directory and the interpreter it is given.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

CRATE = Path(__file__).resolve().parent
# Each native/ task builds into its own target directory under the shared cache; cargo locks
# it, so a concurrent build waits rather than failing. The wheels go there too, so a build
# never leaves anything untracked inside the repository.
DEFAULT_TARGET = Path(os.environ.get("CARGO_TARGET_DIR") or (CRATE / "target"))
WHEELS = DEFAULT_TARGET / "wheels"


def _run(args: list[str], **kw) -> None:
    printable = " ".join(str(a) for a in args)
    print(f"$ {printable}", flush=True)
    # One shell, CREATE_NO_WINDOW: a console window must never appear on someone's desktop.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(args, check=True, cwd=str(CRATE), creationflags=creationflags, **kw)


def _cargo(*args: str) -> None:
    _run(["cargo", *args])


def build(venv: str | None, dest: str | None, release: bool = True) -> Path:
    if venv:
        interpreter = Path(venv).resolve()
        if not interpreter.is_file():
            raise SystemExit(f"--venv: {interpreter} is not a file")
        # maturin reads the interpreter for its ABI and its tag; PYO3_PYTHON is what the
        # build script asks, and both should point at the same one.
        env = {**os.environ, "PYO3_PYTHON": str(interpreter),
               "VIRTUAL_ENV": str(interpreter.parent.parent), "PATH": f"{interpreter.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
        WHEELS.mkdir(parents=True, exist_ok=True)
        _run([str(interpreter), "-m", "maturin", "build", "--release", "--out", str(WHEELS)], env=env)
        wheels = sorted(WHEELS.glob("*.whl"), key=lambda p: p.stat().st_mtime)
        if not wheels:
            raise SystemExit("maturin produced no wheel")
        wheel = wheels[-1]
        _run([str(interpreter), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)], env=env)
        check = subprocess.run([str(interpreter), "-c", "import abp_native, abp_native_core; print(abp_native.active())"],
                               capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if check.returncode != 0:
            raise SystemExit(f"the wheel did not import: {check.stderr.strip() or check.stdout.strip()}")
        print(f"installed {wheel.name} into {interpreter} ({check.stdout.strip()})")
        return wheel

    profile = "release" if release else "debug"
    _cargo("build", *(["--release"] if release else []))
    directory = Path(DEFAULT_TARGET) / profile
    built = directory / _module_name()
    if dest:
        target = Path(dest).resolve()
        target.mkdir(parents=True, exist_ok=True)
        copied = target / built.name
        _copy_artifact(directory, copied)
        print(f"copied {copied.name} to {copied}")
        return copied
    print(f"built {built}")
    return built


def _python_suffix() -> str:
    return ".pyd" if sys.platform == "win32" else ".so"


def _module_name() -> str:
    return f"abp_native_core{_python_suffix()}"


def _copy_artifact(directory: Path, copied: Path) -> None:
    """cargo's cdylib under the name Python imports an extension module by."""
    for candidate in ("abp_native_core.dll", "libabp_native_core.so", "libabp_native_core.dylib"):
        source = directory / candidate
        if source.exists():
            shutil.copy2(source, copied)
            return
    raise SystemExit(f"no built extension module in {directory}")


def check() -> None:
    _cargo("fmt", "--check")
    _cargo("clippy", "--all-targets", "--", "-D", "warnings")
    _cargo("test")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--venv", help="build the wheel with maturin and install it into this interpreter")
    group.add_argument("--dest", help="copy the built extension module into this directory")
    group.add_argument("--check", action="store_true", help="fmt, clippy and the cargo tests")
    parser.add_argument("--debug", action="store_true", help="build without optimisations")
    args = parser.parse_args(argv)

    if args.check:
        check()
    else:
        build(args.venv, args.dest, release=not args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
