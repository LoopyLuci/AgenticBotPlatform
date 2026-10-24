"""Confined Grok in a throw-away WSL2 VM (bot/agent_runtime/grok_vm.py). The unit tests need no VM. The live checks start
real VMs but never Grok itself (no tokens): set RUN_GROK_VM=1 on a machine with WSL and the built base to run them."""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from bot.agent_runtime import grok_vm, wsl_vm
from bot.agent_runtime.errors import ToolError

LIVE = os.environ.get("RUN_GROK_VM") == "1"
_REAL_RUN_TASK = grok_vm.run_task                     # taken at import, before conftest guards the module attribute


def test_the_egress_policy_blocks_the_host_lan_and_tailscale_but_not_the_internet():
    rules = grok_vm.egress_rules()
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8",
                "fc00::/7", "fe80::/10"):
        assert net in rules
    assert "policy accept" in rules and 'oifname "lo" accept' in rules and rules.count(" drop") == 2


def test_the_task_script_locks_the_network_and_drops_host_mounts_before_grok_runs_as_grok():
    s = grok_vm.task_script(model="grok-4.7-build-fast", timeout_s=300)
    nft, check, unmount, grok = (s.index("nft -f -"), s.index(f"nft list table inet {grok_vm.EGRESS_TABLE}"),
                                 s.index("umount -l"), s.index("runuser -u grok"))
    assert nft < check < unmount < grok                                    # policy, verified, mounts gone, then Grok
    assert "exit 97" in s and "set -eu" in s                                # fails closed
    assert "--permission-mode auto" in s and "--always-approve" not in s and "bypassPermissions" not in s
    assert "--model grok-4.7-build-fast" in s and "timeout -k 10 300" in s and "nameserver 1.1.1.1" in s


def test_the_worktree_goes_in_without_its_git_pointer_or_links(tmp_path):
    wt = tmp_path / "wt"
    (wt / "pkg").mkdir(parents=True)
    (wt / "pkg" / "a.py").write_text("x = 1\n")
    (wt / ".git").write_text("gitdir: /somewhere/else\n")
    names = tarfile.open(fileobj=io.BytesIO(grok_vm.pack_worktree(wt))).getnames()
    assert "pkg/a.py" in names and not any(n == ".git" or n.startswith(".git/") for n in names)


def test_only_a_real_sign_in_document_comes_back():
    assert grok_vm._valid_auth(json.dumps({"access_token": "t"}).encode())
    assert not grok_vm._valid_auth(b"") and not grok_vm._valid_auth(b"[]") and not grok_vm._valid_auth(b"{}")


def test_no_base_or_no_sign_in_means_no_run(tmp_path, monkeypatch):
    run_task = _REAL_RUN_TASK
    monkeypatch.setattr(grok_vm, "base_path", lambda: tmp_path / "missing.tar")
    with pytest.raises(ToolError, match="build_grok_vm_base"):
        run_task(tmp_path, "p", auth_file=tmp_path / "auth.json")
    (tmp_path / "base.tar").write_bytes(b"x")
    monkeypatch.setattr(grok_vm, "base_path", lambda: tmp_path / "base.tar")
    with pytest.raises(ToolError, match="not signed in"):
        run_task(tmp_path, "p", auth_file=tmp_path / "auth.json")


# ---- live: real VMs, no Grok ------------------------------------------------------------------------------------------
@pytest.mark.skipif(not (LIVE and grok_vm.available()), reason="RUN_GROK_VM=1 with WSL and the built Grok base")
def test_live_the_vm_has_internet_only_and_no_host_access():
    import uuid

    u = uuid.uuid4().hex[:8]
    name, tmp = f"{grok_vm.NAME_PREFIX}t{u}", wsl_vm.vm_root() / f"grok-t{u}"
    try:
        wsl_vm.provision(name, tmp, grok_vm.base_path())
        setup = grok_vm.task_script(model="", timeout_s=60).split("cd /work")[0]
        assert wsl_vm._guest(name, setup).returncode == 0

        def as_grok(sh: str) -> int:
            return wsl_vm._wsl("-d", name, "-u", "grok", "--exec", "sh", "-c", sh, timeout=90).returncode

        assert as_grok('! grep -E " /mnt/| /usr/lib/wsl| 9p | drvfs | virtiofs " /proc/mounts') == 0
        assert as_grok("! cmd.exe /c ver") == 0
        assert as_grok("timeout 25 git ls-remote https://github.com/git/git HEAD >/dev/null") == 0
        gw = wsl_vm._decode(wsl_vm._guest(name, "awk '$2==\"00000000\"{print $3}' /proc/net/route | head -1").stdout).strip()
        gw_ip = ".".join(str(int(gw[i:i + 2], 16)) for i in (6, 4, 2, 0))
        for target in (f"{gw_ip}:8787", "192.168.1.1", "100.100.100.100"):
            assert as_grok(f"timeout 8 git ls-remote http://{target}/x") != 0, target
        assert as_grok("nft flush ruleset") != 0
        assert wsl_vm._guest(name, f"nft list table inet {grok_vm.EGRESS_TABLE}").returncode == 0
    finally:
        wsl_vm.teardown(name, tmp)
    assert name not in wsl_vm.list_distros() and not Path(tmp).exists()
