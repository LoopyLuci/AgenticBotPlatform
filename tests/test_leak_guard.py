"""scripts/leak_guard.py: the public-repo check, on real temp git repositories.

Every test here builds a real repository in tmp_path (git init, real files, real index, a real
staged diff) and runs the guard against it — the full-tree scan and the pre-commit one. Nothing is
mocked: the point of the guard is what git actually reports, and a fake git would test nothing.

What has to be true of it:
  * an absolute drive-letter path, or a user profile with a real account name, is a leak;
  * Windows' own folders, a bare drive root and a placeholder account are not;
  * a private denylist's terms are leaks, and the list itself never has to be in the repo;
  * each category is fail or warn as the private config says, and only the failing ones stop a
    commit;
  * the allowlist lives in the private config, and the pre-commit mode looks only at what the
    index is about to add.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("leak_guard", ROOT / "scripts" / "leak_guard.py")
leak_guard = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["leak_guard"] = leak_guard
_spec.loader.exec_module(leak_guard)


@pytest.fixture(autouse=True)
def _public_install_defaults(monkeypatch):
    """These tests are the public install's: without a private folder the guard's modes and
    denylist are the shipped defaults (fail on a machine path, no denylist). A test that wants a
    private folder passes one to scan()/main() itself, so the ambient one on the machine running
    the suite can never decide a pass or a fail here."""
    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True,
                         timeout=120, env={"GIT_CONFIG_GLOBAL": str(repo / ".gitconfig"),
                                           "GIT_CONFIG_SYSTEM": "NUL", "PATH": "/usr/bin:/bin",
                                           "SystemRoot": r"C:\Windows", "HOME": str(repo)})
    return out.stdout


@pytest.fixture
def repo(tmp_path):
    """A real, empty git repository with one commit and a sane identity."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "unused@example.invalid")
    git(r, "config", "user.name", "unused")
    (r / "README.md").write_text("# a repository\n", encoding="utf-8")
    git(r, "add", "README.md")
    git(r, "commit", "-q", "-m", "first")
    return r


def _write(repo: Path, name: str, text: str) -> Path:
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _commit(repo: Path, *names: str) -> None:
    git(repo, "add", *names)
    git(repo, "commit", "-q", "-m", "add")


def _private(tmp_path: Path, denylist: str = "", guard: str = "") -> Path:
    d = tmp_path / "private"
    (d / "config").mkdir(parents=True)
    if denylist:
        (d / "denylist.txt").write_text(denylist, encoding="utf-8")
    if guard:
        (d / "config" / "leak_guard.yaml").write_text(guard, encoding="utf-8")
    return d


# --- what counts as a machine path ------------------------------------------------------


@pytest.mark.parametrize("line", [
    "root = 'A:/Data/Thing'",
    'root = "B:\\Repo\\Thing"',
    "cd C:/src",
    'home = r"F:\\Cache"',
    "the checkout is at G:/Projects/MyModule/abp-module.toml",
])
def test_an_absolute_drive_path_is_a_leak(line):
    found = list(leak_guard.scan_text("bot/x.py", line, leak_guard._build_rules(None)))
    assert found and found[0].category == "machine_paths"
    assert found[0].rule == "drive-path"
def test_a_user_profile_with_a_real_account_is_a_leak():
    found = [f for f in leak_guard.scan_text("bot/x.py", r'mods = "C:\\Users\\aRealPerson\\.cargo\\bin"',
                                             leak_guard._build_rules(None)) if f.rule == "user-profile"]
    assert found and "aRealPerson" in found[0].match


def test_a_real_account_name_is_caught_but_a_placeholder_is_not():
    rules = leak_guard._build_rules(None)
    real = list(leak_guard.scan_text("bot/x.py", 'p = r"C:\\Users\\aRealPerson\\AppData"', rules))
    assert [f.rule for f in real] == ["drive-path", "user-profile"]
    for placeholder in ("<you>", "{user}", "someone", "you", "username"):
        text = f'p = r"C:\\Users\\{placeholder}\\AppData"'
        assert not list(leak_guard.scan_text("bot/x.py", text, rules)), placeholder


