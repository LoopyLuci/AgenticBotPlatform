"""scripts/stage_bundle.py — the filtered copy of bot/ and .venv/ the
installer ships. The unfiltered trees leaked dev tooling and the builder's
Windows username/project path into every published installer, so these tests
build a small fake project and check what does and doesn't make it through.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import stage_bundle as sb  # noqa: E402

PERSONAL = "C:\\Users\\someone"


def _fake_project(root: Path, *, leak: bool = False) -> None:
    (root / "bot" / "__pycache__").mkdir(parents=True)
    (root / "bot" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "bot" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\0")
    (root / "config").mkdir()
    (root / "config" / "backends.yaml").write_text("default_backend: cli\n", encoding="utf-8")
    (root / "config" / "providers.yaml").write_text("providers: {x: {api_key: sk-secret}}\n", encoding="utf-8")

    venv = root / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe", "pip.exe", "pytest.exe", "activate", "activate.bat"):
        content = f"{PERSONAL}\\venv\\python.exe" if leak and name == "pip.exe" else "x"
        (venv / "Scripts" / name).write_text(content, encoding="utf-8")
    (venv / "pyvenv.cfg").write_text(
        "home = C:\\Python311\nversion = 3.11.9\ncommand = python -m venv " + PERSONAL + "\\proj\\.venv\n",
        encoding="utf-8",
    )
    sp = venv / "Lib" / "site-packages"
    for pkg in ("numpy", "pip", "pytest", "_pytest", "pluggy", "iniconfig", "pip_audit"):
        (sp / pkg / "__pycache__").mkdir(parents=True)
        (sp / pkg / "__init__.py").write_text("", encoding="utf-8")
    for dist in ("pytest-9.1.1.dist-info", "pip_audit-2.7.dist-info", "numpy-2.0.dist-info", "pip-24.0.dist-info"):
        (sp / dist).mkdir()
        (sp / dist / "METADATA").write_text("x", encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    _fake_project(tmp_path)
    monkeypatch.setattr(sb, "ROOT", tmp_path)
    return tmp_path


def _stage(project: Path) -> Path:
    out = project / "stage"
    sb.stage(out, markers=[PERSONAL, "TelegramBotServer"])
    return out


def test_dev_only_packages_and_their_metadata_are_left_out(project):
    sp = _stage(project) / ".venv" / "Lib" / "site-packages"
    for gone in ("pytest", "_pytest", "pluggy", "iniconfig", "pip_audit", "pytest-9.1.1.dist-info", "pip_audit-2.7.dist-info"):
        assert not (sp / gone).exists(), f"{gone} must not ship"
    for kept in ("numpy", "pip", "numpy-2.0.dist-info", "pip-24.0.dist-info"):
        assert (sp / kept).exists(), f"{kept} is needed at runtime"


def test_pycache_is_left_out_everywhere(project):
    assert not list(_stage(project).rglob("__pycache__"))
    assert not list(_stage(project).rglob("*.pyc"))


def test_only_the_python_launchers_are_kept_in_scripts(project):
    scripts = sorted(p.name for p in (_stage(project) / ".venv" / "Scripts").iterdir())
    assert scripts == ["python.exe", "pythonw.exe"]


def test_pyvenv_cfg_no_longer_records_the_builders_path(project):
    cfg = (_stage(project) / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "command" not in cfg.lower()
    assert PERSONAL not in cfg
    assert "home = C:\\Python311" in cfg  # the Rust side needs this key present


def test_only_backends_yaml_is_staged_from_config(project):
    staged = sorted(p.name for p in (_stage(project) / "config").iterdir())
    assert staged == ["backends.yaml"]


def test_the_build_fails_if_a_personal_path_is_still_in_the_bundle(tmp_path, monkeypatch):
    _fake_project(tmp_path)
    (tmp_path / "bot" / "settings.py").write_text(f"PATH = r'{PERSONAL}\\secret'\n", encoding="utf-8")
    monkeypatch.setattr(sb, "ROOT", tmp_path)

    with pytest.raises(SystemExit, match="personal paths"):
        sb.stage(tmp_path / "stage", markers=[PERSONAL])


def test_the_scan_also_finds_paths_embedded_in_exe_launchers(tmp_path):
    exe = tmp_path / "Scripts"
    exe.mkdir()
    (exe / "tool.exe").write_bytes(b"MZ\0\0" + PERSONAL.encode("utf-16-le") + b"\0")
    hits = sb.scan_for_personal_paths(tmp_path, [PERSONAL])
    assert [h[0].name for h in hits] == ["tool.exe"]
