# ABP modules: build plan

Written 2026-09-29. Covers five new modules (BrainBuilder, Wrightspace (was WebBuilder), TridentDroid,
ModelMistress, Continuum), the module framework they share with the existing three (VM-Harness,
Hermes-Manager, TransferDaemon), and ABP Fabric: ABP's own edge proxy, containers, orchestration and
virtualization, in place of nginx, Docker, Kubernetes and QEMU.

This file is the single source of truth for these build sessions. Each phase lists:
- what it starts from;
- what it delivers;
- the checks that prove it is done (its **exit gate**).

A session picks the first phase whose dependencies are done, builds it, passes its gate, commits, and ticks
it in the status table (§9).

---

## 1. Where things stand (surveyed 2026-09-29)

| Project | Canonical copy | Git state | Stack | What exists |
|---|---|---|---|---|
| BrainBuilder | `Z:\Projects\BrainBuilder` | Branch `initial-implementation`, 10 commits not on GitHub. 139 uncommitted files: 98 deletions (the old `ModelBuilder/` tree), 32 edits, 7 new. 7 leftover `claude/*` worktree branches, and `.claude/worktrees/` sits inside the repo. | Rust workspace (`core`, `concierge-core`, `bot-server`, the Tauri `gui`); React and reactflow GUI; a PyTorch training bridge; `universal-harness` in TypeScript, which has an MCP server | A lot: a visual graph editor, training, prediction, intent-to-model, LLM authoring, component synthesis, a self-building agent. 44 test files. |
| WebBuilder, to be renamed **Wrightspace** | Server `C:\Projects\WebBuilder` | Server: `main` is 1 commit ahead of GitHub, with 84 uncommitted files (it deletes `packages/core/src/neural/**`, and `.pyc` files are tracked). **The `Z:\Projects\WebBuilder` copy has diverged:** 11 local "phase2 marketplace" commits, and 28 behind its `origin/main`. The history is split three ways. | A pnpm and turbo monorepo: TypeScript (218 .ts, 92 .tsx) plus Python (58 .py) plus a PyQt5 desktop app | A website builder: a drag-and-drop editor, export, AI chat, plugins, an MCP server (tool names were fixed in `35792a6`). Not yet an app platform. |
| TridentDroid | `Z:\Projects\TridentDroidEmulator` (its origin is `TridentDroid.git`) | 6 commits ahead of GitHub, 1 uncommitted file. `Z:\Projects\TridentDroid` is a near-empty stub, not a repo. | Rust: `trident-hal`, `trident-hal-kvm`, `trident-hal-whp`, `tridentd` (gRPC with mTLS), and the Tauri `tauri-gui` | A HAL for KVM and Windows WHP, the daemon, a gRPC API (including adb push/pull), a guest kernel and initramfs. Only 2 tests. The README targets Linux KVM with SR-IOV on an RX 7900 XTX. |
| ModelMistress | `Z:\Projects\ModelMistress` | **Not a git repo.** The GitHub repo `LoopyLuci/ModelMistress` exists but is **empty**. The README still says `model-mistress` and "Prometheus Serve". | Rust: `src` (backends, router, runtime, server, mcp, protocol, observability, plugins), `cli`, the Tauri `desktop-app`; plus a frontend, sdk, k8s, crds, a Dockerfile | An OpenAI-compatible server, an L7 router, a backend registry, MCP. 1 test file. |
| Continuum | Server `C:\Projects\Continuum` | Server: level with GitHub (`0dc73c6`), 1 uncommitted file (`continuum-security/src/lib.rs`). **The `Z:\Projects\Continuum` copy has diverged:** 5 local commits, and 24 behind. | A Rust workspace of 11 crates: transport (QUIC), ai, security, server, client, relay-server, ci, observability, plugin-sdk, test; plus mobile FFI and a web client | Remote desktop: QUIC with TLS 1.3, E2E encryption, Windows capture and input injection, multi-monitor, adaptive bitrate, recording, relay, plugins, and `continuum-ci.toml`. 3 test files. |

ABP today: VM-Harness, Hermes-Manager and TransferDaemon are modules. Each is a hand-written package in
`bot/<name>/`: `harness.py` (locate, clone, update, build, start), `client.py` (the control API), and
`tools.py` (agent and MCP tools). Each also has `bot/dashboard/<name>_api.py` and its own dashboard panel.
Where the checkout lives is resolved in this order: an env var, then `config/backends.yaml`, then a sibling
folder, then `data/modules/<Name>`. `bot/docker_mgr.py` and `bot/vm_mgr.py` drive the Docker CLI, QEMU,
Hyper-V and libvirt. The folders `Z:\Projects\NGINX`, `Docker`, `kubernetesSupercomputer`, `OmniRun` and
`hybrid_system` hold only a few loose files, so there is nothing there to reuse for Fabric.

**The lesson from the first three modules:** each took about 900 lines of near-identical plumbing. Doing
that five more times would waste most of the effort. So **phase M0 comes first**: one manifest-driven module
framework, with the existing three moved onto it.

---

## 2. The module contract: what "fully integrated, still a separate program" means

Every module repo must meet all of the following. The M0 conformance test checks each point
automatically.

1. **A separate program in its own repo.** It builds, runs, is tested and is released without ABP. ABP
   never imports its code; it talks to it over a process boundary.
2. **An `abp-module.toml` at the repo root**, the manifest ABP reads (§3.1). It declares:
   - identity and version;
   - how to build and check the toolchain;
   - the binaries, and how to start them;
   - the control endpoint;
   - health checks;
   - the capabilities that map to ABP areas;
   - its UI entry points;
   - the host requirements.
3. **A control hub.** This is a long-running local service, reached by HTTP on `127.0.0.1` with a random port
   and a random token. It writes `<data_dir>/control.json`: `{url, token, pid, version, api}`.
   - `GET /v1/health`
   - `GET /v1/operations`: the self-describing list of operations, with JSON Schema for each one's input
   - `POST /v1/call/{op}` with the arguments as the body; it answers `{"result": ...}` (the same shape VM-Harness and
     TransferDaemon already use)
   - `GET /v1/events`: a stream of events, as SSE
   - `POST /v1/service/stop`

   The token never leaves the machine, never appears in a UI, and is never logged.
4. **An MCP server** that serves the same operations. It runs over stdio (`<cli> mcp`), and over HTTP from
   the hub.
5. **A CLI, a GUI, and a TUI where it makes sense**, all driven by the same operations. Whatever the GUI
   can do, an agent can do too.
6. **A local CI/CD pipeline**, with the same shape as VM-Harness's `ci/pipeline.py`:
   - stages: preflight, static, tests, smoke (a real hub in a throwaway home), build, package;
   - a pre-push hook;
   - logs and reports;
   - a lock that takes over from crashed runs;
   - timeouts that kill the whole process tree;
   - a flaky test is re-run once.
7. **Updated from its repo.** ABP shows the commit, and how far ahead or behind it is. It updates with
   fetch, then a fast-forward only, then a rebuild. If the checkout has local changes, the update refuses and
   says so; it never discards anyone's work.
8. **Runs on this machine or on a peer.** Every operation can be sent to a linked machine through
   `peers.remote_control`. Anything done on Server happens where the user can see it: in the program's own
   window, or a titled console.
9. **No token-shaped secrets in the tests. It never defaults to Claude.** Model choices use OpenCode,
   OpenRouter or local models unless the user picks otherwise.

---

## 3. Phase M0: the module framework in ABP (before anything else)

**Delivers:** `bot/modules/`. It turns a new module into a manifest plus a thin optional adapter, instead
of about 900 lines each.

### 3.1 Manifest (`abp-module.toml`), v1

