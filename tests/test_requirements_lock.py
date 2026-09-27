"""requirements.lock is what every installer actually installs (hash-checked).
It must cover every direct requirement and honour each floor in
requirements.txt, or an edit there silently never reaches a real install.
Regenerate with:
  uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes -o requirements.lock
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
