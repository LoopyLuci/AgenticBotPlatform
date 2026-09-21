"""The credential vault (roadmap P6): encrypted at rest, bound to a site, never returned to the model."""
from __future__ import annotations

import json

import pytest

from bot import vault


@pytest.fixture(autouse=True)
def _vault_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("ABP_VAULT_KEY", raising=False)


def test_a_login_is_stored_encrypted_and_listed_without_its_secrets(tmp_path):
    vault.add("github", origin="https://GitHub.com/login", username="me", password="correct-horse-battery", totp_secret="GEZDGNBVGY3TQOJQ")
    raw = (tmp_path / "vault" / "vault.enc").read_bytes()
    assert b"correct-horse" not in raw and b"github" not in raw
    listing = vault.listing()
    assert listing == [{"name": "github", "origin": "https://github.com", "username": "me", "has_password": True, "has_totp": True, "note": ""}]
    assert "correct-horse" not in json.dumps(listing)


def test_a_secret_is_released_only_for_the_site_it_belongs_to():
    vault.add("bank", origin="https://bank.example", username="u", password="pw-123456")
    assert vault.value("bank", "password", page_url="https://bank.example/login?next=/x") == "pw-123456"
    assert vault.value("bank", "username", page_url="https://bank.example/") == "u"
    for wrong in ("https://bank.example.evil.test/login", "http://bank.example/login", "https://bank.example:8443/", "https://evil.test/"):
        with pytest.raises(vault.VaultError, match="belongs to"):
            vault.value("bank", "password", page_url=wrong)
    with pytest.raises(vault.VaultError, match="not an http"):
        vault.value("bank", "password", page_url="file:///etc/passwd")
    with pytest.raises(vault.VaultError, match="no stored credential"):
        vault.value("nothing", "password", page_url="https://bank.example/")
    with pytest.raises(vault.VaultError, match="no two-factor"):
        vault.value("bank", "totp", page_url="https://bank.example/")
    with pytest.raises(vault.VaultError, match="field must be"):
        vault.value("bank", "cvv", page_url="https://bank.example/")


def test_origins_are_normalised():
    assert vault.origin_of("https://Example.com:443/path") == "https://example.com"
    assert vault.origin_of("http://localhost:8080/x") == "http://localhost:8080"
    with pytest.raises(vault.VaultError):
        vault.origin_of("ftp://x.test")
    with pytest.raises(vault.VaultError):
        vault.origin_of("not a url")


def test_input_is_validated():
    with pytest.raises(vault.VaultError, match="letters, digits"):
        vault.add("bad name!", origin="https://x.test", password="p")
    with pytest.raises(vault.VaultError, match="at least"):
        vault.add("empty", origin="https://x.test")
    with pytest.raises(vault.VaultError, match="base32"):
        vault.add("t", origin="https://x.test", totp_secret="not base32 !!")


def test_removing_and_replacing():
    vault.add("a", origin="https://x.test", password="one-111")
    vault.add("a", origin="https://x.test", password="two-222")
    assert vault.value("a", "password", page_url="https://x.test/") == "two-222"
    assert vault.remove("a") and not vault.remove("a") and vault.listing() == []


def test_the_key_can_come_from_the_environment_and_the_wrong_key_fails(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ABP_VAULT_KEY", Fernet.generate_key().decode())
    vault.add("a", origin="https://x.test", password="secret-1")
    assert not (tmp_path / "vault" / "vault.key").exists(), "no key file when the key is supplied"
    monkeypatch.setenv("ABP_VAULT_KEY", Fernet.generate_key().decode())
    with pytest.raises(vault.VaultError, match="cannot be decrypted"):
        vault.listing()
    assert vault.secrets() == [], "redaction must never raise"
    monkeypatch.setenv("ABP_VAULT_KEY", "not-a-key")
    with pytest.raises(vault.VaultError, match="valid Fernet key"):
        vault.listing()


def test_secrets_for_redaction_include_passwords_and_totp_secrets_only():
    vault.add("a", origin="https://x.test", username="someuser", password="pass-word-1", totp_secret="GEZDGNBVGY3TQOJQ")
    labels = dict(vault.secrets())
    assert labels == {"vault:a:password": "pass-word-1", "vault:a:totp_secret": "GEZDGNBVGY3TQOJQ"}


def test_totp_matches_the_rfc_6238_test_vectors():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"          # the ASCII string 12345678901234567890
    assert vault.totp(secret, at=59, digits=8) == "94287082"
    assert vault.totp(secret, at=1111111109, digits=8) == "07081804"
    assert vault.totp(secret, at=20000000000, digits=8) == "65353130"
    assert vault.totp(secret, at=59) == "287082"
    assert vault.totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", at=59) == "287082"


def test_the_codes_come_from_the_vault_for_the_right_site():
    vault.add("a", origin="https://x.test", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    code = vault.value("a", "totp", page_url="https://x.test/2fa")
    assert len(code) == 6 and code.isdigit()


def test_the_command_line_lists_and_removes(capsys, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "cli-password-1")
    assert vault.main(["add", "cli", "--origin", "https://cli.test", "--username", "me"]) == 0
    assert vault.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "cli\thttps://cli.test\tme\tpassword=yes\ttotp=no" in out and "cli-password-1" not in out
    assert vault.main(["remove", "cli"]) == 0 and vault.main(["add", "bad name", "--origin", "https://x.test"]) == 1