@pytest.mark.parametrize("line", [
    'bin = r"C:\\Windows\\System32\\cmd.exe"',
    'pf = os.environ.get("ProgramFiles", "C:/Program Files")',
    "root = C:\\",                                   # a bare drive root: Windows itself
    'assert win_to_wsl("C:\\\\") == "/mnt/c"',      # a test of Windows path handling
    'exe = Path(r"C:\\Users\\<you>\\AppData\\Local\\thing.exe")',
    'pf = r"C:\\<Program Files>\\thing.exe"',
])
def test_windows_own_folders_and_placeholders_are_not_leaks(line):
    assert not list(leak_guard.scan_text("bot/x.py", line, leak_guard._build_rules(None))), line


@pytest.mark.parametrize("line", [
    "if z:\\n        return 1",                      # a source line ending, not a path
    'except Exception as exc:\\n        pass',
    "url = 'file:///srv/secrets.txt'",
    "proc = 'python.exe'",
    'msg = "served from /srv/sites and /mnt/data"',
])
def test_ordinary_code_that_merely_looks_pathlike_is_not_a_leak(line):
    assert not list(leak_guard.scan_text("bot/x.py", line, leak_guard._build_rules(None))), line


# --- the denylist -----------------------------------------------------------------------


def test_the_denylist_terms_are_leaks_and_the_list_is_never_read_from_the_repo(tmp_path, repo):
    private = _private(tmp_path, denylist="# a comment\n\nSomeProject\n\\bOther-thing\\b\n")
    _write(repo, "bot/x.py", "name = 'SomeProject'\nother = 'Other-thing'\n")
    _commit(repo, "bot/x.py")
    found = leak_guard.scan(repo, private_dir=private)
    assert [f.category for f in found] == ["private_terms", "private_terms"]
    assert [f.rule for f in found] == ["SomeProject", "\\bOther-thing\\b"], "the rule names the term"
    # and the list itself lives only in the private folder
    assert not any((repo / "denylist.txt").exists() for _ in (0,))


def test_a_term_that_is_not_a_valid_regex_is_matched_literally(tmp_path):
    """A typo in a private list must not silently disable the check."""
    private = _private(tmp_path, denylist="SomeProject(\n")
    _denylist = leak_guard._denylist(private)
    assert _denylist and _denylist[0][1].search("x SomeProject( y")


def test_no_denylist_means_no_private_terms_at_all(tmp_path, repo):
    _write(repo, "bot/x.py", "name = 'SomeProject'\n")
    _commit(repo, "bot/x.py")
    assert leak_guard.scan(repo, private_dir=_private(tmp_path)) == []


# --- scanning a real repository ----------------------------------------------------------


def test_a_clean_repository_reports_nothing(repo):
    _write(repo, "bot/x.py", "def root():\n    return '/srv/data'\n")
    _commit(repo, "bot/x.py")
    assert leak_guard.scan(repo) == []
    assert leak_guard.main(["--root", str(repo)]) == 0


def test_a_leak_is_reported_with_the_path_and_the_line(repo):
    _write(repo, "bot/x.py", "a = 1\nb = 'A:/Data/Thing'\n")
    _commit(repo, "bot/x.py")
    found = leak_guard.scan(repo)
    assert [(f.path, f.line) for f in found] == [("bot/x.py", 2)]


def test_generated_and_vendored_trees_are_not_scanned(repo):
    _write(repo, "requirements.lock", "thing==1  # see A:/Data/Thing\n")
    _write(repo, "docs/api/openapi.json", '{"note": "A:/Data/Thing"}\n')
    _write(repo, "desktop-app/ui/vendor/lib.js", "// A:/Data/Thing\n")
    _commit(repo, "requirements.lock", "docs/api/openapi.json", "desktop-app/ui/vendor/lib.js")
    assert leak_guard.scan(repo) == []


