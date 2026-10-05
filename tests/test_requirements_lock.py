"""requirements.lock is what every installer actually installs (hash-checked).
It must cover every direct requirement and honour each floor in
requirements.txt, or an edit there silently never reaches a real install.
Regenerate with:
  uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes -o requirements.lock
"""
from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Packages a named advisory covers, and the version that fixes it. A dependency that only arrives
# transitively (httpx2 comes in through mcp, which asks for httpx2>=2.5.0) has nothing between it and a
# vulnerable version but the floor ABP writes down, so each advisory here is checked against every place
# a version is written down: the requirements.txt floor, the lock the installers actually use, the Nix
# package and the interpreter the tests run on. Sentinel reports these against whatever is installed
# (bot/sentinel/cve.py inventories python-env), so a venv left behind by an older lock is the one place
# a fixed advisory can still show up.
CVE_FLOORS = {
    "httpx2": ("CVE-2026-84380", (2, 11, 0)),   # MEDIUM, affects 2.10.0 and older
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line)
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def _direct() -> list[tuple[str, str | None]]:
    out = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(?:(==|>=)\s*([^\s;,]+))?", line)
        out.append((_norm(m.group(1)), m.group(3)))
    return out


def _vt(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.+-]", v)[:4])


def _nix_pin(name: str) -> str:
    """The version nix/package.nix builds for a package it overrides itself ("" = nixpkgs' own)."""
    m = re.search(rf'pname = "{re.escape(name)}";\s*\n\s*version = "([^"]+)"',
                  (ROOT / "nix" / "package.nix").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def test_every_direct_requirement_is_locked_and_meets_its_floor():
    locked = _locked()
    for name, floor in _direct():
        assert name in locked, f"{name} is in requirements.txt but not in requirements.lock — regenerate the lock"
        if floor:
            assert _vt(locked[name]) >= _vt(floor), f"{name} locked at {locked[name]}, below the floor {floor}"


def test_lock_is_fully_hashed():
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    pins = len(re.findall(r"^[A-Za-z0-9_.\-]+==", text, re.M))
    assert pins > 20
    assert text.count("--hash=sha256:") >= pins


@pytest.mark.parametrize("name", sorted(CVE_FLOORS))
def test_no_pinned_version_is_covered_by_a_known_advisory(name):
    """Every version ABP writes down for this package has to be past the advisory's fix: the floor (what
    requirements.txt asks for), the lock (what scripts/install.py, the Docker image and the run scripts
    install) and the Nix package."""
    advisory, fixed = CVE_FLOORS[name]
    floor = dict(_direct()).get(name)
    assert floor, f"{name} reaches ABP transitively, so it needs its own floor or a resolver can pick a vulnerable one"
    assert _vt(floor) >= fixed, f"{name} floor {floor} allows the versions {advisory} covers (fix: {'.'.join(map(str, fixed))})"
    locked = _locked().get(name)
    assert locked and _vt(locked) >= fixed, f"the lock installs {name} {locked}, which {advisory} covers"
    nix = _nix_pin(name)
    assert not nix or _vt(nix) >= fixed, f"nix/package.nix builds {name} {nix}, which {advisory} covers"


@pytest.mark.parametrize("name", sorted(CVE_FLOORS))
def test_the_interpreter_running_the_tests_is_past_those_advisories(name):
    """A venv is what Sentinel inventories, so it is checked here too: a venv left behind by an older
    lock is the one place a fixed advisory can still be reported. scripts/install.py re-runs
    requirements.lock with --require-hashes to bring it back in line."""
    advisory, fixed = CVE_FLOORS[name]
    try:
        installed = metadata.version(name)
    except metadata.PackageNotFoundError:
        pytest.skip(f"{name} is not installed in this environment")
    assert _vt(installed) >= fixed, (f"{name} {installed} is installed here and {advisory} is fixed in "
                                     f"{'.'.join(map(str, fixed))}: pip install --require-hashes -r requirements.lock")