```toml
[module]
id = "brainbuilder"            # stable; used in URLs, tool names and data dirs
name = "BrainBuilder"
repo = "https://github.com/LoopyLuci/BrainBuilder.git"
branch = "main"
api = 1                        # module contract version
area = "models"                # which ABP area the dashboard puts it under

[checkout]                     # how ABP knows a folder is this module
marker = ["Cargo.toml", "gui/package.json"]

[toolchain]                    # checked before a build; a missing tool gives a clear message and a download link
require = [{ tool = "cargo", min = "1.80" }, { tool = "node", min = "20" }, { tool = "python", min = "3.11" }]

[build]
steps = [["cargo", "build", "--release", "-p", "bb-hub"], ["npm", "--prefix", "gui", "ci"]]
outputs = ["target/release/bb-hub{exe}"]

[hub]                          # the long-running control service
start = ["target/release/bb-hub{exe}", "serve", "--home", "{data_dir}"]
control_file = "{data_dir}/control.json"
health = "/v1/health"

[mcp]
stdio = ["target/release/bb-hub{exe}", "mcp", "--home", "{data_dir}"]

[ui]
gui = ["target/release/brainbuilder{exe}"]
tui = []
dashboard_panel = "operations"   # the generic panel, or "custom:<js file inside the module>"

[host]                           # where it can run
os = ["windows", "linux"]
needs = []                        # e.g. "kvm", "whp", "gpu", "android-sdk"

[pipeline]
run = ["python", "ci/pipeline.py"]
```

### 3.2 Pieces (all under `bot/modules/`)

