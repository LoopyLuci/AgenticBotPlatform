"""ABP modules: separate programs, each in its own repo, that ABP clones, updates, builds, runs and drives.

A module describes itself with an ``abp-module.toml`` at its repo root (see docs/modules/README.md). Until a repo
ships one, ABP uses a built-in manifest for it (registry.BUILTIN). Modules that ABP already drove before this
framework (VM-Harness, Hermes-Manager, TransferDaemon) keep their own code through adapters (adapters.py).
"""
