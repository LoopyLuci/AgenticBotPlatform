"""ABP must start where NumPy cannot load (not installed, or built for a newer CPU: NumPy needs x86-64-v2). Found on
Linux test VMs: the import error in the support bot's neural model crash-looped the whole dashboard."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_dashboard_imports_and_the_support_bot_answers_without_numpy():
    code = textwrap.dedent("""
        import builtins, sys
        real = builtins.__import__
        def fake(name, *a, **k):
            if name == "numpy" or name.startswith("numpy."):
                raise RuntimeError("NumPy was built with baseline optimizations: (X86_V2) but your machine doesn't support")
            return real(name, *a, **k)
        builtins.__import__ = fake
        from bot.support_bot import hybrid
        assert hybrid.NEURAL_UNAVAILABLE and [n for n, _ in hybrid.CLASSIFIERS] == ["tfidf"]
        hybrid.warm_up()
        import bot.dashboard.server
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stderr[-2000:]
