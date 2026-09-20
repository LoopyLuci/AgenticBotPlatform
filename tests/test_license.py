"""The repo is public and embedded by third parties, so it must carry a real
license and every place that declares one must agree."""
from __future__ import annotations

from bot.envfile import CODE_ROOT


def _read(name: str) -> str:
    return (CODE_ROOT / name).read_text(encoding="utf-8")


def test_license_file_is_the_mit_license_with_a_copyright_line():
    text = _read("LICENSE")
    assert text.startswith("MIT License")
    assert "Copyright (c)" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


def test_every_declared_license_says_mit():
    assert 'license = "MIT"' in _read("desktop-app/src-tauri/Cargo.toml")
    assert "licenses.mit" in _read("flake.nix")
    readme = _read("README.md")
    assert "## License" in readme and "MIT" in readme.split("## License", 1)[1]
