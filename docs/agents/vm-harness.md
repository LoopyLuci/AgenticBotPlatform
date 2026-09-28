# VM-Harness from ABP

[VM-Harness](https://github.com/LoopyLuci/VM-Harness) controls virtual machines (QEMU, VirtualBox, VMware, Hyper-V,
WSL, KVM) and containers (Docker, Podman, Kubernetes, Compose). It has its own desktop window. It stays a **separate
program**, with its own git checkout, its own virtualenv and its own releases, worked on in its own repo. ABP installs
it, keeps it up to date from that repo, runs it, and drives it through its API: the page **VM-Harness**, the agent's
`vmh_*` tools, and `/api/vm-harness/*`.

## How the two connect

VM-Harness runs one local service, **the hub** (`python -m vm_harness serve`, 127.0.0.1:8765). Its window, its MCP
server, its command line and ABP are all clients of the hub. When the hub starts it writes `~/.vmharness/control.json`
with its address and a generated token. ABP reads that file, so there is nothing to configure. A VM started from any
client is the same VM every other client sees, and every change is written to VM-Harness's audit log with the client
that asked (ABP's calls show as `abp`).

VM-Harness's own documentation of the hub, its operations and its MCP server is `docs/control.md` in its repo.

## Where ABP finds it

The first of these that is set or exists:

1. `$ABP_VM_HARNESS_DIR`;
2. `vm_harness.path` in `config/backends.yaml`;
3. a `VM-Harness` folder next to ABP's own. This is a developer's working copy, and ABP uses it as it is;
4. `data/modules/VM-Harness`, where **Install** clones it.

**Install** clones the repo if needed, creates its virtualenv and installs its dependencies. **Update** fetches from
the repo, pulls, reinstalls, and restarts the hub if it was running. Update refuses while the checkout has uncommitted
changes, so work in progress is never overwritten. Both run in the background, with progress on the page.

A hub on another machine is used by setting `vm_harness.url` and `vm_harness.token` in `config/backends.yaml`, for
example through an SSH tunnel to that machine's 127.0.0.1:8765.

## The page

| Tab | What it does |
|---|---|
| Overview | Where it is installed, the commit, updates waiting, uncommitted changes. Install, Update, start and stop the hub, open the window, add its MCP server to ABP. Which hypervisors and container engines work on this machine, and why the others do not |
| Machines | Every VM on every hypervisor with its state and resources: start, stop, pause, resume, reboot, snapshots, and a picture of its screen |
| Containers | Docker containers: start, stop, restart, logs |
| Window | The VM-Harness window, live: pick a panel, see a screenshot (refreshed every 1.5 s when *live* is on), and every widget on the panel, which you can click, fill in or read from here |
| All features | Every operation (160+), searchable, with a form built from its own argument schema; destructive ones ask first |
| Audit | VM-Harness's log of every change, with who asked |

## The agent's tools

Reading is free. Anything that changes a VM, a container or the window asks for approval first.

| Tool | What it does |
|---|---|
| `vmh_status` | Installed, commit, updates waiting, hub and window, working hypervisors and container engines |
| `vmh_vms` | Every VM with its state |
| `vmh_operations` | Search the operations, or one operation's arguments |
| `vmh_read` | Run an operation that changes nothing: status, metrics, screenshots, logs, lists (refuses the others) |
| `vmh_call` | Run any operation |
| `vmh_gui_look` | The window without changing it: panels, widgets, a widget's contents, screenshot, open dialogs |
| `vmh_gui_act` | Drive the window: open it, switch panels, click, fill in, select, type, keys, answer dialogs, call a panel's method |
| `vmh_setup` | Install, update, start or stop the hub, register its MCP server |

## Its MCP server

VM-Harness has its own MCP server (`python -m vm_harness mcp`), which offers every operation as a tool, including
driving the window. **Add its MCP server to ABP** registers it among ABP's external MCP servers in compact form (three
tools: search, describe, call), so other MCP-aware parts of ABP can use it without flooding an agent's context.
Claude Desktop or any other MCP client can run it directly. VM-Harness's `docs/control.md` has the configuration.

## Checked on this machine (2026-09-28)

ABP found the working copy at `Z:\Projects\VM-Harness`, started its hub, and listed:

- 158 operations;
- the working hypervisors (Hyper-V, QEMU, VirtualBox, WSL) and Docker;
- every VM, including Kali on Hyper-V, Proxmox on VirtualBox and the WSL distributions.

It then stopped the hub again.

The window was driven through the hub: panels listed, the Create VM wizard filled in and advanced a step.
