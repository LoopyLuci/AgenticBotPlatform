"""Putting an adopted project in its own private GitHub repo, safely.

`publish(path)`: a .gitignore that fits its stacks (merged into an existing one), `git init` if needed, a scan of
everything that would be committed (secret-shaped strings, private keys, .env files, files over 50 MB: any finding
stops it, and only the file and the kind of finding are reported, never the value), a commit, then
`gh repo create <owner>/<name> --private --source <path> --push` (or a push to the origin it already has).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

PATTERNS = {
    "openai/anthropic key": r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}",
    "openrouter key": r"\bsk-or-v1-[A-Za-z0-9]{20,}",
    "github token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "github pat": r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    "aws key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "google api key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |)PRIVATE KEY(?: BLOCK)?-----",
    "telegram bot token": r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b",
    "hf token": r"\bhf_[A-Za-z0-9]{30,}\b",
    "stripe live key": r"\b[rs]k_live_[A-Za-z0-9]{20,}",
    "discord token": r"\b[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27}\b",
    "cloudflare token": r"(?i)cloudflare[^\n]{0,20}['\"][A-Za-z0-9_-]{40}['\"]",
}
SECRET_FILES = re.compile(r"(^|/)(\.env(\.[\w-]+)?|id_(rsa|ed25519|ecdsa)|.*\.pem|.*\.p12|.*\.pfx|.*\.keystore|.*\.jks|"
                          r"credentials\.json|secrets?\.(json|ya?ml|toml)|.*\.kdbx)$", re.I)
SAFE_ENV = re.compile(r"(^|/)\.env\.(example|sample|template|dist|defaults?)$", re.I)
MAX_FILE = 50 << 20

IGNORE = {
    "common": ["# secrets and local state", ".env", ".env.*", "!.env.example", "!.env.sample", "*.pem", "*.key", "*.p12",
               "*.pfx", "*.keystore", "*.jks", "secrets.*", "credentials.json", "*.log", "logs/", ".DS_Store",
               "Thumbs.db", ".idea/", ".vscode/*", "!.vscode/extensions.json", "tmp/", "temp/", "*.tmp", "*.bak",
               "*.swp", "*.sock", "*.qmp",
               "# build output and bundled environments", "dist-*/", "dist_*/", "build-*/", "build_*/", "dist-electron/",
               "_pkg_venv/", "*.pkg", "*.msi", "*.dmg", "*.AppImage"],
    "python": ["# python", "__pycache__/", "*.py[cod]", ".venv/", "venv/", "env/", "*.egg-info/", ".eggs/", "build/",
               "dist/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/", ".coverage", "htmlcov/",
               ".ipynb_checkpoints/"],
    "node": ["# node", "node_modules/", "dist/", "build/", ".next/", ".nuxt/", ".svelte-kit/", ".turbo/", ".parcel-cache/",
             "coverage/", "npm-debug.log*", "yarn-error.log*", ".wrangler/", ".vercel/"],
    "rust": ["# rust", "target/", "**/*.rs.bk"],
    "go": ["# go", "/bin/", "*.exe", "*.test"],
    "gradle": ["# gradle / android", ".gradle/", "build/", "local.properties", "*.apk", "*.aab", "captures/",
               ".externalNativeBuild/", ".cxx/"],
    "nix": ["# nix", "result", "result-*"],
    "elixir": ["# elixir", "_build/", "deps/", "*.ez", "erl_crash.dump"],
    "models": ["# model weights and large binaries", "*.gguf", "*.safetensors", "*.ckpt", "*.pt", "*.pth", "*.onnx",
               "*.bin", "*.iso", "*.qcow2", "*.vhdx", "*.img", "*.zip", "*.7z", "*.tar.gz", "*.mp4", "*.mkv", "*.wav"],
}


class PublishError(RuntimeError):
    pass


def _git(path: Path, *args: str, check: bool = True, timeout: float = 600) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise PublishError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout


def gitignore(path: Path, stacks: list[str]) -> bool:
    """Merge the ignore rules for these stacks into the project's .gitignore; True if it changed."""
    f = path / ".gitignore"
    old = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
    have = {ln.strip() for ln in old.splitlines()}
    groups = ["common", *[s for s in ("python", "node", "rust", "go", "gradle", "nix", "elixir") if s in stacks
                          or (s == "gradle" and "gradle" in stacks)], "models"]
    add: list[str] = []
    for g in groups:
        new = [ln for ln in IGNORE[g] if ln.startswith("#") or ln not in have]
        if any(not ln.startswith("#") for ln in new):
            add += ["", *new]
    if not add:
        return False
    text = (old.rstrip("\n") + "\n" if old else "") + ("\n# added by abp_modkit" if old else "# written by abp_modkit") + \
        "\n".join(add) + "\n"
    f.write_text(text, encoding="utf-8", newline="\n")
    return True


