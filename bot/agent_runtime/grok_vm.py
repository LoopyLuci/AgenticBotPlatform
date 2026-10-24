"""Confined Grok in a throw-away WSL2 VM (conductor.grok.confine_backend = wsl_vm; PLAN-grok-vm.md).

Why a VM: inside a Windows AppContainer Grok cannot resolve a file's path (the Mount Manager lookup is denied), so its
"confirmation floor" cancels every edit to an existing file in a headless run. Linux has no such limit.

Per task (run_task):

1. `wsl --import` a uniquely named distro from the clean Grok base (build_base): Debian slim, an unprivileged user `grok`,
   the official Linux Grok binary, git and nftables. Never exported from an installed distro: those hold home folders,
   keys and credentials, and this VM has internet. wsl.conf as in wsl_vm.py: no /mnt drives, no Windows interop.
2. The worktree goes in as a tar stream (without its .git pointer file); the guest makes its own one-commit repository so
   Grok's git use works. That .git never comes back.
3. Only ~/.grok/auth.json goes in. The prompt goes in as a file outside /work.
4. Network is internet only: as root, before Grok starts, an nftables output policy drops private, local, link-local,
   CGNAT/Tailscale and multicast ranges (the Windows host and the LAN included) and accepts the rest. DNS goes to public
   resolvers. Grok runs as `grok`, which cannot change the rules. Fails closed if the rules are not in place.
5. Grok runs headless in auto mode (never --always-approve), with a timeout and a memory cap (cgroup v2, best effort).
6. /work comes back through wsl_vm.unpack_to (plain files inside the worktree only; nothing on the host is deleted); a
   refreshed auth.json comes back only if it is a valid sign-in document.
7. Teardown always: terminate, unregister, delete the temp dir.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import shlex
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import Callable

from bot.agent_runtime import wsl_vm
from bot.agent_runtime.errors import ToolError

NAME_PREFIX = "abp-grokvm-"
GUEST_HOME = "/home/grok"
GUEST_GROK = f"{GUEST_HOME}/.grok/bin/grok"
GUEST_PROMPT = f"{GUEST_HOME}/prompt.txt"
MEMORY_MAX = "6G"
RESOLVERS = ("1.1.1.1", "9.9.9.9")

# Everything that is not the public internet: this host (the WSL gateway is in 172.16/12), the LAN, Tailscale (100.64/10),
# link-local, loopback (lo itself stays usable), multicast and reserved space.
BLOCKED_V4 = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
              "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4")
BLOCKED_V6 = ("::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "::ffff:0:0/96")
EGRESS_TABLE = "abp_egress"
_HOST_MOUNT = r'$2 ~ /^\/mnt\/|^\/usr\/lib\/wsl/ || $3 ~ /^(9p|drvfs|virtiofs)$/'
UNMOUNT_HOST = (f"for m in $(awk '{_HOST_MOUNT} {{print $2}}' /proc/mounts | sort -r); do umount -l \"$m\" 2>/dev/null || true; done\n"
                f"if awk '{_HOST_MOUNT} {{f=1}} END {{exit !f}}' /proc/mounts; then echo 'host mounts still present' >&2; exit 97; fi")


def egress_rules() -> str:
    """The nftables ruleset the guest loads before Grok starts (`nft -f -`)."""
    return (f"table inet {EGRESS_TABLE} {{\n"
            "  chain out {\n"
            "    type filter hook output priority 0; policy accept;\n"
            '    oifname "lo" accept\n'
            f"    ip daddr {{ {', '.join(BLOCKED_V4)} }} drop\n"
            f"    ip6 daddr {{ {', '.join(BLOCKED_V6)} }} drop\n"
            "  }\n"
            "}\n")


def _data() -> Path:
    return wsl_vm.vm_root().parent


def base_path() -> Path:
    return _data() / "wsl_base" / "grok-base.tar"


def available() -> bool:
    """WSL is here and the Grok base image has been built."""
    try:
        return wsl_vm.available() and base_path().is_file()
    except Exception:  # noqa: BLE001 - an unavailable VM tier is simply "not available"
        return False


# ---- the base image ---------------------------------------------------------------------------------------------------
BUILD_SCRIPT = """set -eu
export DEBIAN_FRONTEND=noninteractive
rm -f /etc/resolv.conf
echo "nameserver 1.1.1.1" > /etc/resolv.conf
echo "nameserver 9.9.9.9" >> /etc/resolv.conf
apt-get update -q
apt-get install -y -qq --no-install-recommends git nftables ca-certificates brotli python3 python3-pytest >/dev/null
id grok >/dev/null 2>&1 || useradd -m -s /bin/sh grok
mkdir -p /home/grok/.grok/bin
brotli -d -f -o /home/grok/.grok/bin/grok /tmp/grok.br
rm -f /tmp/grok.br
chmod 755 /home/grok/.grok/bin/grok
chown -R grok:grok /home/grok
git config --system user.name "ABP Grok worker"
git config --system user.email "grok-worker@abp.invalid"
git config --system init.defaultBranch main
apt-get purge -y -qq brotli >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache
runuser -u grok -- /home/grok/.grok/bin/grok --version
"""


def _grok_br(grok_tgz: Path) -> bytes:
    """bin/grok.br out of the official @xai-official/grok-linux-x64 npm tarball."""
    with tarfile.open(grok_tgz, "r:gz") as tf:
        member = tf.getmember("package/bin/grok.br")
        f = tf.extractfile(member)
        if f is None:
            raise ToolError(f"{grok_tgz} has no package/bin/grok.br")
        return f.read()


def build_base(rootfs: Path, grok_tgz: Path, *, log: Callable[[str], None] = print) -> Path:
    """Builds data/wsl_base/grok-base.tar from a Debian rootfs (.tar or .tar.gz) and the Grok npm tarball. Building needs
    the network (apt); the result never runs with a host mount. Replaces an older base only once the new one is complete."""
    rootfs, grok_tgz = Path(rootfs), Path(grok_tgz)
    name = f"abp-grokbase-{uuid.uuid4().hex[:8]}"
    work = _data() / "wsl_base" / f".build-{name}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        src = rootfs
        if rootfs.suffix == ".gz":
            src = work / "rootfs.tar"
            with gzip.open(rootfs, "rb") as fin, open(src, "wb") as fout:
                shutil.copyfileobj(fin, fout)
        log(f"importing {rootfs.name} as {name}")
        r = wsl_vm._wsl("--import", name, str(work / "vm"), str(src), "--version", "2", timeout=900)
        if r.returncode != 0:
            raise ToolError(f"could not import the rootfs: {wsl_vm._decode(r.stdout + r.stderr).strip()}")
        r = wsl_vm._guest(name, "cat > /tmp/grok.br", input=_grok_br(grok_tgz), timeout=300)
        if r.returncode != 0:
            raise ToolError(f"could not copy the Grok binary in: {wsl_vm._decode(r.stderr).strip()}")
        log("installing git, nftables and Grok in the base")
        r = wsl_vm._guest(name, BUILD_SCRIPT, timeout=1800)
        if r.returncode != 0:
            raise ToolError(f"building the base failed: {wsl_vm._decode(r.stdout + r.stderr).strip()[-800:]}")
        log(wsl_vm._decode(r.stdout).strip().splitlines()[-1] if r.stdout.strip() else "grok installed")
        wsl_vm._wsl("--terminate", name, timeout=120)
        tmp_tar = work / "grok-base.tar"
        r = wsl_vm._wsl("--export", name, str(tmp_tar), timeout=1800)
        if r.returncode != 0:
            raise ToolError(f"could not export the base: {wsl_vm._decode(r.stdout + r.stderr).strip()}")
        os.replace(tmp_tar, base_path())
        log(f"base ready: {base_path()} ({base_path().stat().st_size // (1024 * 1024)} MB)")
        return base_path()
    finally:
        wsl_vm.teardown(name, work / "vm")
        shutil.rmtree(work, ignore_errors=True)


# ---- one task ---------------------------------------------------------------------------------------------------------
def pack_worktree(worktree: Path) -> bytes:
    """The worktree as a tar stream, without .git (a git worktree's .git is a pointer file to a folder the VM never sees)
    and without links or devices."""
    root = Path(worktree)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if rel.parts[0] == ".git" or p.is_symlink() or not (p.is_file() or p.is_dir()):
                continue
            tf.add(p, arcname=rel.as_posix(), recursive=False)
    return buf.getvalue()


def task_script(*, model: str, timeout_s: float) -> str:
    """Root's script in the guest after /work, the prompt and auth.json are in: network policy, then Grok as `grok`."""
    grok = (f"{shlex.quote(GUEST_GROK)} --prompt-file {shlex.quote(GUEST_PROMPT)} --output-format json "
            "--permission-mode auto --disable-web-search --no-auto-update --cwd /work"
            + (f" --model {shlex.quote(model)}" if model else ""))
    resolv = "\\n".join(f"nameserver {ip}" for ip in RESOLVERS)
    return (
        "set -eu\n"
        f"printf '{resolv}\\n' > /etc/resolv.conf\n"
        f"nft -f - <<'EOF'\n{egress_rules()}EOF\n"
        f"nft list table inet {EGRESS_TABLE} >/dev/null\n"           # fail closed: no policy, no Grok
        # WSL's own shared mounts (/mnt/wsl is one tmpfs every running distro sees; /mnt/wslg; the host's GPU drivers) leave
        # this VM before Grok starts; any host-backed mount still present stops the task.
        f"{UNMOUNT_HOST}\n"
        "if [ -d /sys/fs/cgroup ] && mkdir -p /sys/fs/cgroup/abpgrok 2>/dev/null; then\n"
        f"  echo {MEMORY_MAX} > /sys/fs/cgroup/abpgrok/memory.max 2>/dev/null || true\n"
        "  echo $$ > /sys/fs/cgroup/abpgrok/cgroup.procs 2>/dev/null || true\n"
        "fi\n"
        "cd /work\n"
        f"exec timeout -k 10 {int(timeout_s)} runuser -u grok -- env HOME={GUEST_HOME} {grok}\n")


SETUP_SCRIPT = (
    "set -eu\n"
    "chown -R grok:grok /work\n"
    "runuser -u grok -- sh -c 'cd /work && git init -q && git add -A && git commit -q -m base --allow-empty'\n"
    f"chown grok:grok {GUEST_PROMPT} {GUEST_HOME}/.grok/auth.json\n"
    f"chmod 600 {GUEST_HOME}/.grok/auth.json\n")


def _valid_auth(data: bytes) -> bool:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(doc, dict) and bool(doc)


def run_task(worktree: Path, prompt: str, *, auth_file: Path, model: str = "", timeout_s: float = 900,
             log: Callable[[str], None] = lambda _s: None) -> tuple[int, str]:
    """One confined Grok turn on `worktree`. Returns (exit code, Grok's stdout+stderr). Raises ToolError when the VM cannot
    be made; the host is never used as a fallback."""
    if not base_path().is_file():
        raise ToolError(f"no Grok VM base at {base_path()} (scripts/build_grok_vm_base.py builds it)")
    if not Path(auth_file).is_file():
        raise ToolError(f"Grok is not signed in ({auth_file} is missing)")
    worktree = Path(worktree)
    u = uuid.uuid4().hex[:12]
    name, tmp = f"{NAME_PREFIX}{u}", wsl_vm.vm_root() / f"grok-{u}"
    auth_before = Path(auth_file).read_bytes()
    try:
        wsl_vm.provision(name, tmp, base_path())
        steps = ((f"tar -x -C {wsl_vm.GUEST_WORK}", pack_worktree(worktree), "copy the worktree in"),
                 (f"cat > {GUEST_PROMPT}", prompt.encode("utf-8"), "copy the prompt in"),
                 (f"cat > {GUEST_HOME}/.grok/auth.json", auth_before, "copy the sign-in in"),
                 (SETUP_SCRIPT, None, "prepare /work"))
        for script, data, what in steps:
            r = wsl_vm._guest(name, script, input=data, timeout=300)
            if r.returncode != 0:
                raise ToolError(f"could not {what}: {wsl_vm._decode(r.stdout + r.stderr).strip()[-400:]}")
        log(f"grok vm {name}: running")
        r = wsl_vm._guest(name, task_script(model=model, timeout_s=timeout_s), timeout=timeout_s + 60)
        code, out = r.returncode, wsl_vm._decode(r.stdout) + wsl_vm._decode(r.stderr)
        back = wsl_vm._guest(name, f"tar -c -C {wsl_vm.GUEST_WORK} --exclude=./.git .", timeout=600)
        if back.returncode == 0:
            wsl_vm.unpack_to(back.stdout, worktree)
        auth = wsl_vm._guest(name, f"cat {GUEST_HOME}/.grok/auth.json", timeout=60)
        if auth.returncode == 0 and auth.stdout != auth_before and _valid_auth(auth.stdout):
            tmp_auth = Path(str(auth_file) + ".abp-new")
            tmp_auth.write_bytes(auth.stdout)
            os.replace(tmp_auth, auth_file)          # Grok refreshed its token in the VM: keep the new one
        return code, out
    finally:
        wsl_vm.teardown(name, tmp)