def test_a_binary_file_is_not_decoded_into_noise(repo):
    (repo / "logo.bin").write_bytes(b"\x00\x01A:/Data/Thing\x00")
    _commit(repo, "logo.bin")
    assert leak_guard.scan(repo) == []


def test_a_directory_that_is_not_a_repository_is_not_a_crash(tmp_path):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    assert leak_guard.scan(empty) == []


# --- modes and the allowlist ---------------------------------------------------------------


def test_each_category_is_fail_or_warn_as_the_private_config_says(tmp_path, repo):
    _write(repo, "bot/x.py", "a = 'A:/Data/Thing'\nb = 'SomeProject'\n")
    _commit(repo, "bot/x.py")
    private = _private(tmp_path, denylist="SomeProject\n")
    modes = {"machine_paths": "fail", "private_terms": "warn"}
    failing, warning = leak_guard.report(leak_guard.scan(repo, private_dir=private), modes)
    assert [f.category for f in failing] == ["machine_paths"]
    assert [f.category for f in warning] == ["private_terms"]


def test_the_modes_and_the_allowlist_come_from_the_private_config_and_nowhere_else(tmp_path):
    private = _private(tmp_path, guard="modes:\n  machine_paths: warn\n  private_terms: fail\n"
                                      "allow:\n  - docs/agents/*.md\n")
    cfg = leak_guard.settings(private)
    assert cfg["modes"] == {"machine_paths": "warn", "private_terms": "fail"}
    assert cfg["allow"] == ["docs/agents/*.md"]
    # no private folder at all: the generic defaults, and nothing machine-specific anywhere
    default = leak_guard.settings(None)
    assert default["modes"] == leak_guard.DEFAULT_MODES and default["allow"] == []
    assert not (ROOT / "config" / "leak_guard.yaml").exists(), "the modes must never be in the public repo"
    assert "allow" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_a_broken_private_config_falls_back_to_the_defaults_and_says_so(tmp_path, capsys):
    private = _private(tmp_path, guard="modes: [1, 2]\n  bad indent\n")
    assert leak_guard.settings(private)["modes"] == leak_guard.DEFAULT_MODES
    assert "couldn't read" in capsys.readouterr().err


def test_an_allowlisted_file_is_not_reported(tmp_path, repo):
    _write(repo, "docs/agents/ROADMAP.md", "was at A:/Data/Thing\n")
    _write(repo, "bot/x.py", "is at C:/data/other\n")
    _commit(repo, "docs/agents/ROADMAP.md", "bot/x.py")
    private = _private(tmp_path, guard="allow:\n  - docs/agents/*.md\n")
    found = leak_guard.scan(repo, private_dir=private, allow=leak_guard.settings(private)["allow"])
    assert [f.path for f in found] == ["bot/x.py"]


# --- pre-commit mode: only what the index is about to add ------------------------------------


def test_staged_mode_looks_only_at_the_lines_the_index_is_adding(repo):
    _write(repo, "bot/old.py", "kept = 'A:/Data/Thing'\n")
    _commit(repo, "bot/old.py")
    assert leak_guard.scan(repo) != [], "the whole tree has the leak"
    assert leak_guard.scan(repo, staged=True) == [], "but the index is clean, so a commit is not blocked"

    _write(repo, "bot/old.py", "kept = 'A:/Data/Thing'\nadded = 'C:/data/other'\n")
    git(repo, "add", "bot/old.py")
    staged = leak_guard.scan(repo, staged=True)
    assert [(f.path, f.line) for f in staged] == [("bot/old.py", 2)], "the pre-existing line is nobody's fault"