def scan(path: Path, files: list[str]) -> list[tuple[str, str]]:
    """(file, kind) for anything that must not be published. Never includes the matched value."""
    found: list[tuple[str, str]] = []
    for rel in files:
        p = path / rel
        if SECRET_FILES.search(rel) and not SAFE_ENV.search(rel):
            found.append((rel, "a secrets file"))
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE:
            found.append((rel, f"{size >> 20} MB (over GitHub's limits; ignore it or use LFS)"))
            continue
        if size > 3_000_000:
            continue
        try:
            text = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for kind, pat in PATTERNS.items():
            if re.search(pat, text):
                found.append((rel, kind))
    return found


def scan_history(path: Path, rev: str = "--all") -> list[tuple[str, str]]:
    """(file, kind) for anything in the repository's existing history that must not be published: blobs over the size
    limit, and secret-shaped strings in any line a commit added. Never includes the matched value."""
    found: list[tuple[str, str]] = []
    objs = _git(path, "rev-list", "--objects", rev, timeout=1800).splitlines()
    names = {o.split(" ", 1)[0]: (o.split(" ", 1)[1] if " " in o else "") for o in objs}
    r = subprocess.run(["git", "-C", str(path), "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                       input="\n".join(names), capture_output=True, text=True, timeout=1800)
    for row in r.stdout.splitlines():
        parts = row.split(" ")
        if len(parts) == 3 and parts[1] == "blob" and int(parts[2]) > MAX_FILE:
            found.append((names.get(parts[0]) or parts[0], f"{int(parts[2]) >> 20} MB in its history"))
    p = subprocess.Popen(["git", "-C", str(path), "log", "-p", rev, "--no-color", "--no-ext-diff", "--format=@@commit %h"],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    current, seen = "?", set()
    for line in p.stdout:
        if line.startswith("+++ b/"):
            current = line[6:].rstrip("\n")
            if SECRET_FILES.search(current) and not SAFE_ENV.search(current) and (current, "a secrets file") not in seen:
                seen.add((current, "a secrets file"))
                found.append((current, "a secrets file, in its history"))
        elif line.startswith("+") and not line.startswith("+++") and len(line) < 5000:
            for kind, pat in PATTERNS.items():
                if (current, kind) not in seen and re.search(pat, line):
                    seen.add((current, kind))
                    found.append((current, f"{kind}, in its history"))
    p.wait()
    return found


NESTED_ASIDE = ".git.abp-nested"


def _nested_repos(path: Path, log: list[str]) -> None:
    """A folder inside the project with its own .git (cargo new makes one) keeps git from adding its files. One with
    no commits holds no history: its .git is set aside (renamed, not deleted) and ignored. One with commits is left
    as it is and reported: it is a project of its own (a submodule)."""
    for g in sorted(path.rglob(".git")):
        d = g.parent
        if d == path or any(part in ("node_modules", "target", ".venv", "vendor") for part in d.relative_to(path).parts):
            continue
        if not g.is_dir():
            continue
        has = subprocess.run(["git", "-C", str(d), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
        rel = d.relative_to(path).as_posix()
        if has:
            raise PublishError(f"{rel} is a git repository of its own, with history: make it a submodule, or move its "
                               ".git away, then publish again")
        g.rename(d / NESTED_ASIDE)
        log.append(f"{rel}: its empty .git set aside as {NESTED_ASIDE}")


def _to_commit(path: Path) -> list[str]:
    out = _git(path, "ls-files", "--others", "--cached", "--exclude-standard", "-z")
    return [x for x in out.split("\0") if x]


def _unreviewed(bad: list[tuple[str, str]], allow: list[str]) -> list[tuple[str, str]]:
    """Findings a person has not cleared. `allow` clears only findings made from a file's NAME (a Helm template called
    secrets.yaml that holds no values); a secret-shaped string or an oversized file is never cleared this way."""
    ok = {a.replace("\\", "/") for a in allow}
    return [(f, k) for f, k in bad if not (f in ok and k.startswith("a secrets file"))]


def publish(path: str | Path, *, stacks: list[str], owner: str = "LoopyLuci", name: str = "", only: list[str] | None = None,
            message: str = "", create: bool = True, push: bool = True, description: str = "",
            allow: list[str] | None = None) -> dict:
    """See the module docstring. `only`: commit just these files when the project already has commits (its other
    uncommitted work is left alone); a new repository commits everything that is not ignored."""
    path = Path(path).resolve()
    if not shutil.which("git"):
        raise PublishError("git is not installed")
    log: list[str] = []
    g = path / ".git"
    fresh = not g.exists() or (g.is_dir() and not (g / "HEAD").is_file())   # (a .git with only hooks is no repo)
    if fresh:
        _git(path, "init", "-q", "-b", "main")
        log.append("git init")
    has_commits = bool(_git(path, "rev-parse", "--verify", "-q", "HEAD", check=False).strip())
    if not has_commits and gitignore(path, stacks):       # an established repo keeps its own .gitignore as it is
        log.append(".gitignore written")
    if not has_commits:
        # files staged before (a no-commit repo's index) that .gitignore now ignores: unstage them; they stay on disk
        staged = [f for f in _git(path, "ls-files", "--cached", "-i", "--exclude-standard", "-z").split("\0") if f]
        if staged:
            r = subprocess.run(["git", "-C", str(path), "rm", "--cached", "-q", "--pathspec-from-file=-",
                                "--pathspec-file-nul"], input="\0".join(staged), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
            if r.returncode != 0:
                raise PublishError(f"git rm --cached: {(r.stderr or r.stdout).strip()[-600:]}")
            log.append(f"unstaged {len(staged)} ignored file(s) (they stay on disk)")
        _nested_repos(path, log)
        ign = path / ".gitignore"
        text = ign.read_text(encoding="utf-8") if ign.is_file() else ""
        if any(NESTED_ASIDE in x for x in log) and NESTED_ASIDE not in text:
            ign.write_text(text.rstrip("\n") + f"\n{NESTED_ASIDE}/\n", encoding="utf-8", newline="\n")
    origin0 = _git(path, "remote", "get-url", "origin", check=False).strip()
    remote_empty = not origin0 or not _git(path, "ls-remote", "--heads", "origin", check=False, timeout=120).strip()
    if has_commits and remote_empty and (create or push):
        bad = _unreviewed(scan_history(path, "HEAD"), allow or [])   # this branch's whole history is about to go
        if bad:
            raise PublishError("not publishing: its history has what must not go to GitHub (rewrite it, or publish "
                               "without it):\n" + "\n".join(f"  {f}: {k}" for f, k in bad[:40]))
        log.append("history scanned: clean")
    files = _to_commit(path) if (not has_commits or only is None) else [f for f in only if (path / f).exists()]
    if len(files) > 20000:
        raise PublishError(f"{len(files)} files would be committed: add the generated/vendored folders to .gitignore first")
    bad = _unreviewed(scan(path, files), allow or [])
    if bad:
        raise PublishError("not publishing; these must not go to GitHub (move them out or add them to .gitignore):\n" +
                           "\n".join(f"  {f}: {k}" for f, k in bad[:40]))
    if files:
        # paths on stdin: a long list would pass Windows' command-line limit
        r = subprocess.run(["git", "-C", str(path), "add", "--pathspec-from-file=-", "--pathspec-file-nul"],
                           input="\0".join(files), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=1800)
        if r.returncode != 0:
            raise PublishError(f"git add: {(r.stderr or r.stdout).strip()[-600:]}")
        if _git(path, "diff", "--cached", "--name-only").strip():
            email = _git(path, "config", "user.email", check=False).strip()
            args = ["commit", "-q", "-m", message or ("Initial commit" if not has_commits else "Adopt as an ABP module")]
            if has_commits and only is not None:
                args += ["--", *files]                     # just these: whatever else was staged stays staged
            if not email:
                args = ["-c", "user.name=LoopyLuci", "-c", "user.email=noreply@github.com", *args]
            _git(path, *args)
            log.append(f"committed {len(files)} file(s)")
    origin = _git(path, "remote", "get-url", "origin", check=False).strip()
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or "main"
    url = origin
    if not origin and create:
        gh = shutil.which("gh")
        if not gh:
            raise PublishError("the GitHub CLI (gh) is needed to create the repository: https://cli.github.com")
        name = name or path.name
        r = subprocess.run([gh, "repo", "create", f"{owner}/{name}", "--private", "--source", str(path), "--remote",
                            "origin", *(["--description", description[:300]] if description else []),
                            *(["--push"] if push else [])], capture_output=True, text=True, timeout=900,
                           env={**os.environ, "GH_PROMPT_DISABLED": "1"})
        if r.returncode != 0:
            raise PublishError(f"gh repo create: {(r.stderr or r.stdout).strip()[-600:]}")
        url = f"https://github.com/{owner}/{name}"
        log.append(f"created {url} (private)" + (" and pushed" if push else ""))
    elif origin and push:
        _git(path, "push", "-q", "-u", "origin", branch, timeout=1800)
        log.append(f"pushed {branch} to {origin}")
    return {"path": str(path), "repo": url or None, "branch": branch, "fresh": fresh, "log": log}
