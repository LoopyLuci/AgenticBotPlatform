# Measuring spare VRAM

`bot/gpu.py` reports how much video memory is free right now, on any vendor. On Windows it asks DXGI
(`IDXGIAdapter3::QueryVideoMemoryInfo`, local segment) through ctypes, which gives the OS budget and the usage of every
process; this works on AMD, where there is no `nvidia-smi` and WMI counters are empty. Fallbacks, in order: `nvidia-smi`,
Linux amdgpu sysfs, the registry total (free unknown). It never raises: when nothing can be measured `known` is false.

    from bot import gpu
    gpu.gpu_status()              # {"known", "source", "name", "total_gb", "used_gb", "free_gb", "budget_gb", "adapters": [...]}
    gpu.free_vram_gb()            # float, or None if unknown
    gpu.can_fit(18, headroom_gb=1)  # True / False / None

Everywhere else: the `gpu_status` agent tool (`need_gb` optional), `GET /api/gpu?need_gb=18`, and the CLI
`abp gpu` and `abp gpu can-fit 18 [--headroom 1]` (exit 0 fits, 1 does not, 2 unknown).

## Before a GPU run

A worker, the conductor or a measured run checks first, then takes the AgentSentinel lock, then loads:

    abp gpu can-fit 18 || { echo "not enough VRAM"; exit 1; }
    agent-sentinel board lock custom:gpu0 --agent NAME --task ID
    ... load the model, run, release the lock ...

`can-fit` is a point-in-time reading: re-check after taking the lock if another agent could have loaded something in between.
The `used` figure includes other processes (a running hub's loaded models count), so free is what a new load really gets.

## Local AI budget

`localai.gpu_memory_gb` = 0 (auto) now uses 92% of the measured total VRAM as the engine budget; a number set by hand still wins.