| File | Job |
|---|---|
| `manifest.py` | Parse and validate the manifest (it has a schema and a version). Substitute `{exe}`, `{data_dir}` and `{repo}`. |
| `registry.py` | The known modules: built in (the 8 repo URLs), plus any added by URL in the UI. Resolve where each checkout is, with the same order as today: env `ABP_MODULE_<ID>_DIR`, then `modules.<id>.path`, then a sibling folder, then `data/modules/<Name>`. |
| `harness.py` | Generic `install_info`, `clone`, `update` (fast-forward only, refusing when there are local changes), `toolchain_check`, `build` (a job with streamed logs), `start_hub`, `stop_hub`, `hub_status`. This is taken from the three existing harnesses. |
| `client.py` | A generic hub client: read `control.json`, then `health`, `operations`, `call`, and the `events` SSE. |
| `tools.py` | Six generic agent tools, each taking `module` and `machine`: `module_list`, `module_status`, `module_operations` (search a hub's operations), `module_read` (read-only operations), `module_call`, `module_setup`. This stays small however many modules and operations there are; one tool per operation (`<id>__<op>`) would add hundreds of tools to every turn. |
| `bot/dashboard/modules_api.py` | `/api/modules`, `/api/modules/{id}/{info,clone,update,build,start,stop,call,events,pipeline}`, all peer-aware. |
| dashboard `modules-panel.js` (both UI copies) | One "Modules" page with a card per module: status, commit and ahead/behind, and Update, Build, Start, Open GUI and Run pipeline buttons. A generic operation runner builds a form for each operation from its JSON Schema. A module can supply its own panel. |
| `conformance.py` + `tests/test_module_conformance.py` | Checks every §2 point against a module: the manifest, the hub, operations, the MCP tool list, stop. |

### 3.3 Steps and exit gate

1. Build `manifest`, `registry`, `harness` and `client` with unit tests, using a fake module: a 60-line
   Python hub in `tests/fixtures/fake_module/`.
2. Build `tools.py`: generate the tools, and check that they are registered through the existing
   agent-tool and MCP registration paths.
3. Build `modules_api` and the dashboard panel. Test in the in-app browser against the fake module.
4. **Move VM-Harness, Hermes-Manager and TransferDaemon onto the framework.**
   - Add an `abp-module.toml` to each repo.
   - Keep their custom panels as `custom:` panels.
   - Their old routes stay as thin shims until the UI stops calling them. This keeps saved chats and tools
     working.
5. Update `docs/modules/README.md` with how to add a module.

**Gate:**
- ABP's full pipeline is green.
- The fake module and the 3 real ones pass conformance.
- In the in-app browser, each of the 3 can be updated, built, started and have an operation run.
- Running an operation on Server works through the peer link, and is visible on Server's desktop.

---

## 4. Phase R: bring the repos back to one truth (before any module work on them)

**Rules:**
- Nothing is discarded.
- Before touching any repo, write a backup bundle (`git bundle create --all`) plus a zip of its uncommitted
  files, in `Z:\Projects\_backups\`.
- Work on Server happens in a visible console.
- Pushing needs the user's OK (see the push-hold memory).

| Repo | Steps | Needs the user |
|---|---|---|
| **BrainBuilder** | (1) Commit the uncommitted work on `initial-implementation`. The `ModelBuilder/` deletions are part of it: that tree is already archived as `ModelBuilder.archive`. Add `.claude/worktrees/`, `checkpoints/`, `*.sqlite3` and `ci-reports/` to `.gitignore`, and untrack them. (2) Delete the 7 `claude/*` branches once each is confirmed merged or empty. (3) Merge `initial-implementation` into `main`. (4) Push. | Merging into main, and the push |
| **WebBuilder → Wrightspace** | (1) On Server, visibly: commit the 84 files (untrack `.pyc` and `__pycache__`; the `neural/` deletion is on purpose, since model-building moves to BrainBuilder). (2) On Z:, fetch Server's branch and GitHub. Rebase or merge the 11 "phase2 marketplace" commits onto it, so all three lines join. (3) Push the joined history. (4) Rename the GitHub repo `WebBuilder` → `Wrightspace`. GitHub redirects the old URL. Then update the remotes on both machines. (5) Server's `C:\Projects\WebBuilder` stays the canonical checkout, renamed `C:\Projects\Wrightspace`. The Z: copy becomes a clone of the same remote. | The name, the GitHub rename, the push |
| **TridentDroid** | (1) `Z:\Projects\TridentDroidEmulator` is canonical. Commit the 1 file; push the 6 commits. (2) Move `Z:\Projects\TridentDroid\tauri-gui` into it if it differs, then retire the stub folder. (3) Stop tracking the `run_*.txt` and `err.txt` logs; decide whether the guest kernel should use Git LFS or a release asset. | The push |
| **ModelMistress** | (1) `git init`. Add a `.gitignore` (`target/`, `node_modules/`). First commit. (2) Fix the README: its path and the "Prometheus Serve" name. (3) Push to the empty `LoopyLuci/ModelMistress`. | The push |
| **Continuum** | (1) On Server, visibly: commit the `continuum-security` edit after reviewing it. (2) On Z:, rebase the 5 local commits onto `origin/main`. Keep them if they add anything; drop them if they are already there. (3) Push. Server's copy stays canonical. | The push |

**Gate:**
- Each repo: one line of history, a clean working tree, level with GitHub.
- Both machines' copies point at the same remote.
- The backups exist.

---

## 5. Per-module plans

Each module's work runs in the same order:
1. **A.** Baseline: build it, run it, write down what works.
2. **B.** A local pipeline.
3. **C.** Hub, operations and MCP, following the contract.
4. **D.** The ABP manifest, plus conformance.
5. **E onward.** Features.

A through D make it a **working module**; after that, features land one at a time, each gated by the
pipeline.

### 5.1 ModelMistress: model loading and hosting (module 1: the smallest step, and the most value to ABP)

It becomes ABP's own inference layer: local weights served through an OpenAI-compatible API, which ABP's
model router can target like any provider.

| Phase | Delivers | Gate |
|---|---|---|
| MM-A | Build all 3 crates, run the server, and hit `/v1/models` and `/v1/chat/completions` against a GGUF from the local Ollama store. Write down what's real and what's stubbed. | A baseline note in `docs/STATUS.md` |
| MM-B | `ci/pipeline.py` (the Rust version: fmt, clippy `-D warnings`, test, smoke, release build, package with SHA-256) plus a hook | A full run is green |
| MM-C | Hub operations: `model.list/pull/load/unload/info`, `serve.start/stop/status`, `route.list/set`, `bench.run`, `metrics.get`; MCP over stdio and HTTP | Conformance passes |
| MM-D | `abp-module.toml`. ABP's providers get a `modelmistress` provider type, discovered automatically once the hub runs. The model router lists its models. | Chat in ABP through a ModelMistress-served local model, end to end |
| MM-E | Backends. llama.cpp (GGUF) first, then vLLM or ExLlama through a subprocess where the host supports it; Ollama-store import; the Unsloth weights ABP already has. Continuous batching; a KV cache; streaming. | Each backend passes the same OpenAI-compatibility test suite |
| MM-F | Hosting. Serve peers over Tailscale with per-peer keys; quotas; hot swap; multi-GPU placement. It uses Fabric's edge proxy once F1 lands. | A second machine chats through Server's ModelMistress |
| MM-G | Replace ABP's `bot/ollama` and `bot/unsloth` wiring with ModelMistress as the default local runtime, keeping them as backends | ABP's existing model tests pass with ModelMistress as the backend |

### 5.2 Continuum: screen sharing and full device control (module 2)

It becomes how agents and users see and drive a remote machine. VM-Harness already has `continuum/quic.py`
and `continuum/tailscale.py`; those clients move onto Continuum's hub.

| Phase | Delivers | Gate |
|---|---|---|
| CO-A | Build on Server and here. Connect Z: → Server, visibly. List what works (capture, input, multi-monitor, relay, recording) against the README's claims. | A status note |
| CO-B | Pipeline: `continuum-ci.toml` already exists, so wrap it in the standard `ci/pipeline.py` shape and add the hook. | Green |
| CO-C | Hub operations: `session.list/open/close`, `screen.shot`, `screen.stream.url`, `input.key/type/click/move/scroll`, `display.list`, `clipboard.get/set`, `file.send` (through TransferDaemon), `record.start/stop`, `peer.pair`. MCP. | Conformance; an agent takes a screenshot of Server and clicks through ABP |
| CO-D | Manifest. In the ABP dashboard, a "Remote screen" panel embeds Continuum's web client for a live view. Agent tools: `continuum__screen_shot` and the `input.*` operations. | A user in the dashboard watches and controls Server; an agent does the same through tools |
| CO-E | Control policy. Every session shows a visible banner on the controlled machine; consent is asked per peer; an audit log; an emergency stop (it uses ABP's existing estop). | Tested: no session without the banner, and estop cuts it within 1 s |
| CO-F | Linux and macOS capture (PipeWire and ScreenCaptureKit) in place of the synthetic frames; Android through TridentDroid and adb (with 5.3); the mobile client. | Real frames on each OS in CI where possible, otherwise a manual note |
| CO-G | Agent-grade control: an accessibility tree and OCR per frame, so agents act by element instead of by pixel; a recorded session can be replayed as a test | An agent finishes a scripted task on Server with no pixel coordinates |

### 5.3 TridentDroid: Android emulation (module 3; Wrightspace depends on it)

| Phase | Delivers | Gate |
|---|---|---|
| TD-A | Build `tridentd` plus the GUI on Windows with the WHP HAL (this machine) and on Linux with KVM (the Omarchy VM's host, or a Linux peer). Boot the existing guest kernel. **Hardware check:** the README assumes SR-IOV on an RX 7900 XTX; write down which hosts actually have KVM, WHP or a GPU. | A status note, including which host is the reference |
| TD-B | Pipeline plus hook. The HAL tests need a hypervisor, so they run only on hosts that declare one (`[host] needs`). | Green |
| TD-C | Hub operations: `device.list/create/start/stop/snapshot/restore`, `image.list/pull` (AOSP, LineageOS, GrapheneOS images), `adb.shell/install/push/pull/logcat`, `screen.shot/stream`, `input.*`, `sensor.set`. The gRPC API stays; the hub is a thin layer on top. MCP. | Conformance; install and launch an APK through ABP |
| TD-D | Manifest. An "Android devices" panel. TransferDaemon's Android build and ABP's `android_apk.py` can deploy to a TridentDroid device. | An ABP agent builds an APK, installs it on a TridentDroid device, and screenshots it |
| TD-E | Performance: snapshot boot in under 2 s, GPU through virtio-gpu or Venus, then SR-IOV where the hardware has it | The README's figures measured, or corrected |
| TD-F | Android version range and custom ROMs; Play-compatible images where the licence allows; a device farm across peers, which uses Fabric's orchestrator (F3) | A 3-version matrix test run of one APK |

### 5.4 BrainBuilder: model and neural-network building (module 4)

| Phase | Delivers | Gate |
|---|---|---|
| BB-A | After R: build the Rust workspace and the GUI, and run the 44 test files. Write down what works end to end: build a graph, train, predict. | A status note |
| BB-B | Pipeline: Rust, Node and Python stages, the Playwright e2e tests already in `gui/e2e`, and a hook | Green |
| BB-C | Hub operations: `graph.list/create/validate/export`, `component.list/synthesize`, `dataset.inspect`, `train.start/stop/status/metrics`, `checkpoint.list/export`, `predict.run`, `intent.propose`. `universal-harness`'s MCP server becomes, or is replaced by, the hub's. | Conformance; an agent turns "classify these images" into a trained checkpoint through ABP |
| BB-D | Manifest. Links to ModelMistress: export a trained model to GGUF or safetensors, then `modelmistress.model.load`. Links to ABP's Unsloth: fine-tuning jobs go through the same training operations. | Train → serve → chat, all driven from ABP |
| BB-E | Training on peers: send jobs to the machine with the best GPU, through Fabric (F3) | A job sent from Z: runs on Server's GPU and reports back |
| BB-F | The self-building agent's worktree flow moves onto ABP's agent runtime, and never defaults to Claude | The agent adds a component, with tests gated |

### 5.5 Wrightspace (was WebBuilder): the universal application builder (module 5; the largest)

**Name.** *Wrightspace*: a *wright* is a maker (shipwright, playwright), and a *space* is where agents and
people build together. It is short, original, and fits "agents that build applications". A web search on
2026-09-29 found no app-building product with that name. Alternatives, if the user prefers: *Loomwright*,
*Omniwright*.

**Goal:**
- Build apps for web, Android, iOS, desktop and any language.
- Match VS Code's feature set.
- Agents are first-class builders.
- TridentDroid is built in, for Android runs across devices.

**Strategy, and why:** writing an editor from scratch to match VS Code would take years. Instead, build on
the open-source **Code - OSS / OpenVSCode Server core**, which already has the editor, the extension API,
debug, the terminal and SCM, served on the web and wrapped for desktop. That gives VS Code parity by
construction. Wrightspace's own work goes into what VS Code lacks:
- agent-driven building;
- visual builders;
- project templates for every target;
- asset generation;
- build and signing pipelines;
- device testing.

Extensions come from **Open VSX** (the VS Code Marketplace's terms don't allow other IDEs to use it). The
existing drag-and-drop web builder, export and AI chat become Wrightspace extensions.

| Phase | Delivers | Gate |
|---|---|---|
| WS-A | After R and the rename: build the current monorepo on Server, visibly. Write down what works. | A status note |
| WS-B | Pipeline (pnpm and turbo, Python, e2e) plus a hook | Green |
| WS-C | The core: vendor OpenVSCode Server at a pinned tag, with a patch set kept small and rebased on each upstream release; Open VSX. Desktop through the existing shell, or Tauri. | It opens a folder, edits, debugs a Node and a Python program, uses the terminal and git, and installs an Open VSX extension |
| WS-D | Hub operations: `project.create/open/list`, `template.list`, `build.run`, `run.start/stop`, `test.run`, `package.sign`, `deploy.run`, `asset.generate`, `agent.task`; MCP; plus `abp-module.toml`. | Conformance; from ABP, an agent creates a project, builds it and runs it |
| WS-E | Targets, each as a template plus a build recipe plus a run target: web (React, Vue, Svelte), Node, Python, Rust, Go, .NET, Java/Kotlin, C/C++; **Android** (Gradle plus the SDK, run on **TridentDroid** through its hub); desktop (Tauri, Electron); **iOS** (Swift/SwiftUI, React Native, Flutter). | Each target builds hello-world, then runs it on its target. iOS: see the constraint below. |
| WS-F | Visual builders: the old web builder as an extension; a screen builder for Android and iOS that writes real Compose or SwiftUI code | A round trip: visual, then code, then visual, with no loss |
| WS-G | Asset generation: icons, splash screens, images and audio through ModelMistress-hosted models; store listings | One command fills in an app's store assets |
| WS-H | Agents: plan → build → test on TridentDroid → fix, as a loop, with every step visible in the IDE | An agent ships a working Android app from a one-paragraph description |

**The iOS constraint:** Apple's toolchain (Xcode, the simulator, signing) runs only on macOS. Wrightspace
builds iOS by sending the build to a **macOS peer** (a Mac on the tailnet, or a cloud Mac runner) through
the same remote-build operation. Without one, iOS projects can be edited and cross-platform code
(React Native, Flutter) runs on Android and web, but iOS binaries can't be built. This is written into the
iOS templates, not hidden.

### 5.6 CacheIt: tiered caching for everything ABP moves (module 6, added 2026-09-29)

`X:\Projects\CacheIt`, now `LoopyLuci/CacheIt`. A Rust workspace: engine, WAL, storage, API, MCP, CLI, TUI, desktop,
web.

**Baseline (CI-A, 2026-09-29):**
- It builds, and 43 tests pass.
- **The daemon is a demo.** It seeds invented tiers (`/dev/nvme0n1` on Windows), invented volumes and 10
  zero-filled "cached" blocks, and reports stats from them.
- It hard-codes `127.0.0.1:8080`, which Windows reserves on this PC (7981-8080), so it can't start here.
- The RAM and SSD tiers' `read`/`write` are stubs, and the block cache is an unbounded map.
- **Real and tested:** the replacement algorithms (ARC, LRU, LFU, hybrid) and the WAL.
- The README describes PrimoCache-style volume caching as done. On Windows that needs a signed kernel filter
  driver.

**Direction:** make the engine a real **tiered object cache** first, because that speeds up ABP's own data flow
now. Keys and bytes go RAM → disk, eviction uses its ARC/hybrid policies, disk-tier writes are crash-safe, and
namespaces, TTLs and content addressing are built in. ABP and the cluster use it through an API, MCP and a Python
client. Volume (block-level) caching is a later, separate phase.

| Phase | Delivers | Gate |
|---|---|---|
| CI-A | Baseline (above) | done |
| CI-B | A local pipeline (fmt, clippy, test, build, smoke against a real hub) plus a hook; an honest README | Green |
| CI-C | The `cacheit-store` crate. **L1 RAM:** a byte capacity, ARC/hybrid eviction, and evicted entries demote to L2. **L2 disk:** sharded files, with a header per file carrying the key, namespace, TTL and checksum; an LRU byte cap; the index rebuilt by scanning at start, so there is no index to corrupt; L2 hits promote back to L1. Namespaces, TTL, content addressing (`put_cas` gives a sha256 key), pin/unpin, and per-tier stats. **The hub** in `cacheitd`: config (data dir, L1/L2 sizes, port 0 = random), `control.json`, Bearer token, `/v1/health`, `/v1/operations`, `/v1/call/{op}`, `/v1/service/stop`, plus a binary fast path `GET/PUT/DELETE /v1/objects/{ns}/{key}`. The demo seeding is removed. | Unit tests per tier and policy; a crash-safety test (kill during writes, restart, nothing corrupt); the hub passes ABP conformance |
| CI-D | **MCP:** `cacheit mcp` serves the hub's operations over stdio. The **`abp-module.toml`** in the repo. **ABP's client** `bot/cache.py`: get/put/get_or_compute with namespaces and TTLs, and a no-op fallback when CacheIt isn't running. Module tools and page through the framework. | An agent stores and reads a value through `module_call`; ABP's client measurably speeds up a repeated computation |
| CI-E | ABP's hot paths use it: web fetches (a short TTL), embeddings, provider model lists, the cluster's job inputs and outputs (CL6, content-addressed), module build caches (sccache-style), and dataset shards for BrainBuilder. Each is opt-in per feature, with its hit rate shown. | Each: a before/after timing |
| CI-F | Distributed: nodes share one logical cache (consistent hashing, replication 2, read-through from peers) over the cluster's peer links | A value put on this PC is read from Server's cache |
| CI-G | Volume caching. Linux: manage dm-cache / bcache / dm-writecache. Windows: a volume upper-filter driver (signed; its own project, with a written requirements and risk list first). | Measured read latency on a cached volume |

---

### 5.7 The Octopus estate: a friend's server, integrated both ways (added 2026-09-29)

Octopus-Security: ~40 services on a NixOS server behind octopus-auth (RS256 SSO, TOTP), with octopus-router as its
model and coding hub, whose Bot Platform view already drives ABP. User guide: `docs/octopus.md`.

| Phase | Delivers | Gate |
|---|---|---|
| OT-A | Integration keys (scoped, route-allowlisted; the Router preset), framing allowlists, bot credentials masked for them | A contract test replaying the Router's calls; everything outside the scopes refused |
| OT-B | `bot/octopus`: estate catalog + live status, Router client + provider, SSO; the Octopus page | Live: both directions against the Router's latest upstream |
| OT-C | Connectors: one runtime (`abp-octopus-connector`) + one private repo per web service, specs generated from source; ABP registers them and hands them the session | Every connector's pipeline passes; a live call to the real estate |
| OT-D | Services with no web API: vault (records, handoff), nixos-hetzner/pentest-flake (NixOS deploys through Fabric), Cephaloscan/PentestPlayground (run as modules), alfred-js, simplex, mail, conversation-exporter | Each reachable from ABP |
| OT-E | Portainer's replacement (the owner's goal for ABP's Docker manager, see octopus-ops/PORTAINER-EXIT.md): stacks from git, redeploy, host status, a fixed verb set, on the estate's server | The Router's Bot Platform view manages the estate's stacks through ABP |
| OT-F | Hand-written summaries and typed inputs for the most used operations; connectors refreshed when upstream routes change | A refresh with no lost summaries |

### 5.8 Linux platforms (added 2026-09-29)

| Phase | Delivers | Gate |
|---|---|---|
| LX-A | A Nix package (server, CLI, TUI from nixpkgs), `services.agentic-bot-platform`, a NixOS VM test (`nix/`, `flake.nix`) | Builds and passes its VM test on NixOS 26.05 |
| LX-B | The installer on Debian-family distros: `scripts/install.sh` end to end, then server, token auth, CLI, TUI, dashboard in a browser, the test suite | Green on Ubuntu 26.04 and Debian 13 VMs (on Server) |
| LX-C | System-service installs and an updater on every distro family (apt, dnf, pacman, zypper, apk, NixOS) | Install, update and roll back tested on each |

### 5.9 Any project as a module, and programs that use each other (added 2026-09-30)

| Phase | Delivers | Gate |
|---|---|---|
| MK-A | `abp_modkit` (in ABP, stdlib only): detect a project's stacks, write `abp-module.toml` + `abp-ops.toml`, one generic hub (http routes, commands, a managed service, MCP), publish to a private repo with a secret scan. ABP: "Add a project" on the Modules page, `abp modules ...`, the `module_adopt` tool. Docs: `docs/modules/modkit.md` | Every adopted project's hub passes `abp_modkit check` |
| MK-B | Modules use ABP back: `[abp] connect` (ABP mints the module its own scoped key, `ABP_URL`/`ABP_KEY`); `[provider] openai` (a module's OpenAI-compatible API is an ABP provider, with a key from the environment or its hub's store) | A module calls ABP with its key, and ABP chats through a module's API |
| MK-C | The user's projects adopted (34) and published (private unless already public), each with real descriptions | All registered, no manifest errors |
| MK-D | Two-way integrations, each still its own program: Cognitive Companion, Kestrion, KotMoE; Alfred (the Octopus estate) as a runner connector | Live in both directions against the running ABP |
| MK-E | GAImer on AMD: ROCm/HIP, Vulkan (ncnn), DirectML, CUDA, XPU, MPS, CPU behind one `src/accel.py` | Its tests on the 7900 XTX with ROCm |

### 5.10 The Module Management Hub: any GitHub repo as a module (added 2026-09-30)

Third-party repos (OpenCV's, anyone's) become modules without forking them or committing into them: an **overlay**
holds their `abp-module.toml` + `abp-ops.toml` on ABP's side, and the checkout stays exactly upstream.

| Phase | Delivers | Gate |
|---|---|---|
| MH-A | Overlay modules: `abp_modkit adopt --overlay DIR`; ABP reads overlays shipped in `catalog/<id>/` and made by users in `data/module-overlays/<id>/`; the hub runs the overlay's ops against the checkout | An overlay module passes `abp_modkit check` with a pristine `git status` in its checkout |
| MH-B | From a URL: `abp modules add <github url>` / `POST /api/modules/add` / the `module_add` tool: clone (shallow by default, to `modules.clone_root`), detect, write the overlay, check, register, as a job with progress; the Module Hub page (URL, what was detected, the operations, build and test, register) | A repo nobody prepared becomes a working module in one step, from the page and from an agent |
| MH-C | Updates: fetch upstream, re-detect, keep hand edits, show what changed in the operations | A refresh after an upstream change loses nothing hand-made |
| MH-D | Assisted setup through GEN (§5.12): descriptions and curated summaries from the README and code, typed inputs for the most used operations, a generated panel for the module, previewed before it is kept | The Hub proposes, the user picks between variants, nothing is kept unseen |
| MH-E | Sharing: overlays published to a catalog repo; installing a catalog entry clones upstream and applies its overlay | Another machine installs an overlay module from the catalog |

### 5.11 Computer vision for every agent (added 2026-09-30)

OpenCV's 14 repos are modules (overlays, MH-A), and vision is built into ABP itself (`bot/vision`), so any agent,
the dashboard, the CLI and MCP clients have it without a module running.

| Phase | Delivers | Gate |
|---|---|---|
| CV-A | The OpenCV family as overlay modules, checkouts in `E:/Projects/OpenCV` (opencv, opencv_contrib, opencv_extra and opencv_3rdparty shallow): build, tests, samples, benchmarks, model-zoo operations | Each passes `abp_modkit check`; upstream checkouts untouched |
| CV-B | `bot/vision`: images from files, URLs, the screen and cameras; the operations (resize, crop, convert, blur, edges, contours, threshold, histogram, template and feature matching, image diff); the model zoo (downloaded on demand, verified): faces (YuNet + SFace), objects (YOLOX / NanoDet), text (PP-OCR detection + recognition), QR codes, classification, segmentation, pose, tracking | Real models on real images in tests; results match the zoo's own demos |
| CV-C | Agents: `vision_*` tools for ABP's agents, ABP's MCP server, `abp vision ...`, and the Vision page (upload / screen / camera, run a pipeline, see the overlays) | An agent answers a question about a screenshot with a vision tool call |
| CV-D | The screen: find text and UI elements on screen (OCR + template/feature matching) for computer use; visual regression for GEN previews (a before/after image diff) | GEN's preview reports what visibly changed |
| CV-E | Benchmarks: cvbenchmark, opencv_benchmarks and the zoo's benchmark, per device (CPU, OpenCL, Vulkan, CUDA); COOL-Benchmark on cloud machines through a multi-cloud layer (AWS, GCP, Azure, Oracle, Hetzner, DigitalOcean, Vultr, Akamai/Linode, OVHcloud, Scaleway): create, benchmark, collect, destroy, with the owner's own keys and a spending cap | One benchmark result from this PC and one from a cloud machine |
| CV-F | OpenCV built from source with contrib and the accelerators this machine has (OpenCL / Vulkan on the 7900 XTX; CUDA where present), its Python bindings built with opencv-python's builder | ABP's vision runs on the built OpenCV and is faster than the wheel on a zoo benchmark |
| CV-G | The rest: onnx-conformance-proxy scores OpenCV DNN's ONNX coverage; bpc (bin picking) and open_vision_capsules as pipelines; the Jetson and iOS samples as reference modules (no runtime on this PC) | Each has operations that run, or says honestly why it can't here |

### 5.12 Generative code and GUI with real-time preview (added 2026-09-30)

Builds on `bot/ui_customize.py` (describe a change, a live preview and diff, apply on approval, one-click revert).

| Phase | Delivers | Gate |
|---|---|---|
| GEN-0 | Layer 0, by hand: the Studio page. A live preview of any page or component, served from a sandbox copy of ABP's UI (a git worktree) that reloads as files change; variants (A/B/C...) switched instantly; a component gallery (buttons, chat interfaces, panels, settings) with property and theme editors; backend changes previewed on a second ABP server started from the sandbox; apply = a commit, revert = one click | A change is seen live, compared across variants, applied and reverted without restarting ABP |
| GEN-2 | Layer 2, models: local and API models (through ABP's router) produce several variants of a code or UI change; each is validated (syntax, tests in the sandbox, a visual diff through CV-D) before it is shown. Everything is logged: request, context, variants, validation, what the user kept and why | Accepted and rejected variants are in the log with their reasons |
| GEN-D | Data: the log becomes datasets (instruction to diff, preference pairs) and Knowledge Modules automatically, for GEN-1 and the AM models | A dataset and a Knowledge Module built from real sessions |
| GEN-1 | Layer 1, ABP's own models (built with KotMoE / BrainBuilder, trained on GEN-D): UI intent to component, layout and theme suggestions, ranking variants before they are shown, small code edits | Ranks the variant users keep first more often than chance, measured on held-out sessions |
| GEN-3 | Every applied change goes through the pipeline in the sandbox first | A change that breaks a test is stopped before it reaches the running ABP |

### 5.13 ABP's own models (added 2026-09-30)

Small, specific models for ABP's own decisions, each with a heuristic fallback, trained on data ABP already logs,
evaluated offline, run in shadow, promoted only when they beat what they replace.

| Phase | Delivers | Gate |
|---|---|---|
| AM-A | An audit of what KotMoE and BrainBuilder can really train and serve today (KotMoE's API answered with templated text in the 2026-09-30 test), and what each model below needs from them | A written finding per engine |
| AM-B | The designs: request routing (backend/model per request), intent and slash-command classification, tool-call success prediction, log anomaly and crash prediction (Sentinel), cache prefetch (CacheIt), repo-to-stack detection (MH), screen-element detection (CV-D), UI generation ranking (GEN-1): inputs, labels, model family, size, latency budget, evaluation | One page per model |
| AM-C | The shared pipeline: dataset export from ABP's logs, training on KotMoE / BrainBuilder, evaluation gate, shadow run, promotion, rollback | One model through the whole loop |
| AM-D | The models, one at a time, ordered by the value of the decision they make | Each beats its heuristic in shadow before it is promoted |

## 5a. ABP Cluster: paired machines as one computer (inside ABP, `bot/cluster/`)

Asked for on 2026-09-29. Paired devices share resources and act as nodes in a cluster that can run any kind of
work. It is built into ABP itself, not a module, because it is about ABP installations trusting each other. It
reuses what ABP already has:
- **peer links** for trust and transport;
- **Power** to keep nodes awake and wake them;
- **Windows job objects** to cap each job's CPU and memory;
- **the module framework**, so module operations and builds can run on any node.

Fabric's orchestrator (F3) later puts a Kubernetes-compatible API on top of this layer; it does not replace it.

**Principles:**
- **Consent first.** A machine offers nothing until its owner turns sharing on. The owner decides what is
  offered: a CPU share, RAM, which GPUs, disk space and a work folder, which kinds of job, which peers may use
  it, and when (always, or only while the machine is idle).
- **Every node schedules; there is no single point of failure.** The node where a job is submitted places it.
  The chosen node checks its own budget and accepts or refuses, which reserves the resources atomically, so two
  schedulers can never double-book it.
- **The owner's limits are enforced, not advisory.** CPU rate and memory are hard caps (job objects on Windows,
  cgroups on Linux), and each job gets its own work folder.
- **Every job is recorded** (SQLite), with its logs, exit code, result and files. It survives restarts, and
  every job is audited.

| Phase | Delivers | Gate |
|---|---|---|
| CL1 Inventory & membership | Each node describes itself: CPU (model, cores, threads), RAM, GPUs (name, VRAM; live use where measurable), disks, OS, hypervisors, toolchains, installed modules, local models. Live load comes from a heartbeat to every linked peer, and nodes are marked ok, stale or down. | The Cluster page shows this PC and Server with real hardware and live load |
| CL2 Offers | Each node's owner sets its offer (dashboard only, never by a peer). ABP tracks the budget and reservations against it. | A peer's job is refused with a clear reason when it doesn't fit the offer |
| CL3 Jobs | Job kinds: `command` (an argument list, no shell), `python`, `module_op`, `module_build`, `inference` (a local model). Each runs in its own work folder with hard caps; logs stream, jobs can be cancelled, and result files can be downloaded. | A job submitted here runs on Server with its CPU and RAM capped, and its logs and files come back |
| CL4 Scheduler | Requirements (cpu, ram_gb, gpu, vram_gb, os, needs, module, kinds). Nodes are filtered, then scored (free share, data locality, latency), then reserved. On a refusal it tries the next node. Retries and rescheduling cover lost nodes; nodes can be drained. | A job lands on the only node that fits; one killed mid-run is rescheduled |
| CL5 Groups | **Gang jobs**: N replicas, all-or-nothing, spread across distinct nodes, with rank, world size, peer addresses and MASTER_ADDR/PORT in their environment, for torch.distributed, MPI-style and custom protocols. **Job arrays / map**: split inputs across nodes and gather the results. | A 2-node gang (this PC and Server) finds its peers; a 20-task array spreads and gathers |
| CL6 Data | Inputs and outputs move between nodes through a content-addressed cache (chunked HTTP now, TransferDaemon later). Jobs prefer nodes that already hold their data. | A 1 GB input goes once, and a second job reuses it |
| CL7 Pools | The model router sends inference to whichever node has the model loaded (with MM-F). BrainBuilder training goes to GPU nodes (BB-E), TridentDroid devices to hypervisor nodes (TD-F), and agent swarms spread across nodes. | Chat on this PC is answered by a model on Server, picked automatically |
| CL8 Wake & power | Sleeping nodes are woken (Wake-on-LAN) when a job needs them, and kept awake while they run jobs; the idle offer respects the user's activity | A job wakes Server, runs, and lets it sleep again |

---

## 6. ABP Fabric: ABP's own proxy, containers, orchestration and virtualization

**Target:** do what nginx, Docker, Kubernetes and QEMU do, under ABP's control, built to outlast any one
vendor.

**Scope:** "Full parity" is measured against a written checklist per system, and proven by conformance
tests, never claimed. Kubernetes and QEMU are each millions of lines built by thousands of engineers. So
Fabric **speaks their open standards and reuses proven engines where rewriting them adds nothing**:
- OCI images and runtime spec;
- the CRI;
- a Kubernetes-compatible API subset;
- virtio;
- QMP.

It is custom where that pays off: one control plane, agent-native operations, ABP's peers as the cluster,
Windows as a first-class host, and a self-healing loop.

**Surviving 100 years:**
- Open formats only (OCI, OpenAPI, TOML/JSON, plain files, SQLite); no private lock-in.
- APIs are versioned, and there is a migration tool for each version.
- Builds are reproducible, with all dependencies vendored.
- Every engine sits behind an interface, so it can be swapped when better ones appear.
- Documentation lives beside the code, and conformance suites pin behaviour.

Fabric is its own repo and module (`LoopyLuci/ABP-Fabric`, which needs the user's OK to create), in Rust.
It has one daemon, `fabricd`, with four subsystems:

| Layer | Replaces | Built as | Parity checklist (each item has a conformance test) |
|---|---|---|---|
| **F1 Edge** | nginx | A Rust proxy on `hyper` and `rustls` (Pingora's design) | HTTP/1.1, HTTP/2, HTTP/3 (QUIC); TLS with automatic ACME certificates and SNI; reverse proxy and load balancing (round robin, least-conn, hash, health checks); WebSocket and gRPC; static files with range and compression; caching; rate and connection limits; rewrites and redirects; auth (basic, JWT, mTLS); access logs and metrics; config reloads with no dropped connections; TCP/UDP stream proxying. Its config is declarative, and every change is also an ABP operation. |
| **F2 Containers** | Docker | An OCI image store and builder plus the runtime spec | Pull and push to any OCI registry; build from a Dockerfile (BuildKit-compatible subset); run with namespaces, cgroups v2, seccomp and rootless on Linux, using `youki` (a Rust OCI runtime) behind an interface; on Windows and macOS, the same runtime inside a small Fabric Linux VM (from F4). Covers volumes, networks, port publishing, logs, exec, stats, and Compose files. ABP's `docker_mgr.py` operations keep working, now pointed at Fabric. |
| **F3 Orchestrator** | Kubernetes | A declarative controller over ABP peers (the tailnet is the cluster network) | The core Kubernetes API objects (Pod, Deployment, StatefulSet, DaemonSet, Job, CronJob, Service, Ingress (served through F1), ConfigMap, Secret (sealed through ABP's vault), PersistentVolume); a scheduler that knows about GPUs, hypervisors and OS; health checks and self-healing; rolling and canary rollouts; autoscaling; `kubectl` works against its API for the covered subset. Workloads: containers (F2), VMs (F4), Android devices (TridentDroid), models (ModelMistress), training jobs (BrainBuilder). |
| **F4 Virtualization** | QEMU/libvirt (control), Hyper-V/WHP/KVM (execution) | **Phase F4a:** unify VM-Harness, `vm_mgr.py` and TridentDroid's HALs behind one Fabric VM API: QEMU through QMP, Hyper-V, WHP, KVM. **Phase F4b:** a lean VMM of its own for Linux and Windows guests, sharing `trident-hal-*` (virtio devices, snapshots, live migration between peers), with QEMU kept as the fallback for exotic hardware. | Covers the lifecycle, snapshots, disks (qcow2, raw), networking (bridge, NAT, tap), virtio-gpu, a display over VNC or SPICE or Continuum, live migration, and the QMP-compatible commands VM-Harness uses. |

| Phase | Delivers | Gate |
|---|---|---|
| F0 | The repo, `fabricd` skeleton, the hub contract, the manifest, the pipeline | Conformance |
| F1 | The edge proxy, then its parity checklist item by item. Once HTTPS works, ABP's dashboard and the module hubs get HTTPS through it. | Each checklist item's test; ABP served through F1 |
| F4a | The unified VM API over the existing engines | VM-Harness's and TridentDroid's operations work through Fabric |
| F2 | The image store, the Linux runtime, then Windows and macOS through a small VM | Runs the images ABP already uses; Compose files work |
| F3 | The orchestrator: single node, then multi-node over peers | A Deployment of 3 replicas survives one peer being killed |
| F4b | Fabric's own VMM | Boots Linux and Android guests; matches QEMU on the tested workloads |

---

## 7. Order and dependencies (the critical path)

```
M0 framework ─┬─> R (all repos; done alongside M0, since it touches only the repos)
              │
              ├─> ModelMistress A-D ─> E ─> F,G
              ├─> Continuum A-D ─> E ─> F,G
              ├─> TridentDroid A-D ─┬─> E,F
              │                     └─> Wrightspace A-D ─> C,E(android) ─> F,G,H
              ├─> BrainBuilder A-D ─> E (needs F3), F
              └─> Fabric F0 ─> F1 ─> F4a ─> F2 ─> F3 ─> F4b
```

- M0 comes first: every module after it costs about a third as much.
- ModelMistress and Continuum come next: both are small steps with high value (ABP's own inference; seeing
  and driving Server).
- TridentDroid comes before Wrightspace's Android work.
- Fabric runs as its own track after M0. F3 unlocks BB-E and TD-F.

---

## 8. How to run a build session quickly

1. Read §9 and pick the first unticked phase whose dependencies are done.
2. Read only that module's `docs/STATUS.md` and this phase's row. Don't re-survey.
3. Build, test and pass the gate. Commit locally in the module's repo and in ABP. **Never push without the
   user's OK.**
4. Tick the phase in §9, with the commit ids.
5. Anything done on Server happens in a titled console or the program's own window on its desktop, never as
   silent SSH.
6. Stopping mid-phase: write what's done and the next step into §9's notes column.

---

## 9. Status

| Phase | State | Commits / notes |
|---|---|---|
| M0 framework | **done** 2026-09-29 (on this PC) | `96a561a` plus a page fix. The ABP pipeline is green (3250 passed) and deployed. In the in-app browser: the Modules page lists all 8, VM-Harness's hub starts and stops, and an operation runs through its adapter. `modules.build_cache` puts cargo target dirs on fast storage. **Open:** (1) Server's ABP (an installed copy, not git) still runs the old code; update it when ABP is next pushed. (2) Server's `peers.remote_control` allows only hermes-manager and power, so driving Server's modules from here needs its owner to tick "Modules" on Server's Power page. (3) The three adapter modules get their own `abp-module.toml` in their phase D. |
| R BrainBuilder | **done** 2026-09-29 | `20787e4` committed the work in progress; PR #1 merged, `main` = `b196a4a`. The 7 `claude/*` branches are deleted (all already merged); the old worktrees and their unfinished patches are in `D:\Backups\Projects\BrainBuilder-worktrees-20260929`. Pipeline green. |
| R WebBuilder → Wrightspace | **done** 2026-09-29 | Server's work is committed and pushed (`cc92e40`). The GitHub repo is renamed `LoopyLuci/Wrightspace`. Server's folder is `C:\Projects\Wrightspace`, and the Z: copy is `Z:\Projects\Wrightspace`. The unrelated May 2026 codebase (IR round-trip, multi-framework emitters, agent-builder benchmark, marketplace) is kept as branch `legacy/may-2026`, to port from in WS-F/WS-H. Server's `main` still needs `--set-upstream-to=origin/main`. |
| R TridentDroid | **done** 2026-09-29 | Run logs untracked, pushed (`5d501a9`). The canonical folder is now `Z:\Projects\TridentDroid` (renamed from TridentDroidEmulator; the old stub is in the backups). |
| R ModelMistress | **done** 2026-09-29 | `git init`, README fixed, first push (`7aae7da`). |
| R Continuum | **done** 2026-09-29 | `main` did not build (security modules not declared); fixed and pushed (`86287aa`, 435 tests pass). Server and Z: are level. The Z: copy's unrelated older history is kept as local branch `z-local-v1.1`. |
| MM-A…G | A–D done, E mostly | see below |
| CO-A…G | todo | |
| TD-A…F | todo | |
| BB-A…F | todo | |
| WS-A…H | todo | |
| F0, F1, F4a, F2, F3, F4b | todo | needs the new repo |
| CL1…CL5 cluster | **done** 2026-09-29 | `64137dd`, `63e3e23`, pushed. Tested with Server as the second node: a capped job on Server, a 2-node gang meeting over Tailscale, an 8-task array spread 4/4. Server's ABP is updated, and both machines share modestly (this PC 25% CPU / 8 GB, Server 50% / 8 GB). |
| CL6…CL8 | todo | CL6 comes with CacheIt CI-E |
| CacheIt CI-A | **done** 2026-09-29 | `LoopyLuci/CacheIt` created (public, topics, wiki off, delete-on-merge); the baseline is in §5.6 |
| CacheIt CI-B, CI-C | **done** 2026-09-29 | CacheIt `08f5b1c` (pushed; its pipeline passed through its pre-push hook). `cacheit-store` has 16 tests; the hub has 13 operations and a binary fast path; the CLI works and so does `cacheit mcp`. CacheIt passes ABP conformance, the first module on the generic path. README rewritten honestly; MIT/Apache license files added (the README already declared them); `Cargo.lock` committed. |
| CacheIt CI-D | **mostly done** | `bot/cache.py`: get/put/get_or_compute, a no-op fallback, a pooled client (0.44 ms per 1 KB get). Left: the TUI and desktop app still use the older `/api/*` routes |
| CacheIt CI-E…G | todo | Hot paths (each measured), distributed, volume caching |
| MM-A | **done** 2026-09-29 | Builds in 94 s (E:). The server hard-codes 127.0.0.1:8000 (ignoring its own `listen_addr`), which Windows reserves here, so it can't start. Chat is proxied to Ollama. **Its own CPU engine is a stub**: `LoadedModel::generate` returns a placeholder string. So MM-E is the core of it: llama.cpp's `llama-server` as a managed backend process (built from source with the cmake and gcc already here, or its release binary if the user approves the download), with GGUFs from the Ollama store and Unsloth. |
| MM-B, MM-C, MM-D, most of MM-E | **done** 2026-09-29 | llama.cpp built from source here with Vulkan (MSVC 2019, `E:/abp-build/llama.cpp`): 112 tokens/s generating on the 7900 XTX with Qwen3.5-9B. ModelMistress got a real core (`catalog`, `engine`, `hub`, `client`): a GGUF catalog over Ollama stores, HF caches and folders; one `llama-server` per loaded model (loopback, random port and key, in a job object so it dies with the hub even on a hard kill); load on first use, LRU eviction, `ollama/<name>` pass-through; 13 operations, the OpenAI API behind the hub token, MCP. Its pipeline passes including a live chat; pushed (`1154cec`). In ABP: the hub is a module (conformance ok) and, while it runs, `modelmistress` is a provider (`bot/providers.module_providers`, token read from `control.json`, never stored), so `modelmistress/<model>` works anywhere a provider model does. The first design's stub modules stay, marked legacy (deleting them was not approved). Open: embeddings-only mode, vLLM/ExLlama, `model.pull`, the desktop app. |
| OT-A, OT-B | **done** 2026-09-29 | Integration keys + the Router preset (`tests/test_integrations.py`); `bot/octopus` and the Octopus page. Live against the Router's latest upstream (`7838cbd`): its Bot Platform view drives ABP through a scoped key (bots with masked credentials, hosts incl. Server, Docker); ABP drives the Router (status, models, usage, chat through its local route) and uses its `/v1` as the provider `octopus-router`; the Router's UI shows in ABP's pane. Found and fixed: a down Docker daemon flooded the Router's view with the raw `docker info` document. |
| OT-C | **done** 2026-09-29 | `LoopyLuci/abp-octopus-connector` + 29 connector repos, all private, with topics; 1,053 operations; every pipeline passes; a live Budget call (health, `/api/build`, and a private route that correctly asks for sign-in). ABP registers them (area `octopus`, `modules.search_paths`) and pushes the session on sign-in and hub start. |
| OT-D (alfred-js) | **done** 2026-09-30 | `LoopyLuci/abp-octopus-alfred-js` (private): a runner connector (`kind` runner in `bot/octopus/connectors.py`) that installs and runs the upstream bot with ABP's session; its pipeline passes (28 commands; the live start needs the bot's token). Upstream's `package-lock.json` is out of sync with `package.json` (axios), so `npm ci` fails there: the connector falls back to `npm install --no-package-lock`. Reported, not fixed (the friend's repo). |
| OT-D (the rest), OT-E, OT-F | todo | |
| MK-A, MK-B | **done** 2026-09-30 | `9dd0ae5`, `dbf080d` (the desktop bundle ships `abp_modkit`), `5674b7f` (hand-kept ops files survive a refresh), `7b7bd5f` (a provider key a module stored itself). `tests/test_modkit.py`. |
| MK-C | **done** 2026-09-30 | 34 projects adopted; 73 modules registered, no manifest errors. New private repos for the ones that had none; module files pushed to the rest. History rewrites where a push needed one, each with a backup branch: ABABE (dumps out of its single commit), Android_APK_Workshop (binaries to LFS). Universal-Browser-Extension stays public. UMAG: a private `LoopyLuci/universalModelAgnosticGateway` beside its AQSG origin. |
| MK-D Cognitive Companion | **done** 2026-09-30 | `LoopyLuci/Cognitive-Companion` (private). New crate `cc-abp` (ABP's gateway as its `LlmProvider`, ABP's modules), `cc-cli abp ...`, a real MCP server. Live: ABP runs its memory, router and `abp.*` operations; it answered through ABP's models; ABP's agents used its MCP tools (memory round trip, `abp_modules`, `abp_ask`). |
| MK-D Kestrion | **done** 2026-09-30 | Kestrion `de5fa24b` (ADR-0099), ABP `ce25bbc` (Kestrion as a backend, ADR-0097). The connector links with an integration key, or by itself when ABP opens it; grants ABP backend access; the `abp` provider. As a module: its inference server as a managed service (health, hardware found the 7900 XTX with ROCm), CLI operations, its own MCP server (a real client listed and ran its tools). Not yet run: a whole desktop-app turn through ABP. |
| MK-D KotMoE | **done, not pushed** 2026-09-30 | `abp.rs`: ABP's gateway as the `abp` provider, ABP's modules; opened by ABP, KotMoE mints ABP a key to its API (port 8766) and stores it through its hub. Live: KotMoE stored its key; ABP listed its models, chatted repeatedly, queried RAG; a wrong key gets 401. Found and fixed two deadlocks in its API server (a held `Mutex` guard re-locked: the second chat, or one bad key, froze the API); that file is part of uncommitted work in progress, so the fix is in the working tree only. The push is refused: an earlier local commit adds a 231 MB weights file. |
| MK-E GAImer | **done** 2026-09-30 | GAImer `6d8f5dc`, pushed through its pre-push gate. On the 7900 XTX: HIP 7.2 with torch 2.9.1+rocm7.2.1 (Windows wheels need Python 3.12), 92 tests plus integration. The Windows ROCm build has no `torch.distributed`, so multi-GPU training needs Linux. |
| LX-A | **done** 2026-09-30 | On the NixOS VM on Server: 14/14 (the module's service, token generated into `/var/lib/abp` owned by the service user, token auth, restart keeps it, the CLI on PATH, tkinter). Not yet checked: the dashboard in a browser on NixOS's own desktop. |
| LX-B | **done** 2026-09-30 | 23/23 on Ubuntu 26.04 (Python 3.14) and Debian 13 (Python 3.13), ABP `7362362`: installer, server, token auth, CLI, TUI, dashboard in a browser, the whole test suite (3256 passed), the service installer, update, a rolled-back bad update, uninstall. The runs found and fixed: `install.sh` not executable in git; `abp-linux.sh` clobbering `NAME` from os-release; tests that fell back to the checkout's real `data/bot.db` (missing on a fresh clone); `abp mcp list` failing without Claude Desktop; uninstall leaving its folder. SSH Toolkit's tests skip without PowerShell (`pwsh` is not installed by the installer). |
| LX-C | todo | |
| MH-A | **done** 2026-09-30 | `ceb09f6`: overlays (`catalog/`, `data/module-overlays/`, `modules.overlay_dirs`), `adopt --overlay`, `check --project`; CMake detection. `c98fedf`: modules' own Python environments (`project.python_setup`, in the module's data folder). |
| MH-B | **done** 2026-09-30 | `47ec447`: `add_from_url` (API, `module_add` tool, `abp modules add`, the Modules page), tested against a real GitHub repo end to end. |
| MH-C…E | todo | Refresh with a diff of operations; assisted setup through Studio; a shared overlay catalog repo. |
| CV-A | **done** 2026-09-30 | `c98fedf`: 14 OpenCV overlays, each passing `abp_modkit check`; checkouts in `E:/Projects/OpenCV` untouched. |
| CV-B, CV-C | **done** 2026-09-30 | `04ffb2d` (+ `47ec447`): `bot/vision`, the vision tools, the Vision page, `/api/vision`, `abp vision`, MCP. Real models on OpenCV's own images: faces, objects (dog, bicycle, truck), OCR of UI text near-perfect after word splitting, QR, people. OpenCV 5 findings: its new DNN engine ignores GPU targets, the wheel's OpenCL kernels fail on AMD, Caffe is gone (WeChat QR runs without its CNN files). |
| CV-D | **part** | Done: text and templates on the screen with `screen_center`; Studio's screenshot diff. Next: the screen-element model (AM "screen"). |
| CV-E…G | todo | Benchmarks per device, COOL-Benchmark's multi-cloud layer (in a private copy of COOL), OpenCV built here with a working GPU path, ONNX conformance. |
| GEN-0, GEN-2 | **done** 2026-09-30 | `a9310ab`: Studio. Live with three free OpenRouter models: two valid variants, a rate limit passed over to the next model, one unusable answer refused. |
| GEN-D | **part** | Datasets (`sft.jsonl`, `prefs.jsonl`) from the log. Next: Knowledge Modules from applied changes. |
| GEN-1 | todo | Needs AM-C; the rank model first, then the edit model (see docs/models/own-models.md). |
| GEN-3 | **part** | A variant is validated (HTML, `node --check`) before it can be applied. Next: the pipeline in a sandbox before apply. |
| AM-A, AM-B | **done** 2026-09-30 | docs/models/own-models.md. KotMoE's Kotlin core trains real MoE classifiers (MNIST 95.7%); its desktop app's chat answers with keyword-picked text, not a model. BrainBuilder trains real PyTorch models. |
| AM-C, AM-D | todo | The shared loop; "stack" and "screen" first. |
