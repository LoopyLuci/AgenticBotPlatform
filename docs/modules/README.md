# ABP modules

A module is a separate program, in its own repo, that ABP clones, updates, builds, runs and drives:
VM-Harness, Hermes-Manager, TransferDaemon, ModelMistress, Continuum, TridentDroid, BrainBuilder and Wrightspace
today. Each one:
- builds, runs, is tested and is released on its own;
- talks to ABP only over a process boundary;
- can be worked on in its own checkout while ABP uses it.

The plan for each module is in [ROADMAP.md](ROADMAP.md).

## Where things are

| | |
|---|---|
| `bot/modules/manifest.py` | the manifest format (`abp-module.toml`, v1) and its checks |
| `bot/modules/registry.py` | the built-in modules, where each checkout is, placeholders, the build cache |
| `bot/modules/harness.py` | install, update (fast-forward only), build, pipeline, hub start/stop, window, TUI, MCP, status |
| `bot/modules/client.py` | the hub client: control file, health, operations, calls |
| `bot/modules/adapters.py` | VM-Harness, Hermes-Manager and TransferDaemon through their own code |
| `bot/modules/tools.py` | the agent's tools: `module_list`, `module_status`, `module_operations`, `module_read`, `module_call`, `module_setup` |
| `bot/modules/conformance.py` | checks a module against the contract |
| `bot/dashboard/modules_api.py` | `/api/modules/...` |
| `modules-panel.js` (in `bot/dashboard/static/` and `desktop-app/ui/`, identical) | the Modules page |
| `tests/test_modules.py`, `tests/fixtures/fake_module/` | tests against a real, tiny hub |

## Adding a module

**The short way: `abp modules adopt <folder>`** (or Modules -> Add a project, or `python -m abp_modkit adopt`).
abp_modkit reads the project, writes its `abp-module.toml` and `abp-ops.toml`, gives it a hub and an MCP bridge, and
registers it; nothing is written by hand. See [modkit.md](modkit.md). The steps below are for a module that brings
its own hub.

1. **Put an `abp-module.toml` at the repo root.** `tests/fixtures/fake_module/abp-module.toml` is the smallest
   working example. All the fields are described at the top of `bot/modules/manifest.py`.
2. **Give it a control hub.** A local HTTP service, on `127.0.0.1` with a random port and a random token.
   - It writes its control file: JSON `{url, token, pid, version}`.
   - It serves:
     - `GET {api_base}/health`, open to any caller;
     - `GET {api_base}/operations`, which returns a list, or `{"operations": [...]}`, each with an `id`, a
       `summary`, `mutating` and an `input_schema`;
     - `POST {api_base}/call/{op}`, which returns `{"result": ...}`;
     - `POST {api_base}/service/stop`.
   - Every route except health needs `Authorization: Bearer <token>`.
   - Errors come back as `{"error": {"code", "message"}}`.
   - `tests/fixtures/fake_module/hub.py` does all of this in 100 lines.
3. **Serve the same operations over MCP.** Declare the command in `[mcp] stdio`.
4. **Add it to `registry.BUILTIN`** if it is one of ours. Otherwise, add it in config/backends.yaml under
   `modules.extra`.
5. **Check it.** On the Modules page, use **Check conformance** (`POST /api/modules/<id>/conformance`), or call
   `conformance.check("<id>")` in a test.

## Config (config/backends.yaml)

```yaml
modules:
  build_cache: E:/abp-build          # cargo target dirs on fast storage, one folder per module
  brainbuilder: {path: D:/src/BrainBuilder}
  wrightspace: {enabled: false}      # hide a module
  extra:
    - {id: my-tool, name: My Tool, repo: https://github.com/me/my-tool.git, marker: [Cargo.toml]}
peers:
  remote_control: [modules]          # let linked servers use this machine's modules
```

## How it behaves

- **Where the checkout is:** `$ABP_MODULE_<ID>_DIR` wins. Then `modules.<id>.path`. Then a folder with the
  module's name next to ABP's (a developer's working copy). Last, `data/modules/<Name>`, where ABP clones it.
- **Updates** fetch, then fast-forward only, then rebuild.
  - They refuse while tracked files have uncommitted changes. Untracked files don't block them: git itself refuses
    if one would be overwritten.
  - A hub running from the checkout is stopped for the update and started again afterwards.
- **Builds, updates and pipelines** run as background jobs, one at a time per module, each with a streamed log.
  Every command has a timeout, and on timeout its whole process tree is killed.
- **Hub tokens** stay on the machine. ABP reads them from the control file and sends them only to the hub.
- **Every route and every tool works on a linked server** (the `machine` argument, or the machine picker on the
  page), if that server's owner allows `modules` in `peers.remote_control`.
