"""Packaging/deployment invariants that keep secrets out of images and the
dashboard off the network by default (found in the security audit):
- `COPY config ./config` baked the builder's gitignored config/providers.yaml
  (provider API keys) into the Docker image;
- docker-compose published the dashboard on every interface, and the
  dashboard can run shell hooks and read provider keys.
"""
from __future__ import annotations

import re
import subprocess

from bot.envfile import PROJECT_ROOT


def _read(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_never_copies_the_whole_config_directory():
    copies = [ln for ln in _read("Dockerfile").splitlines() if ln.strip().upper().startswith("COPY")]
    for line in copies:
        parts = line.split()
        if len(parts) >= 3 and parts[1].rstrip("/").endswith("config"):
            raise AssertionError(f"Dockerfile copies the whole config dir (would include providers.yaml): {line}")
    assert any("config/backends.yaml" in ln for ln in copies)


def test_dockerignore_excludes_secret_bearing_files():
    ignored = {ln.strip() for ln in _read(".dockerignore").splitlines()}
    for required in ("config/providers.yaml", "config/file_share.yaml", ".env", "*.pem", "*.key"):
        assert required in ignored, f".dockerignore must exclude {required}"


def test_compose_publishes_the_dashboard_on_loopback_only():
    published = re.findall(r'-\s*"([^"]*:8787)"', _read("docker-compose.yml"))
    assert published, "compose no longer publishes 8787 in the expected form"
    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), f"dashboard published beyond loopback: {mapping}"


def test_runtime_directories_and_secret_files_are_gitignored():
    for path in ("data/checkpoint_store/x", "logs/bot.log.1", "config/providers.yaml", "some.pem", ".env.production"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=PROJECT_ROOT, capture_output=True,
        )
        assert result.returncode == 0, f"{path} should be gitignored"


def test_env_example_and_project_skills_stay_committable():
    # The runtime-state rules are root-anchored: an unanchored `data/` once hid
    # the Android app's whole `data` package, so new files there were silently
    # left out of commits.
    for path in (
        ".env.example",
        ".claude/skills/support-bot-nlu/SKILL.md",
        "android-app/app/src/main/kotlin/com/agenticbotplatform/mobile/data/New.kt",
        "android-app/app/src/test/kotlin/com/agenticbotplatform/mobile/data/NewTest.kt",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=PROJECT_ROOT, capture_output=True,
        )
        assert result.returncode == 1, f"{path} must NOT be gitignored"
