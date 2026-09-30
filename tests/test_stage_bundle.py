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


def test_vendored_ssh_toolkit_ships_without_its_git_metadata(project):
    """bot/ssh_toolkit.py shells out to this submodule's own bin/ssh-toolkit.ps1
    for every CRUD operation and the peer-pairing SSH auto-setup - without it
    in the bundle, is_available() is silently False in every installed app
    (a real gap a live two-machine peer-link test surfaced)."""
    vendor_dir = project / "vendor" / "ssh_toolkit"
    (vendor_dir / "bin" / ".git").mkdir(parents=True)
    (vendor_dir / "bin" / "ssh-toolkit.ps1").write_text("# real script\n", encoding="utf-8")
    (vendor_dir / ".git").mkdir()
    (vendor_dir / "SSHToolkit.psm1").write_text("# module\n", encoding="utf-8")

    staged_vendor = _stage(project) / "vendor" / "ssh_toolkit"
    assert (staged_vendor / "bin" / "ssh-toolkit.ps1").is_file()
    assert (staged_vendor / "SSHToolkit.psm1").is_file()
    assert not (staged_vendor / ".git").exists()


def test_missing_vendor_ssh_toolkit_does_not_fail_the_build(project):
    """A checkout that never ran `git submodule update --init` must still
    stage successfully - bot/ssh_toolkit.py already fails closed, cleanly
    when the submodule isn't there; the bundle build shouldn't be stricter
    than the runtime it's building."""
    assert not (project / "vendor").exists()
    staged = _stage(project)
    assert not (staged / "vendor").exists()


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


def test_the_cicd_telemetry_package_ships_beside_bot(project):
    """abp_cicd is a sibling of bot/ (standalone scripts and the CLI import it
    without the bot package); the installed app's server imports it for /api/cicd."""
    (project / "abp_cicd" / "__pycache__").mkdir(parents=True)
    (project / "abp_cicd" / "store.py").write_text("X = 1\n", encoding="utf-8")
    (project / "abp_cicd" / "__pycache__" / "store.cpython-311.pyc").write_bytes(b"\0")
    out = _stage(project)
    assert (out / "abp_cicd" / "store.py").is_file()
    assert not list((out / "abp_cicd").rglob("__pycache__"))


def test_the_committed_config_ships_not_the_builders_live_settings(project):
    """A developer's config/backends.yaml also holds their own settings (a linked skill folder under their home);
    the installer must carry the committed file instead."""
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    git("init", "-q")
    git("add", "config/backends.yaml")
    git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "c")
    (project / "config" / "backends.yaml").write_text(f"skills: {PERSONAL}/skills\n", encoding="utf-8")
    assert (_stage(project) / "config" / "backends.yaml").read_text(encoding="utf-8") == "default_backend: cli\n"


def test_outside_git_the_config_file_is_staged_as_it_is(project):
    assert (_stage(project) / "config" / "backends.yaml").read_text(encoding="utf-8") == "default_backend: cli\n"


def test_every_package_the_server_imports_is_in_the_bundle():
    """bot/ imports sibling packages (abp_cicd, abp_toolkit, abp_modkit...). One that is not staged and listed in the
    bundle's resources works from a checkout and fails in the installed app (abp_modkit did: adopting a project
    answered "No module named 'abp_modkit'")."""
    import json
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    used = set()
    for f in (root / "bot").rglob("*.py"):
        used |= set(re.findall(r"^\s*(?:from|import)\s+(abp_[a-z_]+)", f.read_text(encoding="utf-8", errors="ignore"), re.M))
    used = {p for p in used if (root / p / "__init__.py").is_file()}
    assert "abp_modkit" in used and "abp_cicd" in used
    stage = (root / "scripts" / "stage_bundle.py").read_text(encoding="utf-8")
    resources = json.loads((root / "desktop-app" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["bundle"]["resources"]
    for pkg in sorted(used):
        assert f'"{pkg}"' in stage, f"{pkg} is imported by bot/ but scripts/stage_bundle.py does not stage it"
        assert resources.get(f"stage/{pkg}") == pkg, f"{pkg} is not in tauri.conf.json's bundle resources"


def test_the_module_catalog_ships_in_the_bundle():
    """catalog/ holds the overlays ABP ships (the OpenCV family): the registry reads it from ABP's root, which in the
    installed app is the bundle."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert (root / "catalog" / "opencv" / "abp-module.toml").is_file()
    assert '"catalog"' in (root / "scripts" / "stage_bundle.py").read_text(encoding="utf-8")
    resources = json.loads((root / "desktop-app" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))["bundle"]["resources"]
    assert resources.get("stage/catalog") == "catalog"
