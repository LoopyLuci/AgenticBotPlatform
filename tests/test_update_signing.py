"""scripts/update_signing.py — detached Ed25519 signing of the desktop update
installer. The desktop app refuses an update whose `<installer>.sig` doesn't
verify against the public key compiled into updater.rs, so these tests pin
the properties that keep releases installable AND keep forged ones out.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_signing as us  # noqa: E402


def _matching_updater_rs(tmp_path: Path, key: Ed25519PrivateKey) -> Path:
    rs = tmp_path / "updater.rs"
    rs.write_text(
        "const UPDATE_PUBLIC_KEY: [u8; 32] = [\n    "
        + us.as_rust_bytes(us.public_key_bytes(key))
        + ",\n];\n",
        encoding="utf-8",
    )
    return rs


def test_the_public_key_embedded_in_the_app_parses_as_32_bytes():
    raw = us.embedded_public_key()
    assert len(raw) == 32
    Ed25519PublicKey.from_public_bytes(raw)  # a valid point


def test_the_rust_source_parser_reads_back_exactly_what_was_embedded(tmp_path):
    key = Ed25519PrivateKey.generate()
    rs = _matching_updater_rs(tmp_path, key)
    assert us.embedded_public_key(rs) == us.public_key_bytes(key)


def test_signature_round_trips_and_is_raw_64_bytes(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(us, "embedded_public_key", lambda: us.public_key_bytes(key))
    installer = tmp_path / "AgenticBotPlatform_9.9.9_x64-setup.exe"
    installer.write_bytes(b"MZ fake installer bytes")

    sig_path = us.sign_installer(installer, key)

    assert sig_path.name == installer.name + ".sig"
    sig = sig_path.read_bytes()
    assert len(sig) == 64
    assert us.verify(installer.read_bytes(), sig, us.public_key_bytes(key))


def test_a_tampered_installer_fails_verification(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(us, "embedded_public_key", lambda: us.public_key_bytes(key))
    installer = tmp_path / "x-setup.exe"
    installer.write_bytes(b"original")
    sig = us.sign_installer(installer, key).read_bytes()

    assert not us.verify(b"tampered", sig, us.public_key_bytes(key))


def test_signing_with_a_key_that_does_not_match_the_embedded_key_is_refused(tmp_path, monkeypatch):
    embedded_owner = Ed25519PrivateKey.generate()
    monkeypatch.setattr(us, "embedded_public_key", lambda: us.public_key_bytes(embedded_owner))
    installer = tmp_path / "x-setup.exe"
    installer.write_bytes(b"bytes")

    with pytest.raises(ValueError, match="does NOT match"):
        us.sign_installer(installer, Ed25519PrivateKey.generate())
    assert not (tmp_path / "x-setup.exe.sig").exists()


def test_a_missing_signing_key_is_a_clear_error_not_an_unsigned_release(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_UPDATE_SIGNING_KEY", str(tmp_path / "nope.pem"))
    with pytest.raises(FileNotFoundError, match="MUST be signed"):
        us.load_private_key()


def test_generate_refuses_to_overwrite_an_existing_key(tmp_path):
    path = tmp_path / "key.pem"
    us.generate_key(path)
    with pytest.raises(FileExistsError):
        us.generate_key(path)


def _release_source() -> str:
    return (Path(__file__).resolve().parent.parent / "scripts" / "publish_release.py").read_text(encoding="utf-8")


def test_publish_release_checks_the_signing_key_before_any_irreversible_step():
    """The key check is part of the pre-flight that runs before anything is
    modified — long before a tag or push (behaviour: tests/test_publish_release.py)."""
    import inspect

    import publish_release
    import release_guard

    assert "check_signing()" in inspect.getsource(release_guard.run_preflight)
    main = inspect.getsource(publish_release._main_body)
    assert main.index("run_preflight(") < main.index("_run_release(")


def test_the_signing_preflight_rejects_a_missing_or_mismatched_key(monkeypatch):
    import release_guard

    monkeypatch.setattr(us, "load_private_key", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no key")))
    assert not release_guard.check_signing().ok

    other = Ed25519PrivateKey.generate()
    monkeypatch.setattr(us, "load_private_key", lambda *a, **k: other)
    monkeypatch.setattr(us, "embedded_public_key", lambda *a, **k: bytes(32))
    bad = release_guard.check_signing()
    assert not bad.ok and "not the one embedded" in bad.detail

    monkeypatch.setattr(us, "embedded_public_key", lambda *a, **k: us.public_key_bytes(other))
    assert release_guard.check_signing().ok


def test_the_real_signature_is_made_after_the_pushes_and_right_before_the_upload():
    """The pre-push hook used to rebuild the installer during `git push`,
    replacing the file. Signing before it signed bytes that were never
    uploaded, so installed apps rejected the update (shipped once in
    v0.7.24)."""
    source = _release_source()
    pushed = source.index('"git", "push", "origin", tag')
    final_sign = source.rindex("update_signing.sign_installer")
    upload = source.index('"gh", "release", "create"')
    assert pushed < final_sign < upload
    assert "str(sig)" in source


def test_the_published_assets_are_verified_after_upload():
    source = _release_source()
    assert source.index('"gh", "release", "create"') < source.rindex("verify_published_assets(tag, current_installer")
    assert "sha256:" in source and "update_signing.verify(" in source
