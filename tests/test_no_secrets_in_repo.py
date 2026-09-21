"""No tracked source file may contain something shaped like a real credential.

Keys belong in gitignored files (.env, config/providers.yaml). The repo and its
releases are public, so a key that lands in a tracked file is a leak. Tests are
skipped here on purpose: they carry deliberate placeholders, and GitHub push
protection already covers those.
"""
from __future__ import annotations

import re
import subprocess

from bot.envfile import PROJECT_ROOT

SHAPES = {
    "provider key": re.compile(r"\bsk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_\-]{32,}"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{20,}"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SKIP_SUFFIXES = (".png", ".jpg", ".ico", ".icns", ".jar", ".apk", ".exe", ".zip", ".woff", ".woff2", ".ttf", ".lock")


def _tracked_sources():
    out = subprocess.run(["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True).stdout
    for rel in out.splitlines():
        if rel.startswith("tests/") or rel.lower().endswith(SKIP_SUFFIXES):
            continue
        yield rel


def test_no_tracked_file_contains_a_credential_shape():
    found = []
    for rel in _tracked_sources():
        try:
            text = (PROJECT_ROOT / rel).read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        for label, pattern in SHAPES.items():
            if pattern.search(text):
                found.append(f"{rel}: {label}")
    assert not found, "credential-shaped text in tracked files (values not shown):\n" + "\n".join(found)


def test_local_secret_files_are_gitignored():
    for name in (".env", "config/providers.yaml"):
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=PROJECT_ROOT)
        assert result.returncode == 0, f"{name} must stay gitignored"
