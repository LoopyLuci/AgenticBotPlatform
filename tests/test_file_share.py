"""bot/file_share.py — allowlisted directory browsing/download. Exercises
real file reads/writes against a temp config/file_share.yaml and a real
temp directory tree, not mocks, since the path-escape guard is exactly
the thing that must never have a false negative.
"""

from __future__ import annotations

import pytest

from bot import file_share
from bot.config import ConfigManager


@pytest.fixture(autouse=True)
def _temp_registry(tmp_path, monkeypatch):
    path = tmp_path / "file_share.yaml"
    # Every test gets its own default-roots seed disabled by starting with
    # an explicit empty file — tests that want a root add one themselves,
    # so no test accidentally depends on this machine's real android-app
    # build output existing.
    path.write_text("roots: {}\n", encoding="utf-8")
    monkeypatch.setattr(file_share, "_manager", ConfigManager(path=path))


@pytest.fixture
def root_dir(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    (d / "readme.txt").write_text("hello", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "nested.bin").write_bytes(b"\x00\x01")
    return d


def test_empty_registry_has_no_roots():
    assert file_share.list_roots() == {}
    assert file_share.get_root_path("anything") is None


def test_add_and_list_root(root_dir):
    file_share.add_root("shared", str(root_dir))

    listed = file_share.list_roots()
    assert listed["shared"]["path"] == str(root_dir)
    assert listed["shared"]["exists"] is True


def test_add_root_rejects_a_nonexistent_path(tmp_path):
    with pytest.raises(ValueError):
        file_share.add_root("ghost", str(tmp_path / "does-not-exist"))


def test_add_root_rejects_empty_name(root_dir):
    with pytest.raises(ValueError):
        file_share.add_root("", str(root_dir))


def test_remove_root(root_dir):
    file_share.add_root("shared", str(root_dir))
    assert file_share.remove_root("shared") is True
    assert file_share.list_roots() == {}


def test_remove_root_returns_false_when_not_found():
    assert file_share.remove_root("nope") is False


def test_list_dir_top_level_sorted_dirs_first(root_dir):
    file_share.add_root("shared", str(root_dir))

    entries = file_share.list_dir("shared")

    names = [e["name"] for e in entries]
    assert names == ["sub", "readme.txt"]
    readme = next(e for e in entries if e["name"] == "readme.txt")
    assert readme["is_dir"] is False
    assert readme["size"] == 5


def test_list_dir_descends_into_a_subdirectory(root_dir):
    file_share.add_root("shared", str(root_dir))

    entries = file_share.list_dir("shared", "sub")

    assert [e["name"] for e in entries] == ["nested.bin"]


def test_list_dir_unknown_root_raises_key_error(root_dir):
    with pytest.raises(KeyError):
        file_share.list_dir("nope")


def test_resolve_safe_path_rejects_dotdot_traversal(root_dir):
    file_share.add_root("shared", str(root_dir))

    with pytest.raises(file_share.PathEscapeError):
        file_share.resolve_safe_path("shared", "../../etc/passwd")


def test_resolve_safe_path_rejects_an_absolute_path_override(root_dir, tmp_path):
    file_share.add_root("shared", str(root_dir))
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")

    # pathlib's `/` operator silently replaces the base when the RHS is
    # itself absolute — the real regression this guards against.
    with pytest.raises(file_share.PathEscapeError):
        file_share.resolve_safe_path("shared", str(outside))


def test_resolve_safe_path_allows_a_real_file_inside_the_root(root_dir):
    file_share.add_root("shared", str(root_dir))

    resolved = file_share.resolve_safe_path("shared", "readme.txt")

    assert resolved == (root_dir / "readme.txt").resolve()


def test_seed_if_missing_creates_the_default_android_builds_root(tmp_path):
    path = tmp_path / "fresh_file_share.yaml"

    file_share._seed_if_missing(path)

    assert path.is_file()
    manager = ConfigManager(path=path)
    assert "android-builds" in (manager.current.get("roots") or {})


def test_seed_if_missing_never_overwrites_an_existing_file(tmp_path):
    path = tmp_path / "file_share.yaml"
    path.write_text("roots: {}\n", encoding="utf-8")

    file_share._seed_if_missing(path)

    manager = ConfigManager(path=path)
    assert manager.current.get("roots") == {}
