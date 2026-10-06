"""Build the abp-wasm extension module and, with --check, run the crate's own gate.

    python native/abp-wasm/build.py --venv <python.exe>     # build + install into that venv
    python native/abp-wasm/build.py --dest <dir>             # build the .pyd/.so into <dir>
    python native/abp-wasm/build.py --check                 # cargo fmt, clippy and test

ABP keeps working without this module (bot/wasm_plugins/runtime.py falls back and says so,
and ABP_NATIVE=0 turns it off deliberately), so nothing here is on a startup path: it is
run by scripts/local_pipeline.py, by the release bundle, and by hand.

The build is windowless on Windows: maturin and cargo are console programs, and a console
program started without a console flag puts a window on a person's desktop.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
#: No console window for a console program on Windows. CREATE_NO_WINDOW rather than
#: DETACHED_PROCESS: a detached process has no console at all, which makes every console
#: program *it* starts put one on the desktop.
CREATE_NO_WINDOW = 0x08000000


def _flags() -> int:
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if not cargo:
        raise SystemExit("cargo is not on PATH - install Rust (https://rustup.rs) and retry")
    return cargo


def _run(argv: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run one build step, windowless, and say what it was before it starts."""
    print("+ " + " ".join(argv), flush=True)
    return subprocess.run(  # noqa: S603 - a fixed argv, never a shell string
        argv, cwd=str(cwd), check=check, creationflags=_flags()
    )


def build(*, venv: str | None, dest: str | None, release: bool = True) -> Path:
    """Build the extension module. `--venv` installs it into that interpreter's site-packages,
    which is how ABP finds it; `--dest` copies the artifact somewhere instead (for a release
    bundle, or to look at it)."""
    if bool(venv) == bool(dest):
        raise SystemExit("give exactly one of --venv <python.exe> or --dest <dir>")
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR") or (ROOT / "target"))

    if dest:
        # A binary to a directory rather than a module to an interpreter, so cargo does the
        # build and the file is copied afterwards.
        _run(
            [_cargo(), "build", *(["--release"] if release else []), "--features", "extension-module"],
            cwd=ROOT,
        )
        built = _find_artifact(target_dir / "release")
        out = Path(dest)
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, out / built.name)
        print(f"copied {built.name} to {out}", flush=True)
        return out / built.name

    python = Path(venv)  # type: ignore[arg-type]
    if not python.exists():
        raise SystemExit(f"{python} does not exist")
    # maturin is what knows how to place an extension module where CPython looks for it
    # (and to name and strip it per platform). It finds the target virtualenv from
    # VIRTUAL_ENV, so that is set from the interpreter the caller named rather than from
    # whatever shell the build happens to be running in.
    env = dict(os.environ, VIRTUAL_ENV=str(python.parent.parent))
    print(f"+ {python} -m maturin develop --release", flush=True)
    subprocess.run(  # noqa: S603 - a fixed argv, never a shell string
        [str(python), "-m", "maturin", "develop", "--release", "--manifest-path", str(ROOT / "Cargo.toml")],
        cwd=str(ROOT.parent.parent),
        check=True,
        env=env,
        creationflags=_flags(),
    )
    print(f"installed into {python.parent.parent}", flush=True)
    return _find_artifact(target_dir / "release")


def _find_artifact(directory: Path) -> Path:
    """The extension module cargo produced, whatever this platform calls it: a cdylib is a
    `.dll` on Windows and a `.so`/`.dylib` elsewhere. maturin renames the copy it installs
    to the `.pyd` CPython imports; the cargo output keeps the cdylib's own name."""
    found = sorted(
        p
        for pattern in ("*.pyd", "*.dll", "*.so", "*.dylib")
        for p in directory.glob(pattern)
        if p.stem.startswith("abp_wasm")
    )
    if not found:
        raise SystemExit(f"no abp_wasm extension module was produced in {directory}")
    return found[0]


def check() -> None:
    """The crate's gate: fmt, clippy with warnings as errors, and the tests."""
    _run([_cargo(), "fmt", "--check"], cwd=ROOT)
    _run([_cargo(), "clippy", "--all-targets", "--", "-D", "warnings"], cwd=ROOT)
    _run([_cargo(), "test"], cwd=ROOT)
    print("abp-wasm: fmt, clippy and tests are green", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venv", help="the interpreter to install the extension module into")
    parser.add_argument("--dest", help="a directory to copy the built artifact into, instead of installing")
    parser.add_argument("--debug", action="store_true", help="build without optimisations")
    parser.add_argument("--check", action="store_true", help="run fmt, clippy and cargo test")
    args = parser.parse_args(argv)
    if args.check:
        check()
        return 0
    build(venv=args.venv, dest=args.dest, release=not args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
