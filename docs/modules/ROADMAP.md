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
| M0 framework | todo | Also: a per-machine build cache dir (`CARGO_TARGET_DIR` and the like) on fast storage (E: on this PC), so builds don't fill Z:. |
| R BrainBuilder | **done** 2026-09-29 | `20787e4` committed the work in progress; PR #1 merged, `main` = `b196a4a`. The 7 `claude/*` branches are deleted (all already merged); the old worktrees and their unfinished patches are in `D:\Backups\Projects\BrainBuilder-worktrees-20260929`. Pipeline green. |
| R WebBuilder → Wrightspace | **done** 2026-09-29 | Server's work is committed and pushed (`cc92e40`). The GitHub repo is renamed `LoopyLuci/Wrightspace`. Server's folder is `C:\Projects\Wrightspace`, and the Z: copy is `Z:\Projects\Wrightspace`. The unrelated May 2026 codebase (IR round-trip, multi-framework emitters, agent-builder benchmark, marketplace) is kept as branch `legacy/may-2026`, to port from in WS-F/WS-H. Server's `main` still needs `--set-upstream-to=origin/main`. |
| R TridentDroid | **done** 2026-09-29 | Run logs untracked, pushed (`5d501a9`). The canonical folder is now `Z:\Projects\TridentDroid` (renamed from TridentDroidEmulator; the old stub is in the backups). |
| R ModelMistress | **done** 2026-09-29 | `git init`, README fixed, first push (`7aae7da`). |
| R Continuum | **done** 2026-09-29 | `main` did not build (security modules not declared); fixed and pushed (`86287aa`, 435 tests pass). Server and Z: are level. The Z: copy's unrelated older history is kept as local branch `z-local-v1.1`. |
| MM-A…G | todo | |
| CO-A…G | todo | |
| TD-A…F | todo | |
| BB-A…F | todo | |
| WS-A…H | todo | |
| F0, F1, F4a, F2, F3, F4b | todo | needs the new repo |
