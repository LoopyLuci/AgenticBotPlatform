"""scripts/local_pipeline.py's change-aware skip logic — the part that
decides whether a push needs the Rust check, the Docker build, and the
stop/rebuild/restart cycle at all. Loaded by file path (scripts/ isn't a
package) so this exercises the real module, not a reimplementation of its
prefix rules.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "local_pipeline", Path(__file__).resolve().parent.parent / "scripts" / "local_pipeline.py"
)
local_pipeline = importlib.util.module_from_spec(_SPEC)
sys.modules["local_pipeline"] = local_pipeline
_SPEC.loader.exec_module(local_pipeline)


def test_docs_only_change_needs_nothing():
    changed = {"README.md", "CHANGELOG.md", "docs/adr/0007-example.md"}
    assert not local_pipeline._matches(changed, local_pipeline.RUST_PREFIXES)
    assert not local_pipeline._matches(changed, local_pipeline.DOCKER_PREFIXES)
    assert not local_pipeline._matches(changed, local_pipeline.DEPLOY_PREFIXES)


def test_tests_only_change_needs_nothing_deploy_relevant():
    changed = {"tests/test_export.py", "tests/conftest.py"}
    assert not local_pipeline._matches(changed, local_pipeline.DEPLOY_PREFIXES)
    assert not local_pipeline._matches(changed, local_pipeline.RUST_PREFIXES)
    assert not local_pipeline._matches(changed, local_pipeline.DOCKER_PREFIXES)


def test_python_bot_change_needs_deploy_and_docker_but_not_rust():
    changed = {"bot/router.py"}
    assert local_pipeline._matches(changed, local_pipeline.DEPLOY_PREFIXES)
    assert local_pipeline._matches(changed, local_pipeline.DOCKER_PREFIXES)
    assert not local_pipeline._matches(changed, local_pipeline.RUST_PREFIXES)


def test_rust_source_change_needs_everything_except_docker():
    changed = {"desktop-app/src-tauri/src/lib.rs"}
    assert local_pipeline._matches(changed, local_pipeline.RUST_PREFIXES)
    assert local_pipeline._matches(changed, local_pipeline.DEPLOY_PREFIXES)
    assert not local_pipeline._matches(changed, local_pipeline.DOCKER_PREFIXES)


def test_requirements_change_needs_docker_and_deploy():
    changed = {"requirements.txt"}
    assert local_pipeline._matches(changed, local_pipeline.DOCKER_PREFIXES)
    assert local_pipeline._matches(changed, local_pipeline.DEPLOY_PREFIXES)


def test_changed_files_falls_back_to_none_outside_a_git_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("BOTSERVER_LOCAL_PIPELINE_HOOK", raising=False)
    monkeypatch.setattr(local_pipeline, "ROOT", tmp_path)
    assert local_pipeline.changed_files() is None


def test_changed_files_reads_hook_stdin(monkeypatch):
    monkeypatch.setenv("BOTSERVER_LOCAL_PIPELINE_HOOK", "1")

    def fake_run(cmd, cwd=None, retries=0):
        assert cmd[:3] == ["git", "diff", "--name-only"]
        assert cmd[3] == "oldsha..newsha"
        return True, "bot/router.py\nREADME.md\n"

    monkeypatch.setattr(local_pipeline, "_run", fake_run)
    monkeypatch.setattr(
        sys, "stdin",
        __import__("io").StringIO("refs/heads/main newsha refs/heads/main oldsha\n"),
    )
    assert local_pipeline.changed_files() == {"bot/router.py", "README.md"}


def test_changed_files_new_remote_ref_falls_back_to_merge_base(monkeypatch):
    monkeypatch.setenv("BOTSERVER_LOCAL_PIPELINE_HOOK", "1")
    calls = []

    def fake_run(cmd, cwd=None, retries=0):
        calls.append(cmd)
        if cmd[:2] == ["git", "merge-base"]:
            return True, "basesha\n"
        assert cmd[:4] == ["git", "diff", "--name-only", "basesha..HEAD"]
        return True, "bot/router.py\n"

    monkeypatch.setattr(local_pipeline, "_run", fake_run)
    monkeypatch.setattr(
        sys, "stdin",
        __import__("io").StringIO(f"refs/heads/main newsha refs/heads/main {'0' * 40}\n"),
    )
    assert local_pipeline.changed_files() == {"bot/router.py"}
