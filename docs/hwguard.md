# Hardware guard

`bot/hwguard` watches this machine's health and keeps heavy work inside safe limits. It is on the **Hardware** screen in
the dashboard and the desktop app (the same panel in both), with the API under `/api/hwguard`.

## What it reads

| Reading | Source | Notes |
|---|---|---|
| CPU load | psutil | average and busiest core |
| CPU temperature | LibreHardwareMonitor / OpenHardwareMonitor WMI sensors when one is running, else ACPI thermal zones | Many desktop boards expose neither; the screen then says "not readable" and lists why |
| GPU VRAM in use | the OS `GPU Adapter Memory\Dedicated Usage` counters (every process on the card) | Not Vulkan's or DXGI's per-process budget, which is wrong for "how full is the card". Card size from DXGI |
| GPU load | the OS `GPU Engine\Utilization Percentage` counters, busiest engine type | |
| GPU temperature | LibreHardwareMonitor only | The AMD driver publishes none through Windows |
| RAM, commit, pagefile | psutil and `GetPerformanceInfo` | commit = how close the machine is to running out of RAM + pagefile |
| Disk free | psutil | every fixed drive, or the `disks` list |
| Hardware errors | the System log, provider `Microsoft-Windows-WHEA-Logger` (18 fatal machine check, 19 corrected, 17/20 PCIe, 46/47 memory) | parsed from `wevtutil` XML; source, error type, APIC id, bank |

## What it decides

Every heavy job asks the governor first; the monitor in ABP writes the verdict to a shared folder (`ABP_HWGUARD_DIR`, else
the system temp folder `abp-hwguard`) so tools outside ABP read the same answer.

- **pause**: an unacknowledged new WHEA event, or a reading past a pause limit (CPU 90 C, GPU 95 C, free RAM 2 GB, commit
  95 %, a watched disk under 5 GB). New heavy work waits; a guarded render is suspended until the pause ends. A new WHEA
  event needs the owner's **Acknowledge** on the Hardware screen.
- **caution**: past a caution limit (CPU 80 C, GPU 85 C, CPU load 90 %, VRAM 92 %, free RAM 6 GB, commit 85 %) or a fatal
  WHEA event in the last 7 days. Worker caps are halved.
- **ok**: the configured caps (pytest 4, Factory 2, render 1, model load 1, build 2; pytest also at most half the threads).

**One heavy job at a time:** a render, a full test run, a model load and a build share one machine-wide slot (a file,
created exclusively), so they never stack.

| Heavy work | Where it asks |
|---|---|
| Local pipeline tests and build | `scripts/local_pipeline.py` `_hw_gate`: waits up to `ABP_PIPELINE_HW_WAIT_S` (1800 s) for the slot, caps `pytest -n`, holds the slot until the pipeline exits |
| Conductor suite workers | `bot/conductor/shards.py` `Budget.measure` |
| Conductor and Local Chat Factory workers | `bot/conductor/run.py` `_hw_workers` (waits out a pause, then caps); `bot/localchat/factory.py` refuses to start while paused |
| ModelMistress loads | `bot/modelmistress/harness.load`, `bot/localchat/catalog.load` (slot, waits 120 s) |
| ABP video jobs | `bot/video/jobs.default_gate` (slot; the job waits and retries) |
| Video Studio render harness (outside the repo) | `render.py` runs `python -m bot.hwguard acquire` before sd-cli and `watch` while it runs |

Every setting has a switch or field on the Hardware screen (saved to `hwguard:` in `config/backends.yaml`, audited).
Alerts go through the Sentinel journal (`hwguard.pause` pushes; caution is journal only); each new WHEA event is recorded
in the journal.

## What it does not cover

Software can lower the load, keep heavy jobs apart, pause them and react to the first corrected error. It **cannot**
stop a faulty or unstable CPU from raising a machine check; a fatal one still crashes the machine, and the cause (BIOS
settings, memory profile, the CPU itself) is checked by the owner, not by software. Without LibreHardwareMonitor running
there is no CPU or GPU temperature on a board without ACPI thermal zones, so heat is only inferred from load. Work that
does not ask the guard (a program started by hand) is not limited. A suspended render keeps its VRAM.
