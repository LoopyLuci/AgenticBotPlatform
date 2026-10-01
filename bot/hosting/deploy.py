"""Publish a folder (a site's files, built or not) somewhere other than this machine's edge.

    deploy.publish(site, target)     target = {"account": <id>, ...per kind}; returns {"url", "detail", ...}

  ssh            a VPS or your own server: the files go to <web_root>/<site>/releases/<time> over SSH (tar through the
                 connection, nothing to install locally but OpenSSH), `current` is switched to it at once, the last
                 five releases are kept for rollback. With server_setup() Caddy serves it there with automatic HTTPS.
  ftp            shared web hosting (cPanel, Plesk, DirectAdmin...): only changed files are uploaded (size + time),
                 files no longer in the site removed if the target says so; FTPS unless turned off
  netlify        Netlify's file-digest deploy: only files Netlify does not have yet are uploaded
  vercel         Vercel's deployment API: files uploaded by SHA-1, a production deployment created
  cloudflare     Cloudflare Pages, through Cloudflare's own `wrangler pages deploy` (run by npx)
  github         GitHub Pages: the files force-pushed to a branch (gh-pages), Pages switched on for it
"""
from __future__ import annotations

import base64
import ftplib
import hashlib
import io
import os
import posixpath
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from bot.hosting import accounts
from bot.hosting.store import HostingError

Log = Callable[[str], None]
_SKIP = {".git", ".DS_Store", "Thumbs.db", "node_modules", ".abp-site.json"}


def files_of(folder: Path) -> list[tuple[str, Path]]:
    """(posix relative path, file) for every file to publish."""
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not any(part in _SKIP for part in p.relative_to(folder).parts):
            out.append((p.relative_to(folder).as_posix(), p))
    if not out:
        raise HostingError(f"{folder} has no files to publish")
    return out


# ---- SSH ------------------------------------------------------------------------------------------------------ #

def _ssh_base(acc: dict) -> list[str]:
    exe = shutil.which("ssh")
    if not exe:
        raise HostingError("OpenSSH's ssh is not installed here")
    args = [exe, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
            "-p", accounts.setting(acc, "port", "22")]
    key = accounts.setting(acc, "key_path")
    if key:
        args += ["-i", os.path.expanduser(key)]
    return args + [f"{accounts.setting(acc, 'user')}@{accounts.setting(acc, 'host')}"]


