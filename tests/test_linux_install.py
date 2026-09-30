"""Linux install hygiene. Found on real Ubuntu 26.04 and Debian 13 VMs: `./scripts/install.sh` failed with
"Permission denied" because git stored it without the executable bit (Windows checkouts never notice)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("git") or not (ROOT / ".git").exists(), reason="needs the git checkout")
def test_every_shell_script_is_executable_in_git():
    out = subprocess.run(["git", "ls-files", "-s", "--", "*.sh"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    not_exec = [line.split("\t")[1] for line in out.splitlines() if not line.startswith("100755")]
    assert not not_exec, f"not executable in git (git update-index --chmod=+x): {not_exec}"


def test_shell_scripts_have_lf_line_endings():
    bad = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.sh")
           if not any(part in (".venv", "node_modules", "vendor", "target") for part in p.parts)
           and b"\r\n" in p.read_bytes() and (ROOT / ".git").exists()
           and subprocess.run(["git", "ls-files", "--eol", str(p)], cwd=ROOT, capture_output=True, text=True).stdout.startswith("i/crlf")]
    assert not bad, f"CRLF in the repo copy of: {bad}"
