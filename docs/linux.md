# ABP on Linux

Three ways in, depending on the machine. All of them keep ABP's state (the auto-generated dashboard token in
`.env`, config, the database, logs) in `ABP_HOME`, never next to the code.

## Any distribution: `scripts/linux/abp-linux.sh`

```bash
curl -fsSLO https://raw.githubusercontent.com/LoopyLuci/AgenticBotPlatform/main/scripts/linux/abp-linux.sh
bash abp-linux.sh install              # for you: ~/.local/share/agentic-bot-platform, a systemd --user service
sudo bash abp-linux.sh install --system   # a system service: /opt/agentic-bot-platform, /var/lib/abp, user abp
abp-update                              # later: fast-forward, reinstall, restart; rolls back if it does not come up
bash abp-linux.sh status | uninstall [--keep-data]
```

It installs git, curl and Python with the native package manager: apt (Debian, Ubuntu, Mint, Pop!_OS), dnf (Fedora,
RHEL, Rocky, Alma), zypper (openSUSE), pacman (Arch, Manjaro), apk (Alpine) or xbps (Void). Where the distribution's
Python is older than 3.11, it gets one with uv. It then:

- installs the pinned dependencies (`requirements.lock`, hash-checked) into a private venv;
- adds `abp` (CLI), `abp-tui`, `abp-server` and `abp-update` to your PATH;
- adds a desktop entry that opens the dashboard (it signs itself in on this machine);
- installs a systemd service that starts ABP now and at boot.

**Updates never leave you with a broken ABP.** `abp-update` fast-forwards only (a diverged copy is left alone),
reinstalls the dependencies and restarts ABP. It then waits for ABP's health check, and if the new version doesn't
answer, it puts the previous commit back and restarts that.

## NixOS: the flake

```nix
# flake.nix of your system
inputs.agentic-bot-platform.url = "github:LoopyLuci/AgenticBotPlatform";
# in the system's modules:
imports = [ agentic-bot-platform.nixosModules.default ];
services.agentic-bot-platform = {
  enable = true;
  # host = "0.0.0.0"; openFirewall = true;         # to reach it from other machines
  # environmentFile = "/run/secrets/abp.env";      # tokens and keys, kept out of the store
  # extraPackages = [ pkgs.docker pkgs.qemu ];     # programs ABP may run
};
```

- `nix run github:LoopyLuci/AgenticBotPlatform` runs the server once; `#cli` and `#tui` run the CLI and TUI.
- `nix develop` gives a shell with everything needed to build the desktop app.
- `nix flake check` runs the module in a VM test.

The package takes every dependency from nixpkgs (no pip, no FHS assumptions). The service is hardened (its own user,
`ProtectSystem=strict`, state only in `/var/lib/abp`).

## From a checkout (development)

`./scripts/install.sh` (add `--yes --cli` for no prompts, `--no-system-deps` to skip the desktop build tools) sets
up `.venv` and the configuration; `./scripts/run.sh` runs the server, `./scripts/tui.sh` the TUI.

## Tested

Tested 2026-09-29 on VMs on the Server machine (QEMU/WHPX): Ubuntu 26.04 LTS (Python 3.14), Debian 13 (Python 3.13)
and NixOS 26.05. The results are in docs/modules/ROADMAP.md §9 (LX-A, LX-B). What those runs found and fixed:

- `./scripts/install.sh` wasn't executable in git.
- ABP crashed at start on CPUs without x86-64-v2, because NumPy's wheels need it. Now only the support bot's neural
  model turns off.
- `--check` failed on desktop tools that `--no-system-deps` had skipped.

**Note for VMs:** QEMU's default CPU model lacks x86-64-v2. Give the VM `-cpu qemu64,+ssse3,+sse4.1,+sse4.2,+popcnt,+cx16`
(or a real CPU model) so NumPy-based features work.
