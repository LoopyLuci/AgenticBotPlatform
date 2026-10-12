"""Several processes writing .env at once (a fresh install: every process that starts generates DASHBOARD_TOKEN)
must not fail: each writer has its own temp file, and a replace that Windows refuses for an instant is retried."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CODE = r"""
import sys
from bot import envfile
assert str(envfile.resolve()).startswith(sys.argv[2]), envfile.resolve()
for i in range(25):
    envfile.write_content(f"A=1\nWRITER={sys.argv[1]}-{i}\n", actor="test")
print("ok")
"""


def test_concurrent_writers_all_succeed_and_leave_no_temp_files(tmp_path):
    env = dict(os.environ, ABP_HOME=str(tmp_path), PYTHONPATH=str(ROOT))  # an isolated install: its .env is in tmp_path
    procs = [subprocess.Popen([sys.executable, "-c", CODE, str(n), str(tmp_path)], cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in range(6)]
    results = [p.communicate(timeout=120) for p in procs]
    for p, (out, err) in zip(procs, results):
        assert p.returncode == 0 and out.strip() == "ok", err[-2000:]
    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith("A=1\n")
    assert not list(tmp_path.glob("*.tmp"))