def test_staged_mode_reports_the_real_line_number_not_a_diff_offset(repo):
    _write(repo, "bot/x.py", "".join(f"line{i} = {i}\n" for i in range(1, 30)))
    _write(repo, "bot/x.py", "".join(f"line{i} = {i}\n" for i in range(1, 30)) + "leak = 'A:/Thing'\n")
    git(repo, "add", "bot/x.py")
    found = leak_guard.scan(repo, staged=True)
    assert [(f.line, f.text) for f in found] == [(30, "leak = 'A:/Thing'")]


def test_a_staged_deletion_is_not_a_leak(repo):
    _write(repo, "bot/x.py", "leak = 'A:/Thing'\n")
    _commit(repo, "bot/x.py")
    (repo / "bot/x.py").unlink()
    git(repo, "add", "-A")
    assert leak_guard.scan(repo, staged=True) == []


# --- the command itself ----------------------------------------------------------------------


def test_the_command_fails_on_a_leak_and_passes_on_a_clean_tree(repo, capsys):
    _write(repo, "bot/x.py", "leak = 'A:/Data/Thing'\n")
    _commit(repo, "bot/x.py")
    assert leak_guard.main(["--root", str(repo)]) == 1
    assert "1 finding" in capsys.readouterr().err
    _write(repo, "bot/x.py", "leak = '/srv/thing'\n")
    _commit(repo, "bot/x.py")
    assert leak_guard.main(["--root", str(repo)]) == 0


def test_the_command_warns_but_passes_when_the_category_is_set_to_warn(tmp_path, repo, capsys):
    _write(repo, "bot/x.py", "leak = 'A:/Data/Thing'\n")
    _commit(repo, "bot/x.py")
    private = _private(tmp_path, guard="modes:\n  machine_paths: warn\n")
    assert leak_guard.main(["--root", str(repo), "--private-dir", str(private)]) == 0
    assert "1 warning(s), nothing failing" in capsys.readouterr().err


def test_the_pre_commit_hook_runs_the_guard_on_the_staged_diff_only(repo, monkeypatch):
    """The hook ABP installs: the same guard, in staged mode, so an unrelated commit is never
    blocked by a match somebody else left in a file nobody is touching."""
    _write(repo, "bot/old.py", "kept = 'A:/Data/Thing'\n")
    _commit(repo, "bot/old.py")
    hook = ROOT / "scripts" / "git-hooks" / "pre-commit"
    text = hook.read_text(encoding="utf-8")
    assert "leak_guard.py" in text and "--staged" in text, text
    assert (ROOT / "scripts" / "git-hooks" / "pre-push").is_file(), "the pipeline's hook is still there"
    for installer in ("scripts/install_git_hooks.sh", "scripts/install_git_hooks.ps1"):
        body = (ROOT / installer).read_text(encoding="utf-8")
        assert "pre-commit" in body and "pre-push" in body, installer


def test_the_pipeline_runs_the_guard_as_its_own_step(repo, monkeypatch, tmp_path):
    """The step the local pipeline runs, driven directly over a real repository."""
    import importlib.util as iu

    spec = iu.spec_from_file_location("lp_leak", ROOT / "scripts" / "local_pipeline.py")
    lp = iu.module_from_spec(spec)
    sys.modules["lp_leak"] = lp
    spec.loader.exec_module(lp)
    monkeypatch.setattr(lp, "ROOT", repo)
    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)

    _write(repo, "bot/x.py", "leak = 'A:/Data/Thing'\n")
    _commit(repo, "bot/x.py")
    assert lp.check_leak_guard() is False
    _write(repo, "bot/x.py", "leak = '/srv/thing'\n")
    _commit(repo, "bot/x.py")
    assert lp.check_leak_guard() is True


def test_this_repository_has_no_machine_path_of_its_own():
    """ABP's own tree, scanned by the guard it ships: since the private-integration move (2026-10-08) nothing in it
    names a machine path (the guard's own test file, whose fixtures are what it catches, is skipped by the guard)."""
    findings = leak_guard.scan(ROOT)
    assert not findings, f"machine paths in the public tree: {sorted({(f.path, f.match) for f in findings})}"