def ssh_run(acc: dict, command: str, stdin: Optional[bytes] = None, timeout: float = 600.0) -> str:
    try:
        r = subprocess.run(_ssh_base(acc) + [command], input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise HostingError(f"the server did not finish within {timeout:.0f}s") from e
    out = (r.stdout or b"").decode(errors="replace") + (r.stderr or b"").decode(errors="replace")
    if r.returncode != 0:
        if r.returncode == 255:
            raise HostingError(f"cannot connect over SSH: {out.strip()[-400:]}")
        raise HostingError(f"the command failed on the server ({r.returncode}): {out.strip()[-600:]}")
    return out


def ssh_check(acc: dict) -> str:
    out = ssh_run(acc, "uname -srm; id -un; command -v caddy || true; sudo -n true 2>/dev/null && echo SUDO_OK || echo SUDO_NO", timeout=30)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    caddy = next((ln for ln in lines if ln.endswith("/caddy")), None)
    return (f"{lines[0] if lines else '?'}; user {lines[1] if len(lines) > 1 else '?'}; "
            f"{'Caddy installed' if caddy else 'no Caddy yet'}; {'passwordless sudo' if 'SUDO_OK' in out else 'no passwordless sudo'}")


def _web_root(acc: dict) -> str:
    return accounts.setting(acc, "web_root", "/var/www").rstrip("/")


def ssh_publish(site: dict, folder: Path, acc: dict, log: Log) -> dict:
    sid = site["id"]
    release = time.strftime("%Y%m%d-%H%M%S")
    base = f"{_web_root(acc)}/{sid}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for rel, p in files_of(folder):
            t.add(p, arcname=rel)
    log(f"uploading {buf.tell() >> 10} KB to {accounts.setting(acc, 'host')}:{base}/releases/{release}")
    q = shlex.quote
    script = (f"set -e; mkdir -p {q(base)}/releases/{release} && tar -xzf - -C {q(base)}/releases/{release} && "
              f"ln -sfn releases/{release} {q(base)}/current.tmp && mv -Tf {q(base)}/current.tmp {q(base)}/current && "
              f"cd {q(base)}/releases && ls -1 | sort | head -n -5 | xargs -r rm -rf")
    ssh_run(acc, script, stdin=buf.getvalue())
    return {"release": release, "path": f"{base}/current", "host": accounts.setting(acc, "host")}


def ssh_rollback(site: dict, acc: dict) -> str:
    base = f"{_web_root(acc)}/{site['id']}"
    q = shlex.quote
    out = ssh_run(acc, f"set -e; cd {q(base)}; cur=$(readlink current | sed 's#releases/##'); "
                       f"prev=$(ls -1 releases | sort | grep -B1 -x \"$cur\" | head -n1); "
                       f"[ -n \"$prev\" ] && [ \"$prev\" != \"$cur\" ] || {{ echo NOPREV; exit 0; }}; "
                       f"ln -sfn releases/$prev current.tmp && mv -Tf current.tmp current && echo $prev")
    if "NOPREV" in out:
        raise HostingError("there is no earlier release to go back to")
    return out.strip().splitlines()[-1]


CADDY_SETUP = r"""set -e
if ! command -v caddy >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo -n apt-get update -y && sudo -n apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo -n gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo -n tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo -n apt-get update -y && sudo -n apt-get install -y caddy
  elif command -v dnf >/dev/null 2>&1; then
    sudo -n dnf install -y 'dnf-command(copr)' && sudo -n dnf copr enable -y @caddy/caddy && sudo -n dnf install -y caddy
  elif command -v pacman >/dev/null 2>&1; then
    sudo -n pacman -Sy --noconfirm caddy
  elif command -v apk >/dev/null 2>&1; then
    sudo -n apk add caddy
  else
    echo "no supported package manager (apt, dnf, pacman, apk)"; exit 3
  fi
fi
sudo -n mkdir -p /etc/caddy/sites
grep -q 'import /etc/caddy/sites/\*' /etc/caddy/Caddyfile 2>/dev/null || echo 'import /etc/caddy/sites/*.caddy' | sudo -n tee -a /etc/caddy/Caddyfile >/dev/null
sudo -n sed -i 's/^:80 {/# :80 {/' /etc/caddy/Caddyfile || true
sudo -n mkdir -p WEBROOT && sudo -n chown -R "$(id -un)" WEBROOT
sudo -n systemctl enable --now caddy 2>/dev/null || sudo -n rc-service caddy start 2>/dev/null || true
if command -v ufw >/dev/null 2>&1 && sudo -n ufw status | grep -q active; then sudo -n ufw allow 80/tcp; sudo -n ufw allow 443/tcp; fi
caddy version
"""


def server_setup(acc: dict, log: Log) -> str:
    """Install Caddy on the server (needs passwordless sudo) and make it read one file per site."""
    log(f"setting up Caddy on {accounts.setting(acc, 'host')}")
    out = ssh_run(acc, CADDY_SETUP.replace("WEBROOT", shlex.quote(_web_root(acc))), timeout=900)
    return out.strip().splitlines()[-1] if out.strip() else "ok"


def server_site(acc: dict, site: dict, log: Log) -> str:
    """Write the site's Caddy block on the server and reload Caddy (it gets certificates by itself)."""
    from bot.hosting.caddy import block
    remote = dict(site, root=f"{_web_root(acc)}/{site['id']}/current")
    text = block(remote, remote=True)
    q = shlex.quote
    ssh_run(acc, f"echo {q(text)} | sudo -n tee /etc/caddy/sites/{q(site['id'])}.caddy >/dev/null && "
                 f"sudo -n caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null && "
                 f"(sudo -n systemctl reload caddy || sudo -n caddy reload --config /etc/caddy/Caddyfile)")
    log(f"Caddy on {accounts.setting(acc, 'host')} serves {', '.join(site.get('domains') or [])}")
    return text


def server_site_remove(acc: dict, site_id: str) -> None:
    q = shlex.quote
    ssh_run(acc, f"sudo -n rm -f /etc/caddy/sites/{q(site_id)}.caddy && (sudo -n systemctl reload caddy || true)")


# ---- FTP / FTPS ----------------------------------------------------------------------------------------------- #

def _ftp(acc: dict) -> ftplib.FTP:
    tls = accounts.setting(acc, "tls", "yes").lower() not in ("no", "false", "0", "off")
    f = ftplib.FTP_TLS(timeout=30) if tls else ftplib.FTP(timeout=30)
    try:
        f.connect(accounts.setting(acc, "host"), int(accounts.setting(acc, "port", "21")))
        f.login(accounts.setting(acc, "user"), accounts.secret(acc, "password"))
        if tls:
            f.prot_p()
    except (ftplib.all_errors) as e:
        raise HostingError(f"FTP{'S' if tls else ''}: {e}") from e
    return f


def ftp_check(acc: dict) -> str:
    f = _ftp(acc)
    try:
        return f"signed in; {f.getwelcome()[:80]}; working folder {f.pwd()}"
    finally:
        f.quit()


def _ftp_listing(f: ftplib.FTP, path: str) -> dict[str, tuple[int, str]]:
    """{relative path: (size, modify)} under path, recursively (MLSD; empty if the server lacks it)."""
    out: dict[str, tuple[int, str]] = {}

    def walk(d: str, rel: str):
        try:
            entries = list(f.mlsd(d, facts=["type", "size", "modify"]))
        except ftplib.error_perm:
            return
        for name, facts in entries:
            if name in (".", ".."):
                continue
            r = f"{rel}{name}"
            if facts.get("type") == "dir":
                walk(f"{d}/{name}", r + "/")
            elif facts.get("type") == "file":
                out[r] = (int(facts.get("size", -1)), facts.get("modify", ""))
    walk(path, "")
    return out


def ftp_publish(site: dict, folder: Path, acc: dict, target: dict, log: Log) -> dict:
    remote = (target.get("remote_dir") or accounts.setting(acc, "remote_dir", "public_html")).rstrip("/")
    f = _ftp(acc)
    try:
        have = _ftp_listing(f, remote)
        made: set[str] = set()
        sent = skipped = 0
        local = files_of(folder)
        for rel, p in local:
            size = p.stat().st_size
            if rel in have and have[rel][0] == size:
                skipped += 1
                continue
            d = posixpath.dirname(rel)
            parts = [x for x in d.split("/") if x]
            for i in range(len(parts)):
                sub = remote + "/" + "/".join(parts[: i + 1])
                if sub not in made:
                    try:
                        f.mkd(sub)
                    except ftplib.error_perm:
                        pass
                    made.add(sub)
            with p.open("rb") as fh:
                f.storbinary(f"STOR {remote}/{rel}", fh)
            sent += 1
        removed = 0
        if target.get("delete_extra"):
            keep = {rel for rel, _ in local}
            for rel in have:
                if rel not in keep:
                    try:
                        f.delete(f"{remote}/{rel}")
                        removed += 1
                    except ftplib.error_perm:
                        pass
        log(f"FTP: {sent} file(s) uploaded, {skipped} unchanged, {removed} removed")
        return {"uploaded": sent, "unchanged": skipped, "removed": removed, "remote_dir": remote}
    finally:
        try:
            f.quit()
        except ftplib.all_errors:
            pass


# ---- Netlify -------------------------------------------------------------------------------------------------- #

def _http(method: str, url: str, token: str, label: str, **kw) -> httpx.Response:
    try:
        r = httpx.request(method, url, headers={"Authorization": f"Bearer {token}", **kw.pop("headers", {})}, timeout=120.0, **kw)
    except httpx.HTTPError as e:
        raise HostingError(f"{label}: {e}") from e
    if r.status_code >= 400:
        raise HostingError(f"{label}: {method} {url.split('?')[0]} failed ({r.status_code}): {r.text[:300]}")
    return r


def netlify_publish(site: dict, folder: Path, acc: dict, target: dict, log: Log) -> dict:
    token = accounts.secret(acc, "token")
    api = "https://api.netlify.com/api/v1"
    name = target.get("site_name") or f"abp-{site['id']}"
    sid = target.get("site_id")
    if not sid:
        mine = _http("GET", f"{api}/sites?name={name}&filter=all", token, "Netlify").json()
        hit = next((s for s in mine if s.get("name") == name), None)
        if not hit:
            hit = _http("POST", f"{api}/sites", token, "Netlify", json={"name": name}).json()
            log(f"Netlify: created site {name}")
        sid = hit["id"]
        target["site_id"] = sid
    files = {"/" + rel: hashlib.sha1(p.read_bytes()).hexdigest() for rel, p in files_of(folder)}
    d = _http("POST", f"{api}/sites/{sid}/deploys", token, "Netlify", json={"files": files}).json()
    need = set(d.get("required") or [])
    by_sha = {sha: rel for rel, sha in files.items()}
    for sha in need:
        rel = by_sha[sha]
        _http("PUT", f"{api}/deploys/{d['id']}/files{rel}", token, "Netlify", content=(folder / rel.lstrip("/")).read_bytes(),
              headers={"Content-Type": "application/octet-stream"})
    log(f"Netlify: {len(need)} of {len(files)} file(s) uploaded")
    end = time.monotonic() + 300
    while True:
        st = _http("GET", f"{api}/deploys/{d['id']}", token, "Netlify").json()
        if st.get("state") == "ready":
            break
        if st.get("state") == "error" or time.monotonic() > end:
            raise HostingError(f"Netlify deploy {st.get('state')}: {st.get('error_message', '')}")
        time.sleep(2)
    for dom in site.get("domains") or []:
        if target.get("set_domain") and dom:
            _http("PATCH", f"{api}/sites/{sid}", token, "Netlify", json={"custom_domain": dom})
            break
    return {"url": st.get("ssl_url") or st.get("url"), "deploy_id": d["id"], "site_id": sid, "uploaded": len(need)}


# ---- Vercel --------------------------------------------------------------------------------------------------- #

def vercel_publish(site: dict, folder: Path, acc: dict, target: dict, log: Log) -> dict:
    token = accounts.secret(acc, "token")
    team = accounts.setting(acc, "team_id")
    q = f"?teamId={team}" if team else ""
    entries = []
    for rel, p in files_of(folder):
        data = p.read_bytes()
        sha = hashlib.sha1(data).hexdigest()
        _http("POST", f"https://api.vercel.com/v2/files{q}", token, "Vercel", content=data,
              headers={"x-vercel-digest": sha, "Content-Type": "application/octet-stream"})
        entries.append({"file": rel, "sha": sha, "size": len(data)})
    name = target.get("project") or f"abp-{site['id']}"
    body = {"name": name, "files": entries, "target": "production", "projectSettings": {"framework": None}}
    d = _http("POST", f"https://api.vercel.com/v13/deployments{q}", token, "Vercel", json=body).json()
    end = time.monotonic() + 300
    while d.get("readyState") not in ("READY", "ERROR", "CANCELED") and time.monotonic() < end:
        time.sleep(2)
        d = _http("GET", f"https://api.vercel.com/v13/deployments/{d['id']}{q}", token, "Vercel").json()
    if d.get("readyState") != "READY":
        raise HostingError(f"Vercel deployment {d.get('readyState')}: {d.get('errorMessage', '')}")
    log(f"Vercel: {len(entries)} file(s), deployment {d['id']}")
    return {"url": f"https://{d.get('url')}", "deploy_id": d["id"], "project": name}


# ---- Cloudflare Pages (wrangler) ------------------------------------------------------------------------------ #

def cloudflare_publish(site: dict, folder: Path, acc: dict, target: dict, log: Log) -> dict:
    npx = shutil.which("npx")
    if not npx:
        raise HostingError("Cloudflare Pages deploys run Cloudflare's wrangler through npx: install Node.js first")
    project = target.get("project") or f"abp-{site['id']}"
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": accounts.secret(acc, "api_token"),
           "CLOUDFLARE_ACCOUNT_ID": accounts.setting(acc, "account_id")}
    if not env["CLOUDFLARE_ACCOUNT_ID"]:
        raise HostingError("set the Cloudflare account's Account ID for Pages")
    from bot.hosting import dns
    p = dns.provider(acc)
    try:
        p.req("GET", f"/accounts/{env['CLOUDFLARE_ACCOUNT_ID']}/pages/projects/{project}")
    except HostingError:
        p.req("POST", f"/accounts/{env['CLOUDFLARE_ACCOUNT_ID']}/pages/projects",
              json={"name": project, "production_branch": "main"})
        log(f"Cloudflare Pages: created project {project}")
    r = subprocess.run([npx, "--yes", "wrangler@3", "pages", "deploy", str(folder), "--project-name", project,
                        "--branch", "main", "--commit-dirty=true"], capture_output=True, text=True, env=env, timeout=900,
                       shell=os.name == "nt")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise HostingError(f"wrangler pages deploy failed: {out[-800:]}")
    import re
    m = re.findall(r"https://[\w.-]+\.pages\.dev", out)
    return {"url": m[-1] if m else f"https://{project}.pages.dev", "project": project}


