"""Getting a skill pack from the internet, safely (roadmap P4).

A skill is text that ends up in a model's context (and may carry scripts it could be told to
run), so a skill from a stranger is treated like any download from one:

1. **Fetch** - only `https://` git URLs on an allowed host (github.com, gitlab.com, codeberg.org,
   bitbucket.org, plus `native_agent.skills.allowed_hosts`), shallow clone with no prompts, no
   file:// or ssh:// remotes, symlinks off, time-limited.
2. **Scan** - the pack is checked before anything else happens: it must contain a valid
   `SKILL.md`; limits on file count and size; no symlinks; no compiled binaries; and its text and
   scripts are searched for patterns that deserve a person's attention (piping downloads into a
   shell, decoding and executing blobs, reading SSH keys or cloud credentials, dumping the
   environment, prompt-injection phrases). Findings are graded `block` or `warn`.
3. **Quarantine** - the pack is copied into a holding folder, not into your skills. A pack with
   a `block` finding cannot be approved at all.
4. **Approve** - only a person can approve (a command, or `POST /api/skills/quarantine/approve`);
   the agent has no tool for installing. Approval moves the pack into your skill folder.

**Signatures.** A pack may carry `SKILL.sig`: an Ed25519 signature (hex) over the pack's tree
digest. If its public key is one you listed under `native_agent.skills.trusted_keys`, the report says
"signed by a trusted publisher"; a valid signature by an unknown key or an invalid one is reported
as such. A signature never bypasses the scan and never approves anything.

**The scan is a filter, not a guarantee.** Pattern matching catches careless and obvious
attacks and cannot prove a pack safe. That is why approval is a person's decision and why
scripts only ever run through `run_shell`, with approval, like any command.

Not built: fetching from a registry (there is no single registry API to build against) or from
non-git sources, and automatic updates.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from bot import skill_packs
from bot.agent_runtime.errors import ToolError
from bot.agent_runtime.state import state_dir

DEFAULT_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org")
MAX_FILES = 200
MAX_TOTAL_BYTES = 2_000_000
MAX_FILE_BYTES = 300_000
GIT_TIMEOUT_S = 90
_BINARY_MAGIC = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"PK\x03\x04", b"\x1f\x8b", b"%PDF")
_ALLOWED_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf"}

# (severity, regex, what it means)
_PATTERNS: list[tuple[str, re.Pattern, str]] = [(sev, re.compile(rx, re.I | re.S), why) for sev, rx, why in [
    ("block", r"(curl|wget|iwr|invoke-webrequest)[^\n|]{0,200}\|\s*(sudo\s+)?(ba|z|da)?sh\b", "downloads something and pipes it into a shell"),
    ("block", r"(curl|wget)[^\n]{0,200}\|\s*(python3?|perl|ruby|node|powershell|pwsh)\b", "downloads something and pipes it into an interpreter"),
    ("block", r"base64\s+(-d|--decode)[^\n]{0,120}\|\s*(ba|z)?sh\b", "decodes a blob and runs it"),
    ("block", r"(exec|eval)\s*\(\s*(base64\.b64decode|codecs\.decode|bytes\.fromhex|zlib\.decompress)", "decodes a blob and executes it"),
    ("block", r"rm\s+-rf\s+(/|~|\$HOME|\*)(\s|$)", "recursively deletes a top-level location"),
    ("block", r"(~|\$HOME|%USERPROFILE%)[/\\]\.ssh", "reads the SSH folder"),
    ("block", r"\.aws[/\\]credentials|\.config[/\\]gcloud|\.azure[/\\]|\.kube[/\\]config", "reads cloud credentials"),
    ("warn", r"id_rsa|id_ed25519|\.npmrc|\.pypirc|\.netrc", "mentions private keys or package-registry credentials"),
    ("warn", r"\b(printenv|env\s*\||os\.environ\b(?!\.get\(['\"](HOME|PATH|USER|LANG)))", "reads environment variables (which may hold secrets)"),
    ("warn", r"(ignore|disregard|forget)\s+(all\s+|any\s+|your\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)", "contains a prompt-injection phrase"),
    ("warn", r"do not (tell|inform|mention to) the user|without (asking|telling|informing) (the )?user|hide this from the user", "asks the agent to hide something from the user"),
    ("warn", r"(chmod\s+\+x|sudo\s|Set-ExecutionPolicy|reg\s+add)", "changes permissions or system settings"),
    ("warn", r"https?://\S*(pastebin|transfer\.sh|ngrok|webhook\.site|requestbin|discord(app)?\.com/api/webhooks)", "talks to a paste, tunnel or webhook service"),
    ("warn", r"\b(subprocess|os\.system|child_process|Runtime\.getRuntime)\b", "runs other programs"),
]]


@dataclass
class Finding:
    severity: str
    file: str
    message: str


@dataclass
class Report:
    ok: bool = True                       # False = at least one block finding or a structural problem
    findings: list[Finding] = field(default_factory=list)
    files: int = 0
    bytes: int = 0
    digest: str = ""
    signature: str = "none"               # none | trusted | untrusted-key | invalid
    name: str = ""
    description: str = ""

    def add(self, severity: str, file: str, message: str) -> None:
        self.findings.append(Finding(severity, file, message))
        if severity == "block":
            self.ok = False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "name": self.name, "description": self.description, "files": self.files, "bytes": self.bytes,
                "digest": self.digest, "signature": self.signature,
                "findings": [f.__dict__ for f in self.findings]}


def _cfg() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("skills")) or {}
    except Exception:  # noqa: BLE001
        return {}


def allowed_hosts() -> set[str]:
    return {h.lower() for h in DEFAULT_HOSTS} | {str(h).lower() for h in (_cfg().get("allowed_hosts") or [])}


# ---- fetching -----------------------------------------------------------------------
def validate_url(url: str) -> str:
    parts = urlparse(url.strip())
    if parts.scheme != "https":
        raise ToolError("only https:// git URLs can be fetched")
    if parts.username or parts.password:
        raise ToolError("URLs with embedded credentials are not accepted")
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts():
        raise ToolError(f"{host or 'that host'} is not an allowed source (allowed: {', '.join(sorted(allowed_hosts()))}; "
                        "add more under native_agent.skills.allowed_hosts)")
    if not parts.path.strip("/"):
        raise ToolError("that URL has no repository path")
    return url.strip()


def fetch_git(url: str, ref: Optional[str] = None, subdir: str = "") -> tuple[Path, Path]:
    """Clone into a temporary folder. Returns (that temporary folder, the pack's folder inside it: the repository root or `subdir`)."""
    url = validate_url(url)
    if ref and not re.match(r"^[A-Za-z0-9._/-]{1,100}$", ref):
        raise ToolError("that git ref is not valid")
    dest = Path(tempfile.mkdtemp(prefix="abp-skill-"))
    cmd = ["git", "-c", "core.symlinks=false", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
           "clone", "--depth", "1", "--no-tags", "--single-branch", *(["--branch", ref] if ref else []), "--", url, str(dest / "src")]
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "PATH": __import__("os").environ.get("PATH", ""),
           "HOME": str(dest), "USERPROFILE": str(dest), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise ToolError(f"could not fetch the repository: {exc}")
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise ToolError(f"git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    src = dest / "src"
    shutil.rmtree(src / ".git", ignore_errors=True)
    if subdir:
        pack = (src / subdir).resolve()
        if not pack.is_dir() or src.resolve() not in pack.parents and pack != src.resolve():
            shutil.rmtree(dest, ignore_errors=True)
            raise ToolError("subdir is not a folder inside the repository")
        return dest, pack
    return dest, src


# ---- scanning -----------------------------------------------------------------------
def tree_digest(root: Path) -> str:
    """A hash of the pack's files (names and contents), independent of timestamps and of SKILL.sig."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "SKILL.sig":
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest() + b"\n")
    return h.hexdigest()


def _trusted_keys() -> list[str]:
    return [str(k).strip().lower() for k in (_cfg().get("trusted_keys") or [])]


def verify_signature(root: Path, digest: str) -> str:
    """"none", "trusted", "untrusted-key" or "invalid"."""
    sig_file = root / "SKILL.sig"
    if not sig_file.is_file():
        return "none"
    try:
        lines = sig_file.read_text(encoding="utf-8").split()
        public_hex, sig_hex = lines[0].lower(), lines[1].lower()
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        try:
            key.verify(bytes.fromhex(sig_hex), digest.encode("ascii"))
        except InvalidSignature:
            return "invalid"
    except (IndexError, ValueError, ImportError):
        return "invalid"
    return "trusted" if public_hex in _trusted_keys() else "untrusted-key"


def scan(root: Path) -> Report:
    root = Path(root)
    report = Report()
    skill = skill_packs.load_dir(root, "user") if (root / "SKILL.md").is_file() else None
    if skill is None:
        report.add("block", "SKILL.md", "no valid SKILL.md (a folder with a SKILL.md whose name is lowercase letters, digits and hyphens)")
        return report
    report.name, report.description = skill.name, skill.description
    for problem in skill.problems:
        report.add("warn", "SKILL.md", problem)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            report.add("block", rel, "is a symbolic link")
            continue
        if not p.is_file():
            continue
        report.files += 1
        size = p.stat().st_size
        report.bytes += size
        if report.files > MAX_FILES:
            report.add("block", rel, f"more than {MAX_FILES} files")
            break
        if size > MAX_FILE_BYTES:
            report.add("block", rel, f"is larger than {MAX_FILE_BYTES} bytes")
            continue
        head = p.read_bytes()[:16]
        if p.suffix.lower() not in _ALLOWED_BINARY_EXT and any(head.startswith(m) for m in _BINARY_MAGIC):
            report.add("block", rel, "is a compiled binary or an archive")
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            if p.suffix.lower() not in _ALLOWED_BINARY_EXT:
                report.add("warn", rel, "is not readable text")
            continue
        for severity, rx, why in _PATTERNS:
            if rx.search(text):
                report.add(severity, rel, why)
    if report.bytes > MAX_TOTAL_BYTES:
        report.add("block", "", f"the pack is larger than {MAX_TOTAL_BYTES} bytes")
    report.digest = tree_digest(root)
    report.signature = verify_signature(root, report.digest)
    if report.signature == "invalid":
        report.add("block", "SKILL.sig", "the signature does not verify")
    return report


# ---- quarantine -----------------------------------------------------------------------
def quarantine_root() -> Path:
    return state_dir("skill_quarantine")


def stage(pack: Path, *, source: str) -> dict:
    """Scan `pack` and copy it into quarantine. Returns the report (with the quarantine name)."""
    report = scan(pack)
    name = report.name or re.sub(r"[^a-z0-9-]+", "-", pack.name.lower()).strip("-") or "pack"
    target = quarantine_root() / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(pack, target, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=False, dirs_exist_ok=False)
    info = {**report.to_dict(), "source": source, "quarantined_at": time.time(), "quarantine_name": name}
    (quarantine_root() / f"{name}.report.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    return info


def install_from_git(url: str, ref: Optional[str] = None, subdir: str = "") -> dict:
    temp, pack = fetch_git(url, ref, subdir)
    try:
        return stage(pack, source=f"{url}" + (f"@{ref}" if ref else ""))
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def install_from_dir(path: str) -> dict:
    """Stage a local folder (for packs you already have). Same scan, same quarantine."""
    pack = Path(path).expanduser().resolve()
    if not pack.is_dir():
        raise ToolError(f"{path!r} is not a folder")
    return stage(pack, source=f"local:{pack}")


def list_quarantine() -> list[dict]:
    out = []
    for f in sorted(quarantine_root().glob("*.report.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _report_for(name: str) -> dict:
    f = quarantine_root() / f"{name}.report.json"
    if not re.match(r"^[a-z0-9-]{1,64}$", name) or not f.is_file():
        raise ToolError(f"nothing named {name!r} is in quarantine")
    return json.loads(f.read_text(encoding="utf-8"))


def approve(name: str) -> dict:
    """A person approves a quarantined pack: it moves into the user's skill folder. Refused if the scan blocked it,
    or if the files changed since they were scanned."""
    report = _report_for(name)
    folder = quarantine_root() / name
    if not report.get("ok"):
        raise ToolError("this pack has blocking findings and cannot be approved: " + "; ".join(
            f"{f['file']}: {f['message']}" for f in report["findings"] if f["severity"] == "block")[:400])
    if tree_digest(folder) != report.get("digest"):
        raise ToolError("the quarantined files changed after they were scanned; fetch the pack again")
    dest_root = skill_packs.user_root()
    if dest_root is None:
        raise ToolError("no folder to install skills into")
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / name
    if dest.exists():
        raise ToolError(f"you already have a skill named {name!r}; remove it first")
    shutil.move(str(folder), str(dest))
    (quarantine_root() / f"{name}.report.json").unlink(missing_ok=True)
    return {"installed": name, "path": str(dest)}


def reject(name: str) -> bool:
    _report_for(name)
    shutil.rmtree(quarantine_root() / name, ignore_errors=True)
    (quarantine_root() / f"{name}.report.json").unlink(missing_ok=True)
    return True
