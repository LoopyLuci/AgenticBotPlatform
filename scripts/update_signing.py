"""Detached Ed25519 signing for the desktop auto-update installer.

The desktop app (desktop-app/src-tauri/src/updater.rs) refuses to run an
update installer unless a `<installer>.sig` asset next to it verifies
against the public key compiled into the app. That is what stops a hijacked
GitHub account or release from pushing code to every installed copy.

The PRIVATE key never lives in the repo. It defaults to
~/.abp-release/update_signing_key.pem (override with the
ABP_UPDATE_SIGNING_KEY environment variable). Back it up: if it is lost,
already-installed apps reject every future update until reinstalled by hand.

Usage:
    python scripts/update_signing.py --generate        # once; prints the Rust bytes
    python scripts/update_signing.py --print-public    # public key as Rust bytes
    python scripts/update_signing.py <installer.exe>   # writes <installer.exe>.sig
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parent.parent
UPDATER_RS = ROOT / "desktop-app" / "src-tauri" / "src" / "updater.rs"
DEFAULT_KEY_PATH = Path.home() / ".abp-release" / "update_signing_key.pem"


def key_path() -> Path:
    override = os.environ.get("ABP_UPDATE_SIGNING_KEY")
    return Path(override) if override else DEFAULT_KEY_PATH


def generate_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        raise FileExistsError(f"{path} already exists — refusing to overwrite a signing key")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return key


def load_private_key(path: Path | None = None) -> Ed25519PrivateKey:
    path = path or key_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"update signing key not found at {path}. Releases MUST be signed — installed apps "
            "reject unsigned updates. Restore the key from backup (or run --generate ONLY for a "
            "brand-new project; a new key means embedding its public key and one manual reinstall)."
        )
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"{path} is not an Ed25519 private key")
    return key


def public_key_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def as_rust_bytes(raw: bytes) -> str:
    return ", ".join(f"0x{b:02x}" for b in raw)


def embedded_public_key(updater_rs: Path = UPDATER_RS) -> bytes:
    """The public key compiled into the app, parsed out of updater.rs."""
    text = updater_rs.read_text(encoding="utf-8")
    match = re.search(r"UPDATE_PUBLIC_KEY:\s*\[u8;\s*32\]\s*=\s*\[(.*?)\];", text, re.DOTALL)
    if not match:
        raise ValueError(f"couldn't find UPDATE_PUBLIC_KEY in {updater_rs}")
    raw = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", match.group(1)))
    if len(raw) != 32:
        raise ValueError(f"UPDATE_PUBLIC_KEY in {updater_rs} is {len(raw)} bytes, expected 32")
    return raw


def verify(data: bytes, signature: bytes, public_key: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


def sign_installer(installer: Path, key: Ed25519PrivateKey | None = None) -> Path:
    """Writes `<installer>.sig` (raw 64 bytes) next to the installer and
    checks it against the key embedded in updater.rs, so a release can never
    ship a signature the app itself would reject."""
    key = key or load_private_key()
    data = installer.read_bytes()
    signature = key.sign(data)
    if not verify(data, signature, embedded_public_key()):
        raise ValueError(
            "the signing key does NOT match the public key embedded in updater.rs — installed apps "
            "would reject this update. Use the original key, or embed the new public key first."
        )
    sig_path = installer.with_name(installer.name + ".sig")
    sig_path.write_bytes(signature)
    return sig_path


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--generate":
        key = generate_key(key_path())
        print(f"wrote {key_path()}")
        print("embed in updater.rs UPDATE_PUBLIC_KEY:\n" + as_rust_bytes(public_key_bytes(key)))
        return 0
    if len(argv) == 2 and argv[1] == "--print-public":
        print(as_rust_bytes(public_key_bytes(load_private_key())))
        return 0
    if len(argv) == 2:
        print(sign_installer(Path(argv[1])))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