# ---- GitHub Pages --------------------------------------------------------------------------------------------- #

def github_publish(site: dict, folder: Path, acc: dict, target: dict, log: Log) -> dict:
    git = shutil.which("git")
    if not git:
        raise HostingError("git is not installed here")
    token = accounts.secret(acc, "token")
    repo = target.get("repo") or accounts.setting(acc, "repo")
    branch = target.get("branch") or "gh-pages"
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
           "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}", "GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "site"
        shutil.copytree(folder, work, ignore=shutil.ignore_patterns(*_SKIP))
        (work / ".nojekyll").write_text("")
        doms = [d for d in site.get("domains") or [] if not d.endswith("github.io")]
        if doms:
            (work / "CNAME").write_text(doms[0] + "\n")

        def g(*args):
            r = subprocess.run([git, *args], cwd=work, env=env, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise HostingError(f"git {args[0]} failed: {(r.stderr or r.stdout)[-500:]}")
            return r.stdout
        g("init", "-q", "-b", branch)
        g("add", "-A")
        g("-c", "user.name=ABP", "-c", "user.email=abp@localhost", "commit", "-q", "-m", f"Publish {site['id']}")
        g("push", "-q", "--force", f"https://github.com/{repo}.git", f"{branch}:{branch}")
    api = f"https://api.github.com/repos/{repo}/pages"
    hdr = {"Accept": "application/vnd.github+json"}
    try:
        httpx.post(api, headers={"Authorization": f"Bearer {token}", **hdr}, json={"source": {"branch": branch, "path": "/"}}, timeout=30)
    except httpx.HTTPError:
        pass
    info = _http("GET", api, token, "GitHub", headers=hdr).json()
    log(f"GitHub Pages: pushed to {repo}@{branch}")
    return {"url": info.get("html_url"), "repo": repo, "branch": branch}


def check(acc: dict) -> str:
    """Verify a deploy-only account."""
    if acc["provider"] == "github":
        token = accounts.secret(acc, "token")
        repo = accounts.setting(acc, "repo")
        r = _http("GET", f"https://api.github.com/repos/{repo}", token, "GitHub").json()
        perms = r.get("permissions") or {}
        return f"{r.get('full_name')}: {'can push' if perms.get('push') else 'read only — the token cannot push'}"
    raise HostingError(f"no check for {acc['provider']}")


PUBLISHERS = {"ssh": ssh_publish, "ftp": ftp_publish, "netlify": netlify_publish, "vercel": vercel_publish,
              "cloudflare": cloudflare_publish, "github": github_publish}


def publish(site: dict, folder: Path, target: dict, log: Log = lambda m: None) -> dict:
    acc = accounts.get(target["account"])
    fn = PUBLISHERS.get(acc["provider"])
    if not fn:
        raise HostingError(f"{acc['provider']} accounts cannot receive deployments")
    if acc["provider"] == "ssh":
        res = ssh_publish(site, folder, acc, log)
        if target.get("caddy", True) and site.get("domains"):
            server_site(acc, site, log)
            res["url"] = f"https://{site['domains'][0]}"
        return res
    return fn(site, folder, acc, target, log)
