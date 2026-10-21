# ABP capability index

Everything ABP can do, generated from the sources by `python scripts/gen_capabilities.py` (do not edit by hand). Local Chat's `/` palette, Settings search and the agent's `abp_capabilities` tool search this list plus the live agent tools and MCP servers. *net* = needs the internet; *cloud* = uses a cloud model or service; everything else works offline on this machine.

| Kind | Count |
|---|---|
| page | 59 |
| action | 102 |
| route | 1047 |
| module | 9 |
| setting | 72 |

## Pages

| Name | What | Where | |
|---|---|---|---|
| ABP Agents | Settings, safety, tools, skills | `#agents` |  |
| Automation | Hooks, agent settings & auto-manage | `#automation` |  |
| Bots | Manage bot instances | `#bots` |  |
| Browser | Link your real browser | `#browser` | net |
| Remote Bot Chat | Talk to any connected bot | `#chat` |  |
| Cluster | Share machines, run jobs | `#cluster` |  |
| Containers | Docker, images, stacks, templates | `#containers` |  |
| Control Center | Backends, models & security | `#control` |  |
| Coordination | Who is doing what | `#coordination` |  |
| Customize UI | Describe a change, preview, apply | `#customize` |  |
| Database | Storage & maintenance | `#database` |  |
| Diagnostics | Crash reports, telemetry & support bundle | `#diagnostics` |  |
| Evals | Agent eval results | `#evals` |  |
| Files |  | `#files` |  |
| Hermes Manager | Manage the Hermes agent | `#hermes-manager` | net cloud |
| Hosting | Websites, domains, DNS, deploys | `#hosting` | net cloud |
| Infra Automation | Rules for containers, VMs, Tailscale | `#infra-rules` | net |
| Jobs | Queue, history & backends | `#jobs` |  |
| Kanban | Per-bot task board | `#kanban` |  |
| Neural Lab | Design, train, system models | `#lab` |  |
| Agentic Workspace | Chat & code with local models | `#local-chat` |  |
| Local AI | Models, server, fine-tuning | `#localai` | net |
| Live Logs | Streaming tail | `#logs` |  |
| Mobile | Pair & manage devices | `#mobile` | net |
| Models | Providers, API keys, per-model toggles | `#models` | net cloud |
| Modules | Every program ABP drives | `#modules` |  |
| Local Models | Pull, load, create & cloud models | `#ollama` | net cloud |
| Overview | Live status & KPIs | `#overview` |  |
| Permissions & approvals | Approvals, rules, MCP pins | `#permissions` |  |
| Platforms | Legacy single-bot config | `#platforms` |  |
| Power | Keep awake & wake machines | `#power` |  |
| User profiles | What the agent learned about people | `#profiles` |  |
| Resilience | Hot reload & health checks | `#resilience` |  |
| Model Router | Decisions, learning, training & policy | `#router` |  |
| Routines | Saved tasks you can run & re-time | `#routines` |  |
| Server Chat | Message & send files between devices | `#server-chat` |  |
| Linked Servers | Manage other AgenticBotPlatform installs | `#servers` | net |
| Sessions | Past conversations | `#sessions` |  |
| Settings | Every setting, organized | `#settings` | cloud |
| Skills | Packs, quarantine and drafts | `#skills` |  |
| Sandbox | What ABP has running | `#sns` |  |
| SSH Toolkit | Manage SSH connections | `#ssh-toolkit` |  |
| Storage | File server, parity, backups | `#storage` |  |
| Studio | Change the UI, see it live | `#studio` |  |
| Support Bot |  | `#support` |  |
| Support Bot | Local assistant for server management | `#support-bot` |  |
| Swarms | Multi-bot orchestration | `#swarms` |  |
| Tailscale | Tailnet, Serve & Funnel, devices, ACLs | `#tailscale` | net |
| Connections & Telemetry | Health, latency & errors | `#telemetry` |  |
| Traces | What each agent run did | `#traces` |  |
| Training | Support Bot phrases & bot instructions | `#training` |  |
| TransferDaemon | Encrypted messages & files | `#transferdaemon` | net |
| Model Studio | Local models, fine-tuning & export | `#unsloth` |  |
| Updates | Check & install the latest version | `#updates` | net |
| Usage & limits | Model use, allowances & blocks | `#usage` |  |
| Vault | Logins the browser agent can use | `#vault` | net |
| Vision | Objects, faces, text, screen | `#vision` |  |
| VM-Harness | Virtual machines & containers | `#vm-harness` |  |
| Virtual Machines | QEMU, Hyper-V, libvirt | `#vms` |  |

## Settings

| Name | What | Where | |
|---|---|---|---|
| Settings: Advanced | Raw configuration, its history and backups, the environment, logs. | `settings/advanced` |  |
| Config history (Advanced) | Every config change and a way back | `settings/advanced` |  |
| Connections & Telemetry (Advanced) | Health, latency and errors | `settings/advanced` |  |
| Environment (Advanced) | The .env file ABP reads | `settings/advanced` |  |
| Jobs (Advanced) | Queue, history and backends | `settings/advanced` |  |
| Live Logs (Advanced) | Streaming tail | `settings/advanced` |  |
| Sessions (Advanced) | Past conversations | `settings/advanced` |  |
| Snapshots (Advanced) | Save and restore the whole configuration | `settings/advanced` |  |
| Settings: Agent | How ABP's own agent behaves: permissions, approvals, tools, skills, MCP servers, hooks. | `settings/agent` |  |
| ABP Agents (Agent) | Agent settings, safety, tools, skills and sub-agents | `settings/agent` |  |
| Automation (Agent) | Hooks, agent settings and auto-manage | `settings/agent` |  |
| MCP servers (Agent) | The MCP servers ABP runs and controlling ABP over MCP | `settings/agent` | net |
| Permissions & approvals (Agent) | Approval rules, modes, MCP pins, taint | `settings/agent` |  |
| Routines (Agent) | Saved tasks you can run and re-time | `settings/agent` |  |
| Skills (Agent) | Skill packs, quarantine and drafts | `settings/agent` |  |
| User profiles (Agent) | What the agent learned about people | `settings/agent` |  |
| Settings: Appearance & shortcuts | How ABP looks on this device and its keyboard shortcuts. | `settings/appearance` |  |
| Appearance (Appearance & shortcuts) | Theme, accent, scale, density, icon (this device only) | `settings/appearance` |  |
| Customize UI (Appearance & shortcuts) | Describe a change, preview, apply | `settings/appearance` |  |
| Keyboard shortcuts (Appearance & shortcuts) | Every shortcut, and changing them | `settings/appearance` |  |
| Studio (Appearance & shortcuts) | Change the UI, see it live | `settings/appearance` |  |
| Settings: Backends | The cloud and local backends ABP routes work through: Claude API, Claude Code CLI, Claude Desktop, Hermes. Agentic Workspace needs none of them. | `settings/backends` |  |
| Control Center (Backends) | Default backend per action, backend models, security and process controls | `settings/backends` |  |
| Hermes Manager (Backends) | The Hermes agent: gateway, sessions, config, backups | `settings/backends` | net cloud |
| Model Router (Backends) | Automatic routing between models, its learning and policy | `settings/backends` |  |
| Models & providers (Backends) | Providers, API keys and per-model toggles | `settings/backends` | net cloud |
| Run setup again (Backends) | The backend setup wizard: API keys, CLI paths, the Hermes gateway | `settings/backends` | cloud |
| Settings: Cloud & capacity | When cloud models help: the capacity governor (cloud-led / hybrid / local-only), budgets and usage. | `settings/cloud` | net cloud |
| Capacity governor (Cloud & capacity) | The current mode, pinning it by hand, probes and handoff packs | `settings/cloud` | net cloud |
| Usage & limits (Cloud & capacity) | Model use, allowances, blocks and resets | `settings/cloud` | net cloud |
| Settings: Devices & linked servers | Phones, other ABP installs, remote machines and networks. | `settings/devices` | net |
| Browser (Devices & linked servers) | Link your real browser | `settings/devices` | net |
| Linked Servers (Devices & linked servers) | Other AgenticBotPlatform installs | `settings/devices` | net |
| Mobile (Devices & linked servers) | Pair and manage devices | `settings/devices` | net |
| Power (Devices & linked servers) | Keep awake and wake machines | `settings/devices` | net |
| SSH Toolkit (Devices & linked servers) | SSH connections | `settings/devices` | net |
| Server Chat (Devices & linked servers) | Messages and files between devices | `settings/devices` | net |
| Tailscale (Devices & linked servers) | Tailnet, Serve and Funnel | `settings/devices` | net |
| TransferDaemon (Devices & linked servers) | Encrypted messages and files | `settings/devices` | net |
| Settings: Factory | The conductor (planner, confined workers, reviewer, push queue), swarms and evaluation. | `settings/factory` |  |
| Conductor & push queue (Factory) | Integration branches waiting for approval; nothing pushes without it | `settings/factory` | net |
| Evals (Factory) | Agent eval results | `settings/factory` |  |
| Kanban (Factory) | Per-bot task boards | `settings/factory` |  |
| Swarms (Factory) | Multi-bot orchestration | `settings/factory` |  |
| Traces (Factory) | What each agent run did | `settings/factory` |  |
| Settings: Local models | ModelMistress (the local model hub), Agentic Workspace's defaults and the other local runtimes. | `settings/local-models` |  |
| Agentic Workspace defaults (Local models) | Default model, agent type, autonomy, cloud assist and worktrees for new sessions (below) | `settings/local-models` |  |
| Local AI (Local models) | Local models, server and fine-tuning | `settings/local-models` |  |
| Local Models (Local models) | Pull, load and create models through a connected Ollama (opt-in) | `settings/local-models` |  |
| Model Studio (Local models) | Local models, fine-tuning and export through a connected Unsloth (opt-in) | `settings/local-models` |  |
| Modules (ModelMistress) (Local models) | The hub: install, update, start, its models and settings | `settings/local-models` |  |
| Neural Lab (Local models) | Design and train models | `settings/local-models` |  |
| Settings: Platforms & bots | Chat platforms and the bots on them. | `settings/platforms` |  |
| Bots (Platforms & bots) | Bot instances, their platforms and agents | `settings/platforms` |  |
| Platforms (Platforms & bots) | Legacy single-bot config | `settings/platforms` |  |
| Remote Bot Chat (Platforms & bots) | Talk to any connected bot (Chat with Bot) | `settings/platforms` |  |
| Support Bot (Platforms & bots) | The local assistant for server management | `settings/platforms` |  |
| Training (Platforms & bots) | Support Bot phrases and bot instructions | `settings/platforms` |  |
| Settings: Safety | Confinement, what is running, the emergency stops, credentials. | `settings/safety` |  |
| Allowlisted users (Safety) | Who may talk to the bots | `settings/safety` |  |
| Diagnostics (Safety) | Crash reports, processes, support bundle | `settings/safety` |  |
| Emergency stop (Safety) | Engage or release the agent emergency stop | `settings/safety` |  |
| Sandbox (Safety) | What ABP has running, cells and limits | `settings/safety` |  |
| Vault (Safety) | Logins the browser agent may use | `settings/safety` |  |
| Settings: Storage | ABP's database, retention and the file server. | `settings/storage` |  |
| Database (Storage) | Storage and maintenance | `settings/storage` |  |
| Files (Storage) | Browse the server's files | `settings/storage` |  |
| Retention (Storage) | How long records are kept, auto-vacuum | `settings/storage` |  |
| Storage (Storage) | File server, parity, backups | `settings/storage` |  |
| Settings: Updates | New versions and keeping ABP healthy. | `settings/updates` | net |
| Resilience (Updates) | Hot reload and health checks | `settings/updates` | net |
| Updates (Updates) | Check for and install the latest version | `settings/updates` | net |

## Actions (command palette, shortcuts)

| Name | What | Where | |
|---|---|---|---|
| Clear the Activity list (the log itself is untouched) | Activity action | `action:activity.clear` |  |
| Copy this activity entry | Activity action | `action:activity.copy` |  |
| Agent settings for this bot | Bots action | `action:bot.agent` |  |
| Chat with this bot | Bots action | `action:bot.chat` |  |
| Delete this bot | Bots action | `action:bot.delete` |  |
| Edit this bot | Bots action | `action:bot.edit` |  |
| Restart this bot | Bots action | `action:bot.restart` |  |
| Start or stop this bot | Bots action | `action:bot.toggle` |  |
| Delete this bot's chat history | Chat action | `action:chat.clear` |  |
| Copy this conversation as text | Chat action | `action:chat.copy` |  |
| Jump to the message box | Chat action | `action:chat.focus` |  |
| Next bot in this chat | Chat action | `action:chat.instance.next` |  |
| Previous bot in this chat | Chat action | `action:chat.instance.prev` |  |
| Copy this message | Chat action | `action:chat.message.copy` |  |
| Delete this message | Chat action | `action:chat.message.delete` |  |
| Quote this message into the box | Chat action | `action:chat.message.quote` |  |
| Ask the bot again with this message | Chat action | `action:chat.message.retry` |  |
| Switch between Chat with Bot and Send from Server | Chat action | `action:chat.mode` |  |
| Start a new session with this bot | Chat action | `action:chat.new` |  |
| Send the message | Chat action | `action:chat.send` |  |
| Re-read the config from disk | General action | `action:config.reload` |  |
| Copy the selected text | General action | `action:copy.selection` |  |
| Engage or release the agent emergency stop | Processes action | `action:estop.agent` | net |
| Emergency stop: kill every cell | Processes action | `action:estop.emergency` |  |
| Copy this file's path | Files action | `action:file.copyPath` |  |
| Download this file | Files action | `action:file.download` |  |
| Re-read this folder | Files action | `action:files.refresh` |  |
| Copy this card's text | Kanban action | `action:kanban.card.copy` |  |
| Delete this card | Kanban action | `action:kanban.card.delete` |  |
| Refresh the board | Kanban action | `action:kanban.refresh` |  |
| Jump to the Agentic Workspace message box | Agentic Workspace action | `action:localchat.focus` |  |
| New Agentic Workspace session | Agentic Workspace action | `action:localchat.new` |  |
| Copy the whole log view | Logs action | `action:logs.copyAll` |  |
| Copy this log line | Logs action | `action:logs.copyLine` |  |
| Run this module's action (install, update, hub) | Modules action | `action:module.act` |  |
| Open this module's page | Modules action | `action:module.open` |  |
| Go to Modules and reload the list | Modules action | `action:modules.refresh` |  |
| Go to ABP Agents | Go to action (shortcut g ag) | `action:nav.agents` |  |
| Go to Automation | Go to action (shortcut g au) | `action:nav.automation` |  |
| Go to Bots | Go to action (shortcut g b) | `action:nav.bots` |  |
| Go to Browser | Go to action (shortcut g br) | `action:nav.browser` | net |
| Go to Remote Bot Chat | Go to action (shortcut g h) | `action:nav.chat` |  |
| Go to Cluster | Go to action (shortcut g cl) | `action:nav.cluster` |  |
| Go to Containers | Go to action (shortcut g co) | `action:nav.containers` |  |
| Go to Control Center | Go to action (shortcut g c) | `action:nav.control` |  |
| Go to Customize UI | Go to action (shortcut g cu) | `action:nav.customize` |  |
| Go to Database | Go to action (shortcut g db) | `action:nav.database` |  |
| Go to Diagnostics | Go to action (shortcut g dg) | `action:nav.diagnostics` |  |
| Go to Files | Go to action (shortcut g fi) | `action:nav.files` |  |
| Go to Hermes Manager | Go to action (shortcut g hm) | `action:nav.hermes-manager` | net cloud |
| Go to Hosting | Go to action (shortcut g ho) | `action:nav.hosting` | net |
| Go to Infra Automation | Go to action (shortcut g ir) | `action:nav.infra-rules` |  |
| Go to Jobs | Go to action (shortcut g j) | `action:nav.jobs` |  |
| Go to Kanban | Go to action (shortcut g kb) | `action:nav.kanban` |  |
| Go to Neural Lab | Go to action (shortcut g lb) | `action:nav.lab` |  |
| Go to Agentic Workspace | Go to action (shortcut g lc) | `action:nav.local-chat` |  |
| Go to Local AI | Go to action (shortcut g lo) | `action:nav.localai` |  |
| Go to Live Logs | Go to action (shortcut g l) | `action:nav.logs` |  |
| Go to Mobile | Go to action (shortcut g mb) | `action:nav.mobile` | net |
| Go to Models | Go to action (shortcut g m) | `action:nav.models` |  |
| Go to Modules | Go to action (shortcut g mo) | `action:nav.modules` |  |
| Go to Local Models | Go to action (shortcut g ol) | `action:nav.ollama` |  |
| Go to Overview | Go to action (shortcut g o) | `action:nav.overview` |  |
| Go to Platforms | Go to action (shortcut g pf) | `action:nav.platforms` |  |
| Go to Power | Go to action (shortcut g pw) | `action:nav.power` |  |
| Go to Resilience | Go to action (shortcut g re) | `action:nav.resilience` |  |
| Go to Model Router | Go to action (shortcut g ro) | `action:nav.router` |  |
| Go to Routines | Go to action (shortcut g rt) | `action:nav.routines` |  |
| Go to Server Chat | Go to action (shortcut g sc) | `action:nav.server-chat` |  |
| Go to Linked Servers | Go to action (shortcut g sv) | `action:nav.servers` | net |
| Go to Sessions | Go to action (shortcut g se) | `action:nav.sessions` |  |
| Go to Settings | Go to action (shortcut g gs) | `action:nav.settings` |  |
| Go to SSH Toolkit | Go to action (shortcut g sk) | `action:nav.ssh-toolkit` |  |
| Go to Storage | Go to action (shortcut g st) | `action:nav.storage` |  |
| Go to Studio | Go to action (shortcut g sd) | `action:nav.studio` |  |
| Go to Support Bot | Go to action (shortcut g sp) | `action:nav.support` |  |
| Go to Swarms | Go to action (shortcut g sw) | `action:nav.swarms` |  |
| Go to Tailscale | Go to action (shortcut g ts) | `action:nav.tailscale` | net |
| Go to Connections & Telemetry | Go to action (shortcut g te) | `action:nav.telemetry` |  |
| Go to Training | Go to action (shortcut g tr) | `action:nav.training` |  |
| Go to TransferDaemon | Go to action (shortcut g td) | `action:nav.transferdaemon` | net |
| Go to Model Studio | Go to action (shortcut g un) | `action:nav.unsloth` |  |
| Go to Updates | Go to action (shortcut g up) | `action:nav.updates` | net |
| Go to Vision | Go to action (shortcut g vi) | `action:nav.vision` |  |
| Go to VM-Harness | Go to action (shortcut g vm) | `action:nav.vm-harness` |  |
| Go to Virtual Machines | Go to action (shortcut g vs) | `action:nav.vms` |  |
| Kill this cell | Processes action | `action:process.kill` |  |
| Open the Processes panel | Processes action | `action:processes.open` |  |
| Go to Routines and reload the list | Routines action | `action:routine.refresh` |  |
| Run this routine now | Routines action | `action:routine.run` |  |
| Copy this message | Chat action | `action:serverchat.message.copy` |  |
| Delete this message | Chat action | `action:serverchat.message.delete` |  |
| Send from the server | Chat action | `action:serverchat.send` |  |
| Export this session as JSON | Sessions action | `action:sessions.export` |  |
| Send this to the Support Bot | Chat action | `action:support.send` |  |
| Keyboard shortcuts | General action | `action:ui.help` |  |
| Command palette | General action | `action:ui.palette` |  |
| Refresh everything on screen | General action | `action:ui.refresh` |  |
| Search this screen | General action | `action:ui.search` |  |
| Collapse or expand the sidebar | General action | `action:ui.sidebar` |  |
| Show or hide Terminal and Activity | General action | `action:ui.terminal` |  |
| Switch between light and dark | General action | `action:ui.theme` |  |

## Modules

| Name | What | Where | |
|---|---|---|---|
| BrainBuilder | Building, training and running neural networks, visually or from a description. | `#modules` |  |
| CacheIt | Tiered caching (RAM, NVMe, SSD) in front of storage, with write-back and a WAL. | `#modules` |  |
| Continuum | Screen sharing and full remote control of devices, for people and agents. | `#modules` |  |
| Hermes-Manager | Hermes's gateway, logs, config and backups, with its own window. | `#modules` | net cloud |
| ModelMistress | Loading and hosting models: an OpenAI-compatible server with a router and backends. | `#modules` |  |
| TransferDaemon | End-to-end encrypted messages and files between devices; window, terminal UI, relays. | `#modules` | net |
| TridentDroid | Android emulation: devices on KVM or Windows' hypervisor, driven over gRPC. | `#modules` |  |
| VM-Harness | Virtual machines and containers on every hypervisor, with its own window. | `#modules` |  |
| Wrightspace | Building applications for every platform and language, with agents; was WebBuilder. | `#modules` |  |

## API routes

### - (4)

- `GET /` Index
- `GET /desktop-ui/` Desktop Ui Index
- `GET /healthz` Healthz
- `GET /metrics` Metrics

### activity (1)

- `GET /api/activity` Api Activity

### agent (17)

- `GET /api/agent/config` Get Config
- `GET /api/agent/config/schema` Config Schema
- `GET /api/agent/evals/reports` List Eval Reports
- `GET /api/agent/evals/reports/{name}` Get Eval Report
- `GET /api/agent/overview` Overview
- `GET /api/agent/permissions` Get Permissions
- `GET /api/agent/router/recommend` Router Recommend
- `GET /api/agent/sessions/{session_key}/export` Export Session
- `GET /api/agent/taint` Get Taint
- `GET /api/agent/tools` Tool Inventory
- `GET /api/agent/traces` List Traces
- `GET /api/agent/traces/{run_id}` Trace Detail
- `POST /api/agent/ask` Api Agent Ask
- `POST /api/agent/config` Set Config
- `POST /api/agent/config/reset` Reset Config
- `POST /api/agent/permissions/validate` Validate Permissions
- `POST /api/agent/taint/clear` Clear Taint

### agent-settings (2)

- `GET /api/agent-settings` Api Agent Settings Get
- `POST /api/agent-settings` Api Agent Settings Set

### android (6)

- `GET /api/android/apk/download/{push_id}` Api Android Apk Download
- `GET /api/android/apk/pending` Api Android Apk Pending
- `GET /api/android/apk/status` Api Android Apk Status
- `POST /api/android/apk/mesh/redeem` Api Android Apk Mesh Redeem
- `POST /api/android/apk/send` Api Android Apk Send
- `POST /api/android/apk/send-all` Api Android Apk Send All

### approvals (3)

- `GET /api/approvals` List Approvals
- `GET /api/approvals/{approval_id}` Get Approval
- `POST /api/approvals/{approval_id}/resolve` Resolve Approval

### auto-manage (2)

- `GET /api/auto-manage/{instance_id}` Api Auto Manage Get
- `POST /api/auto-manage/{instance_id}` Api Auto Manage Set

### backend (1)

- `POST /api/backend/{action_or_default}/{backend}` Api Set Backend

### bluebubbles (1)

- `POST /webhooks/bluebubbles` Bluebubbles Webhook

### bots (22)

- `DELETE /api/bots/{instance_id}` Api Bots Delete
- `DELETE /api/bots/{instance_id}/schedules/{sched_id}` Api Bots Schedules Delete
- `GET /api/bots` Api Bots List
- `GET /api/bots/backups` Api Bots Backups
- `GET /api/bots/{instance_id}` Api Bots Get
- `GET /api/bots/{instance_id}/model-picker` Api Bots Model Picker
- `GET /api/bots/{instance_id}/profile` Api Bots Profile
- `GET /api/bots/{instance_id}/schedules` Api Bots Schedules List
- `POST /api/bots` Api Bots Create
- `POST /api/bots/backups/{name}/restore` Api Bots Restore
- `POST /api/bots/{instance_id}/circuit/reset` Api Bots Circuit Reset
- `POST /api/bots/{instance_id}/disable` Api Bots Disable
- `POST /api/bots/{instance_id}/enable` Api Bots Enable
- `POST /api/bots/{instance_id}/model` Api Bots Set Model
- `POST /api/bots/{instance_id}/restart` Api Bots Restart
- `POST /api/bots/{instance_id}/schedules` Api Bots Schedules Create
- `POST /api/bots/{instance_id}/schedules/{sched_id}/pause` Api Bots Schedules Pause
- `POST /api/bots/{instance_id}/schedules/{sched_id}/resume` Api Bots Schedules Resume
- `POST /api/bots/{instance_id}/session/new` Api Bots New Session
- `POST /api/bots/{instance_id}/start` Api Bots Start
- `POST /api/bots/{instance_id}/stop` Api Bots Stop
- `PUT /api/bots/{instance_id}` Api Bots Update

### browser (25)

- `DELETE /api/browser/browsers/{key_id}` Browser Unpair
- `DELETE /api/browser/pair/code` Browser Pair Code Cancel
- `GET /api/browser/classify` Browser Classify
- `GET /api/browser/hello` Browser Hello
- `GET /api/browser/models/catalog` Models Catalog
- `GET /api/browser/pair/pending` Browser Pair Pending
- `GET /api/browser/policy` Browser Policy Get
- `GET /api/browser/status` Browser Status
- `GET /api/browser/v1/models` V1 Models
- `GET /api/browser/v1/{ns}/models` V1 Models Ns
- `GET /api/browser/web/adapters` Web Adapters
- `POST /api/browser/gateway/register` Gateway Register
- `POST /api/browser/models/{model_id}/load` Models Load
- `POST /api/browser/pair/code` Browser Pair Code
- `POST /api/browser/pair/collect` Browser Pair Collect
- `POST /api/browser/pair/complete` Browser Pair Complete
- `POST /api/browser/pair/request` Browser Pair Request
- `POST /api/browser/pair/{request_id}/{decision}` Browser Pair Decide
- `POST /api/browser/rpc` Browser Rpc
- `POST /api/browser/v1/audio/speech` V1 Speech
- `POST /api/browser/v1/audio/transcriptions` V1 Transcriptions
- `POST /api/browser/v1/chat/completions` V1 Chat
- `POST /api/browser/v1/embeddings` V1 Embeddings
- `POST /api/browser/v1/{ns}/chat/completions` V1 Chat Ns
- `POST /api/browser/web/{adapter}/selftest` Web Selftest

### canvas (2)

- `GET /api/canvas` List Canvases
- `POST /api/canvas/{name}/link` Canvas Link

### capabilities (2)

- `GET /api/capabilities` Capabilities Search
- `GET /api/capabilities/stats` Capabilities Stats

### capacity (13)

- `GET /api/capacity/events` Capacity Events
- `GET /api/capacity/handoff` Capacity Handoffs
- `GET /api/capacity/handoff/{task_id}` Capacity Handoff
- `GET /api/capacity/history` Capacity History
- `GET /api/capacity/policy` Capacity Policy
- `GET /api/capacity/probe` Capacity Probe State
- `GET /api/capacity/route` Capacity Route
- `GET /api/capacity/state` Capacity State
- `POST /api/capacity/clear` Capacity Clear
- `POST /api/capacity/handoff/prune` Capacity Handoff Prune
- `POST /api/capacity/override` Capacity Override
- `POST /api/capacity/probe` Capacity Probe Now
- `POST /api/capacity/report` Capacity Report

### chat (10)

- `DELETE /api/chat/messages` Api Chat Messages Delete
- `DELETE /api/chat/messages/{message_id}` Api Chat Message Delete One
- `GET /api/chat/attachments/{message_id}` Api Chat Attachment
- `GET /api/chat/attachments/{message_id}/thumbnail` Api Chat Attachment Thumbnail
- `GET /api/chat/messages` Api Chat Messages
- `GET /api/chat/messages/export` Api Chat Messages Export
- `GET /api/chat/recipients` Api Chat Recipients
- `POST /api/chat/send` Api Chat Send
- `POST /api/chat/send-file` Api Chat Send File
- `POST /api/chat/send-to-bot` Api Chat Send To Bot

### cicd (10)

- `GET /api/cicd/chain` Cicd Chain
- `GET /api/cicd/decisions` Cicd Decisions
- `GET /api/cicd/events` Cicd Events
- `GET /api/cicd/events/stream` Cicd Stream
- `GET /api/cicd/runs` Cicd Runs
- `GET /api/cicd/runs/{run_id}` Cicd Run
- `GET /api/cicd/runs/{run_id}/explain` Cicd Explain
- `GET /api/cicd/steps/stats` Cicd Step Stats
- `GET /api/cicd/summary` Cicd Summary
- `GET /api/cicd/workers` Cicd Workers

### cluster (26)

- `GET /api/cluster` Cluster Overview
- `GET /api/cluster/blobs` Cluster Blobs
- `GET /api/cluster/blobs/{h}` Cluster Blob
- `GET /api/cluster/blobs/{h}/chunks/{index}` Cluster Blob Chunk
- `GET /api/cluster/groups` Cluster Groups
- `GET /api/cluster/groups/{gid}` Cluster Group Get
- `GET /api/cluster/jobs` Cluster Jobs
- `GET /api/cluster/jobs/{pid}` Cluster Job
- `GET /api/cluster/jobs/{pid}/logs` Cluster Job Logs
- `GET /api/cluster/node` Cluster Node
- `GET /api/cluster/offer` Cluster Offer
- `GET /api/cluster/runs` Cluster Runs
- `GET /api/cluster/runs/{rid}` Cluster Run
- `GET /api/cluster/runs/{rid}/files/{name}` Cluster Run File
- `GET /api/cluster/runs/{rid}/logs` Cluster Run Logs
- `POST /api/cluster/candidates` Cluster Candidates
- `POST /api/cluster/groups` Cluster Group
- `POST /api/cluster/jobs` Cluster Submit
- `POST /api/cluster/jobs/{pid}/cancel` Cluster Job Cancel
- `POST /api/cluster/jobs/{pid}/collect` Cluster Job Collect
- `POST /api/cluster/refresh` Cluster Refresh
- `POST /api/cluster/runs` Cluster Run Accept
- `POST /api/cluster/runs/{rid}/cancel` Cluster Run Cancel
- `POST /api/cluster/runs/{rid}/files/{name}/send` Cluster Run Send File
- `POST /api/cluster/runs/{rid}/start` Cluster Run Start
- `PUT /api/cluster/offer` Cluster Offer Set

### codeintel (10)

- `GET /api/codeintel/callers` Codeintel Callers
- `GET /api/codeintel/find` Codeintel Find
- `GET /api/codeintel/outline` Codeintel Outline
- `GET /api/codeintel/read` Codeintel Read
- `GET /api/codeintel/refs` Codeintel Refs
- `GET /api/codeintel/stats` Codeintel Stats
- `GET /api/codeintel/tests` Codeintel Tests
- `POST /api/codeintel/impact` Codeintel Impact
- `POST /api/codeintel/index` Codeintel Index
- `POST /api/codeintel/pack` Codeintel Pack

### conductor (12)

- `GET /api/conductor/escalations` Conductor Escalations
- `GET /api/conductor/plans` Conductor Plans
- `GET /api/conductor/plans/{plan_id}` Conductor Plan Get
- `GET /api/conductor/queue` Conductor Queue
- `GET /api/conductor/queue/{queue_id}/audit` Conductor Queue Audit
- `GET /api/conductor/reviews` Conductor Reviews
- `GET /api/conductor/status` Conductor Status
- `POST /api/conductor/plan` Conductor Plan
- `POST /api/conductor/queue/{queue_id}/approve` Conductor Approve
- `POST /api/conductor/queue/{queue_id}/push` Conductor Push
- `POST /api/conductor/queue/{queue_id}/reject` Conductor Reject
- `POST /api/conductor/review` Conductor Review

### config (7)

- `GET /api/config` Api Config
- `GET /api/config/backups` Api Config Backups Files
- `GET /api/config/backups/{name}` Api Config Backups Versions
- `GET /api/config/backups/{name}/{version}` Api Config Backups Read
- `POST /api/config/backups/{name}/{version}/restore` Api Config Backups Restore
- `POST /api/config/reload` Api Config Reload
- `POST /api/config/set` Api Config Set

### context (4)

- `DELETE /api/context/{name}` Api Context Delete
- `GET /api/context` Api Context List
- `GET /api/context/{name}` Api Context Get
- `POST /api/context/{name}` Api Context Set

### context-vault (2)

- `GET /api/context-vault/items` Cv Items
- `GET /api/context-vault/status` Cv Status

### coordination (3)

- `GET /api/coordination/locks` Coordination Locks
- `GET /api/coordination/summary` Coordination Summary
- `GET /api/coordination/tasks` Coordination Tasks

### database (2)

- `GET /api/database` Api Database
- `POST /api/database/vacuum` Api Vacuum

### delegation-activity (1)

- `GET /api/delegation-activity` Api Delegation Activity

### desktop (3)

- `POST /api/desktop/restart` Api Desktop Restart
- `POST /api/desktop/start` Api Desktop Start
- `POST /api/desktop/stop` Api Desktop Stop

### devices (1)

- `GET /api/devices` Api Devices

### diagnostics (4)

- `GET /api/diagnostics/bundle` Api Diagnostics Bundle
- `GET /api/diagnostics/crash-reports` Api Diagnostics Crash Reports
- `GET /api/diagnostics/crash-reports/{report_id}` Api Diagnostics Crash Report Detail
- `GET /api/diagnostics/summary` Api Diagnostics Summary

### docker (52)

- `DELETE /api/docker/git-stacks/{name}` Gs Remove
- `DELETE /api/docker/networks/{name}` Dk Net Rm
- `DELETE /api/docker/volumes/{name}` Dk Vol Rm
- `GET /api/docker/containers` Handler
- `GET /api/docker/containers/{ident}` Dk Container
- `GET /api/docker/containers/{ident}/files` Dk Files
- `GET /api/docker/containers/{ident}/logs` Dk Logs
- `GET /api/docker/containers/{ident}/stats` Dk Cstats
- `GET /api/docker/containers/{ident}/top` Dk Top
- `GET /api/docker/df` Handler
- `GET /api/docker/events` Handler
- `GET /api/docker/git-stacks` Gs List
- `GET /api/docker/git-stacks/events` Gs Events
- `GET /api/docker/git-stacks/{name}` Gs Get
- `GET /api/docker/images` Handler
- `GET /api/docker/images/history` Dk Image History
- `GET /api/docker/images/inspect` Dk Image
- `GET /api/docker/images/search` Dk Search
- `GET /api/docker/info` Handler
- `GET /api/docker/networks` Handler
- `GET /api/docker/networks/{name}` Dk Net
- `GET /api/docker/registries` Handler
- `GET /api/docker/stacks` Handler
- `GET /api/docker/stacks/{name}` Dk Stack Get
- `GET /api/docker/stacks/{name}/logs` Dk Stack Logs
- `GET /api/docker/stacks/{name}/services` Dk Stack Services
- `GET /api/docker/stats` Handler
- `GET /api/docker/templates` Handler
- `GET /api/docker/volumes` Handler
- `GET /api/docker/volumes/{name}` Dk Vol
- `PATCH /api/docker/git-stacks/{name}` Gs Update
- `POST /api/docker/containers` Dk Create
- `POST /api/docker/containers/{ident}/action` Dk Action
- `POST /api/docker/containers/{ident}/commit` Dk Commit
- `POST /api/docker/containers/{ident}/copy` Dk Copy
- `POST /api/docker/containers/{ident}/exec` Dk Exec
- `POST /api/docker/containers/{ident}/rename` Dk Rename
- `POST /api/docker/containers/{ident}/update` Dk Update
- `POST /api/docker/git-stacks` Gs Add
- `POST /api/docker/git-stacks/poller/run` Gs Poll
- `POST /api/docker/git-stacks/{name}/check` Gs Check
- `POST /api/docker/git-stacks/{name}/deploy` Gs Deploy
- `POST /api/docker/images/{op}` Dk Image Op
- `POST /api/docker/networks` Dk Net Create
- `POST /api/docker/networks/{name}/connect` Dk Net Connect
- `POST /api/docker/prune` Dk Prune
- `POST /api/docker/registries/login` Dk Login
- `POST /api/docker/registries/logout` Dk Logout
- `POST /api/docker/stacks` Dk Stack Deploy
- `POST /api/docker/stacks/{name}/action` Dk Stack Action
- `POST /api/docker/templates/{template_id}/deploy` Dk Template
- `POST /api/docker/volumes` Dk Vol Create

### editors (2)

- `GET /api/editors/status` Editors Status
- `POST /api/editors/vscode/install` Editors Install Vscode

### env (6)

- `GET /api/env` Api Env
- `GET /api/env/backups` Api Env Backups
- `GET /api/env/content` Api Env Content
- `POST /api/env/backups/{name}/restore` Api Env Restore
- `POST /api/env/content` Api Env Content Save
- `POST /api/env/set` Api Env Set

### estop (2)

- `GET /api/estop` Api Estop Get
- `POST /api/estop` Api Estop Set

### export (2)

- `GET /api/export/tables` Api Export Tables
- `GET /api/export/{table}` Api Export Table

### extensions (1)

- `GET /api/extensions` Extensions List

### files (5)

- `DELETE /api/files/{root}` Api Files Remove Root
- `GET /api/files` Api Files Roots
- `GET /api/files/{root}` Api Files List
- `GET /api/files/{root}/download` Api Files Download
- `POST /api/files` Api Files Add Root

### fileserver (52)

- `DELETE /api/fileserver/backups/{name}` Fs Backup Rm
- `DELETE /api/fileserver/links/{token}` Fs Link Rm
- `DELETE /api/fileserver/pools/{name}` Fs Pool Rm
- `DELETE /api/fileserver/remotes/{name}` Fs Remote Rm
- `DELETE /api/fileserver/shares/{name}` Fs Share Rm
- `DELETE /api/fileserver/transfers/{name}` Fs Transfer Rm
- `DELETE /api/fileserver/users/{name}` Fs User Rm
- `GET /api/fileserver` Fs Overview
- `GET /api/fileserver/apps` Fs Apps
- `GET /api/fileserver/array` Fs Array
- `GET /api/fileserver/backups` Fs Backups
- `GET /api/fileserver/backups/{name}/snapshots` Fs Snaps
- `GET /api/fileserver/disks` Fs Disks
- `GET /api/fileserver/duplicates` Fs Dups
- `GET /api/fileserver/events` Fs Events
- `GET /api/fileserver/guard` Fs Guard
- `GET /api/fileserver/index` Fs Index
- `GET /api/fileserver/links` Fs Links
- `GET /api/fileserver/pools` Fs Pools
- `GET /api/fileserver/remotes` Fs Remotes
- `GET /api/fileserver/runs/{rid}` Fs Run
- `GET /api/fileserver/search` Fs Search
- `GET /api/fileserver/settings` Fs Settings
- `GET /api/fileserver/shares` Fs Shares
- `GET /api/fileserver/shares/{name}/nfs` Fs Nfs
- `GET /api/fileserver/shares/{name}/smb` Fs Smb
- `GET /api/fileserver/stats` Fs Stats
- `GET /api/fileserver/tiering` Fs Tiering Status
- `GET /api/fileserver/tiering/index` Fs Tiering Index
- `GET /api/fileserver/tiering/scan` Fs Tiering Scan
- `GET /api/fileserver/transfers` Fs Transfers
- `GET /api/fileserver/users` Fs Users
- `PATCH /api/fileserver/shares/{name}` Fs Share Edit
- `POST /api/fileserver/apps` Fs App Install
- `POST /api/fileserver/array/{action}` Fs Array Action
- `POST /api/fileserver/backups/{name}/{action}` Fs Backup Action
- `POST /api/fileserver/guard/unfreeze` Fs Unfreeze
- `POST /api/fileserver/index` Fs Index Run
- `POST /api/fileserver/mover` Fs Mover
- `POST /api/fileserver/server/{action}` Fs Server
- `POST /api/fileserver/shares` Fs Share Add
- `POST /api/fileserver/tiering/apply` Fs Tiering Apply
- `POST /api/fileserver/tiering/recall` Fs Tiering Recall
- `POST /api/fileserver/transfers/{name}/run` Fs Transfer Run
- `POST /api/fileserver/users` Fs User Set
- `PUT /api/fileserver/array` Fs Array Set
- `PUT /api/fileserver/backups/{name}` Fs Backup Set
- `PUT /api/fileserver/guard` Fs Guard Set
- `PUT /api/fileserver/pools/{name}` Fs Pool Set
- `PUT /api/fileserver/remotes/{name}` Fs Remote Set
- `PUT /api/fileserver/settings` Fs Settings Put
- `PUT /api/fileserver/transfers/{name}` Fs Transfer Set

### googlechat (1)

- `POST /webhooks/googlechat` Googlechat Webhook

### gpu (1)

- `GET /api/gpu` Api Gpu

### hermes (14)

- `GET /api/hermes/gateway` Api Hermes Gateway Overview
- `GET /api/hermes/gateway/list` Api Hermes Gateway List
- `GET /api/hermes/gateway/logs` Api Hermes Gateway Logs
- `GET /api/hermes/gateway/status` Api Hermes Gateway Status
- `GET /api/hermes/swarm-tools-status` Api Hermes Swarm Tools Status
- `GET /api/hermes/{instance_id}/agent-config` Api Hermes Agent Config Get
- `GET /api/hermes/{instance_id}/delegation` Api Hermes Delegation Get
- `POST /api/hermes/ask` Api Hermes Ask
- `POST /api/hermes/gateway/{action}` Api Hermes Gateway Action
- `POST /api/hermes/{instance_id}/agent-config` Api Hermes Agent Config Set
- `POST /api/hermes/{instance_id}/delegation` Api Hermes Delegation Set
- `POST /api/hermes/{instance_id}/disable-swarm-tools` Api Hermes Disable Swarm Tools
- `POST /api/hermes/{instance_id}/dispatch` Api Hermes Dispatch
- `POST /api/hermes/{instance_id}/enable-swarm-tools` Api Hermes Enable Swarm Tools

### hermes-manager (10)

- `GET /api/hermes-manager/jobs` Hm Jobs
- `GET /api/hermes-manager/operations` Hm Operations
- `GET /api/hermes-manager/status` Hm Status
- `POST /api/hermes-manager/bridge/start` Hm Start
- `POST /api/hermes-manager/bridge/stop` Hm Stop
- `POST /api/hermes-manager/call` Hm Call
- `POST /api/hermes-manager/mcp` Hm Mcp
- `POST /api/hermes-manager/setup` Hm Setup
- `POST /api/hermes-manager/update` Hm Update
- `POST /api/hermes-manager/window` Hm Window

### hooks (5)

- `DELETE /api/hooks/{hook_id}` Api Hooks Delete
- `GET /api/hooks` Api Hooks List
- `POST /api/hooks` Api Hooks Add
- `POST /api/hooks/{hook_id}/disable` Api Hooks Disable
- `POST /api/hooks/{hook_id}/enable` Api Hooks Enable

### hosting (44)

- `DELETE /api/hosting/accounts/{acc_id}` H Account Rm
- `DELETE /api/hosting/dns/{acc_id}/records` H Record Rm
- `DELETE /api/hosting/servers/{acc_id}/{server_id}` H Srv Destroy
- `DELETE /api/hosting/sites/{sid}` H Site Rm
- `DELETE /api/hosting/upnp` H Upnp Rm
- `GET /api/hosting` H Overview
- `GET /api/hosting/accounts` H Accounts
- `GET /api/hosting/certs` H Certs
- `GET /api/hosting/dns/{acc_id}/records` H Records
- `GET /api/hosting/dns/{acc_id}/zones` H Zones
- `GET /api/hosting/edge` H Edge
- `GET /api/hosting/edge/log` H Edge Log
- `GET /api/hosting/network` H Network
- `GET /api/hosting/providers` H Providers
- `GET /api/hosting/resolve` H Resolve
- `GET /api/hosting/runs` H Runs
- `GET /api/hosting/runs/{rid}` H Run
- `GET /api/hosting/servers/{acc_id}` H Servers
- `GET /api/hosting/servers/{acc_id}/options` H Srv Options
- `GET /api/hosting/settings` H Settings
- `GET /api/hosting/sites` H Sites
- `GET /api/hosting/sites/{sid}` H Site
- `GET /api/hosting/sites/{sid}/plan` H Plan
- `GET /api/hosting/tunnels` H Tunnels
- `GET /api/hosting/upnp` H Upnp
- `PATCH /api/hosting/accounts/{acc_id}` H Account Edit
- `PATCH /api/hosting/sites/{sid}` H Site Edit
- `POST /api/hosting/accounts` H Account Add
- `POST /api/hosting/accounts/{acc_id}/verify` H Account Verify
- `POST /api/hosting/certs` H Cert
- `POST /api/hosting/edge/start` H Edge Start
- `POST /api/hosting/edge/stop` H Edge Stop
- `POST /api/hosting/servers/{acc_id}` H Srv Create
- `POST /api/hosting/servers/{acc_id}/setup` H Srv Setup
- `POST /api/hosting/sites` H Site Add
- `POST /api/hosting/sites/{sid}/check` H Check
- `POST /api/hosting/sites/{sid}/go-live` H Go Live
- `POST /api/hosting/sites/{sid}/publish` H Publish
- `POST /api/hosting/tunnels/cloudflare/run` H Cf Run
- `POST /api/hosting/tunnels/cloudflare/stop` H Cf Stop
- `POST /api/hosting/tunnels/cloudflared/install` H Cfd Install
- `POST /api/hosting/upnp` H Upnp Add
- `PUT /api/hosting/dns/{acc_id}/records` H Record Set
- `PUT /api/hosting/settings` H Settings Put

### hotreload (2)

- `GET /api/hotreload/status` Api Hotreload Status
- `POST /api/hotreload/run` Api Hotreload Run

### index.html (1)

- `GET /desktop-ui/index.html` Desktop Ui Index

### infra (9)

- `DELETE /api/infra/rules/{rule_id}` Rules Delete
- `GET /api/infra/hosts` Infra Hosts
- `GET /api/infra/peer-access` Infra Peer Access Get
- `GET /api/infra/rules` Rules List
- `GET /api/infra/rules/{rule_id}/history` Rules History
- `POST /api/infra/peer-access` Infra Peer Access Set
- `POST /api/infra/rules` Rules Create
- `POST /api/infra/rules/{rule_id}/enable` Rules Enable
- `POST /api/infra/rules/{rule_id}/run` Rules Run

### instances (2)

- `GET /api/instances/{instance_id}/permissions` Get Instance Permissions
- `PUT /api/instances/{instance_id}/permissions` Put Instance Permissions

### integrations (5)

- `DELETE /api/integrations/keys/{key_id}` Integrations Revoke
- `GET /api/integrations` Integrations Overview
- `GET /api/integrations/whoami` Integrations Whoami
- `POST /api/integrations/keys` Integrations Mint
- `PUT /api/integrations/framing` Integrations Framing

### jobs (5)

- `GET /api/jobs` Api Jobs
- `GET /api/jobs/by-backend` Api Jobs By Backend
- `GET /api/jobs/timeseries` Api Jobs Timeseries
- `GET /api/jobs/{job_id}/children` Api Job Children
- `GET /api/jobs/{job_id}/tool-events` Api Job Tool Events

### kanban (6)

- `DELETE /api/kanban/cards/{card_id}` Api Kanban Delete Card
- `GET /api/kanban/boards` Api Kanban Boards
- `GET /api/kanban/cards` Api Kanban Cards
- `GET /api/kanban/work-board` Api Kanban Work Board
- `POST /api/kanban/cards` Api Kanban Add Card
- `POST /api/kanban/cards/{card_id}/move` Api Kanban Move Card

### lab (21)

- `GET /api/lab` Lab Overview
- `GET /api/lab/brainbuilder` Lab Bb
- `GET /api/lab/designs` Lab Designs
- `GET /api/lab/designs/{name}` Lab Design
- `GET /api/lab/importers` Lab Importers
- `GET /api/lab/projects` Lab Projects
- `GET /api/lab/runs` Lab Runs
- `GET /api/lab/runs/{rid}` Lab Run
- `GET /api/lab/systune` Lab Systune
- `GET /api/lab/systune/advice` Lab Advice
- `GET /api/lab/telemetry` Lab Telemetry
- `GET /api/lab/telemetry/hw` Lab Hw
- `POST /api/lab/autotune` Lab Autotune
- `POST /api/lab/export/brainbuilder` Lab Export Bb
- `POST /api/lab/import` Lab Import
- `POST /api/lab/runs` Lab Start
- `POST /api/lab/runs/{rid}/stop` Lab Stop
- `POST /api/lab/systune/bench` Lab Bench
- `POST /api/lab/systune/train` Lab Systune Train
- `POST /api/lab/validate` Lab Validate
- `PUT /api/lab/designs/{name}` Lab Design Put

### lease (3)

- `GET /api/lease` Api Lease Status
- `POST /api/lease/release` Api Lease Release
- `POST /api/lease/take` Api Lease Take

### local-chat (42)

- `DELETE /api/local-chat/groups/{gid}` Local Chat Group Delete
- `DELETE /api/local-chat/sessions/{sid}` Local Chat Delete
- `DELETE /api/local-chat/sessions/{sid}/queued/{qid}` Local Chat Queued Drop
- `GET /api/local-chat/events` Local Chat Events
- `GET /api/local-chat/groups` Local Chat Groups
- `GET /api/local-chat/models` Local Chat Models
- `GET /api/local-chat/sessions` Local Chat Sessions
- `GET /api/local-chat/sessions/{sid}` Local Chat Session
- `GET /api/local-chat/sessions/{sid}/checkpoints` Local Chat Checkpoints
- `GET /api/local-chat/sessions/{sid}/children` Local Chat Children
- `GET /api/local-chat/sessions/{sid}/context` Local Chat Context
- `GET /api/local-chat/sessions/{sid}/export` Local Chat Export
- `GET /api/local-chat/sessions/{sid}/factory` Local Chat Factory Board
- `GET /api/local-chat/sessions/{sid}/queued` Local Chat Queued
- `GET /api/local-chat/sessions/{sid}/worktree` Local Chat Worktree
- `GET /api/local-chat/sessions/{sid}/worktree/commit-message` Local Chat Worktree Draft
- `GET /api/local-chat/sessions/{sid}/worktree/diff` Local Chat Worktree Diff
- `GET /api/local-chat/status` Local Chat Status
- `PATCH /api/local-chat/groups/{gid}` Local Chat Group Rename
- `PATCH /api/local-chat/sessions/{sid}` Local Chat Update
- `POST /api/local-chat/groups` Local Chat Group Create
- `POST /api/local-chat/models/benchmark` Local Chat Benchmark
- `POST /api/local-chat/sessions` Local Chat Create
- `POST /api/local-chat/sessions/{sid}/cancel` Local Chat Cancel
- `POST /api/local-chat/sessions/{sid}/chapters` Local Chat Chapter
- `POST /api/local-chat/sessions/{sid}/compact` Local Chat Compact
- `POST /api/local-chat/sessions/{sid}/factory/run` Local Chat Factory Run
- `POST /api/local-chat/sessions/{sid}/factory/stop` Local Chat Factory Stop
- `POST /api/local-chat/sessions/{sid}/fork` Local Chat Fork
- `POST /api/local-chat/sessions/{sid}/handoff` Local Chat Handoff
- `POST /api/local-chat/sessions/{sid}/read` Local Chat Read
- `POST /api/local-chat/sessions/{sid}/rewind` Local Chat Rewind
- `POST /api/local-chat/sessions/{sid}/send` Local Chat Send
- `POST /api/local-chat/sessions/{sid}/steer` Local Chat Steer
- `POST /api/local-chat/sessions/{sid}/task` Local Chat Task
- `POST /api/local-chat/sessions/{sid}/worktree` Local Chat Worktree Create
- `POST /api/local-chat/sessions/{sid}/worktree/commit` Local Chat Worktree Commit
- `POST /api/local-chat/sessions/{sid}/worktree/discard` Local Chat Worktree Discard
- `POST /api/local-chat/sessions/{sid}/worktree/keep` Local Chat Worktree Keep
- `POST /api/local-chat/sessions/{sid}/worktree/queue-merge` Local Chat Worktree Queue
- `POST /api/local-chat/sessions/{sid}/worktree/sync` Local Chat Worktree Sync
- `POST /api/local-chat/worktrees/prune` Local Chat Worktrees Prune

### localai (26)

- `DELETE /api/localai/models` Ai Delete
- `GET /api/localai` Ai Overview
- `GET /api/localai/discover` Ai Discover
- `GET /api/localai/mesh` Ai Mesh
- `GET /api/localai/models` Ai Models
- `GET /api/localai/models/show` Ai Show
- `GET /api/localai/runs/{rid}` Ai Run
- `GET /api/localai/settings` Ai Settings
- `GET /api/localai/stores` Ai Stores
- `GET /api/localai/train` Ai Train
- `GET /api/localai/train/{rid}` Ai Train Status
- `POST /api/localai/discover/adopt` Ai Adopt
- `POST /api/localai/discover/adopt-all` Ai Adopt All
- `POST /api/localai/engine/install` Ai Engine Install
- `POST /api/localai/models/copy` Ai Copy
- `POST /api/localai/models/create` Ai Create
- `POST /api/localai/models/import` Ai Import
- `POST /api/localai/models/pull` Ai Pull
- `POST /api/localai/server/{action}` Ai Server
- `POST /api/localai/train` Ai Train Start
- `POST /api/localai/train/setup` Ai Train Setup
- `POST /api/localai/train/{rid}/amethyst` Ai Train Amethyst
- `POST /api/localai/train/{rid}/export` Ai Train Export
- `POST /api/localai/train/{rid}/stop` Ai Train Stop
- `PUT /api/localai/settings` Ai Settings Put
- `PUT /api/localai/stores` Ai Stores Put

### logs (1)

- `GET /api/logs` Api Logs

### manager (4)

- `GET /api/manager/checkin` Manager Checkin State
- `GET /api/manager/digest` Manager Digest
- `GET /api/manager/escalations` Manager Escalations
- `POST /api/manager/checkin` Manager Checkin Run

### mcp (7)

- `GET /api/mcp` Api Mcp
- `GET /api/mcp/pins` Get Pins
- `GET /api/mcp/{name}/logs` Api Mcp Logs
- `POST /api/mcp/pins/approve` Approve Pin
- `POST /api/mcp/self-register` Api Mcp Self Register
- `POST /api/mcp/{name}/disable` Api Mcp Disable
- `POST /api/mcp/{name}/enable` Api Mcp Enable

### mcp-external (6)

- `DELETE /api/mcp-external/{name}` Api Mcp External Delete
- `GET /api/mcp-external` Api Mcp External List
- `GET /api/mcp-external/oauth/callback` Api Mcp External Oauth Callback
- `POST /api/mcp-external` Api Mcp External Add
- `POST /api/mcp-external/{name}/disable` Api Mcp External Disable
- `POST /api/mcp-external/{name}/enable` Api Mcp External Enable

### memory (29)

- `DELETE /api/memory/entries/{entry_id}` Mem Forget
- `DELETE /api/memory/goals/{goal_id}` Mem Goal Delete
- `DELETE /api/memory/sources/{source_id}` Mem Source Remove
- `DELETE /api/memory/tool-rules/{rule_id}` Mem Tool Rule Delete
- `GET /api/memory` Mem Overview
- `GET /api/memory/context` Mem Context
- `GET /api/memory/diff` Mem Diff
- `GET /api/memory/entries` Mem Entries
- `GET /api/memory/goals` Mem Goals
- `GET /api/memory/search` Mem Search
- `GET /api/memory/sources` Mem Sources
- `GET /api/memory/threads` Mem Threads
- `GET /api/memory/threads/{thread}` Mem Thread
- `GET /api/memory/tool-rules` Mem Tool Rules
- `GET /api/memory/tree/stats` Mem Tree Stats
- `GET /api/memory/vault` Mem Vault
- `PATCH /api/memory/sources/{source_id}` Mem Source Update
- `POST /api/memory/diff/checkpoint` Mem Checkpoint
- `POST /api/memory/entries` Mem Add
- `POST /api/memory/entries/{entry_id}/{action}` Mem Review
- `POST /api/memory/goals` Mem Goal Put
- `POST /api/memory/sources` Mem Source Add
- `POST /api/memory/sources/{source_id}/sync` Mem Source Sync
- `POST /api/memory/threads/{thread}` Mem Thread Add
- `POST /api/memory/tool-rules` Mem Tool Rule Put
- `POST /api/memory/tree` Mem Tree
- `POST /api/memory/tree/ingest` Mem Tree Ingest
- `POST /api/memory/vault/sync` Mem Vault Sync
- `PUT /api/memory/settings` Mem Settings

### mobile-keys (7)

- `DELETE /api/mobile-keys/{key_id}` Api Mobile Keys Revoke
- `GET /api/mobile-keys` Api Mobile Keys List
- `POST /api/mobile-keys` Api Mobile Keys Create
- `POST /api/mobile-keys/purge-revoked` Api Mobile Keys Purge Revoked
- `POST /api/mobile-keys/{key_id}/revoke-by-device` Api Mobile Keys Revoke By Device
- `POST /api/mobile-keys/{key_id}/tier` Api Mobile Keys Set Tier
- `PUT /api/mobile-keys/{key_id}` Api Mobile Keys Update

### modelmistress (7)

- `GET /api/modelmistress/hub` Modelmistress Hub
- `GET /api/modelmistress/jobs` Modelmistress Jobs
- `GET /api/modelmistress/models` Modelmistress Models
- `GET /api/modelmistress/status` Modelmistress Status
- `POST /api/modelmistress/load` Modelmistress Load
- `POST /api/modelmistress/pull` Modelmistress Pull
- `POST /api/modelmistress/unload` Modelmistress Unload

### models (8)

- `DELETE /api/models/limits` Clear Limits
- `GET /api/models` Api Models
- `GET /api/models/find` Find Models
- `GET /api/models/info` Model Info
- `GET /api/models/usage` Model Usage
- `GET /api/models/usage-limits` Model Usage Limits
- `POST /api/models/refresh` Refresh Catalog
- `PUT /api/models/limits` Set Limits

### modules (31)

- `GET /api/modules` Modules List
- `GET /api/modules/candidates` Modules Candidates
- `GET /api/modules/catalog` Modules Catalog
- `GET /api/modules/jobs/{job_id}` Modules Job
- `GET /api/modules/{mid}` Module Status
- `GET /api/modules/{mid}/assist` Module Assist List
- `GET /api/modules/{mid}/jobs` Module Jobs
- `GET /api/modules/{mid}/operations` Module Operations
- `GET /api/modules/{mid}/panel` Module Panel Layout
- `GET /api/modules/{mid}/refresh` Module Refresh Preview
- `POST /api/modules/add` Modules Add
- `POST /api/modules/adopt` Modules Adopt
- `POST /api/modules/catalog/install` Modules Catalog Install
- `POST /api/modules/catalog/publish` Modules Catalog Publish
- `POST /api/modules/catalog/sync` Modules Catalog Sync
- `POST /api/modules/{mid}/assist` Module Assist Generate
- `POST /api/modules/{mid}/assist/keep` Module Assist Keep
- `POST /api/modules/{mid}/build` Module Build
- `POST /api/modules/{mid}/call` Module Call
- `POST /api/modules/{mid}/conformance` Module Conformance
- `POST /api/modules/{mid}/forget` Module Forget
- `POST /api/modules/{mid}/gui` Module Gui
- `POST /api/modules/{mid}/hub/start` Module Hub Start
- `POST /api/modules/{mid}/hub/stop` Module Hub Stop
- `POST /api/modules/{mid}/mcp` Module Mcp
- `POST /api/modules/{mid}/pipeline` Module Pipeline
- `POST /api/modules/{mid}/publish` Module Publish
- `POST /api/modules/{mid}/refresh` Module Refresh Apply
- `POST /api/modules/{mid}/setup` Module Setup
- `POST /api/modules/{mid}/tui` Module Tui
- `POST /api/modules/{mid}/update` Module Update

### native-agent (1)

- `POST /api/native-agent/{instance_id}/dispatch` Api Native Agent Dispatch

### network-info (1)

- `GET /api/network-info` Api Network Info

### nodes (5)

- `GET /api/nodes` Node List
- `GET /api/nodes/poll` Node Poll
- `POST /api/nodes/register` Node Register
- `POST /api/nodes/result` Node Result
- `PUT /api/nodes/{device_id}/consent` Node Consent

### ollama (17)

- `GET /api/ollama/gguf-files` Ollama Gguf Files
- `GET /api/ollama/jobs` Ollama Jobs
- `GET /api/ollama/models` Ollama Models
- `GET /api/ollama/operations` Ollama Operations
- `GET /api/ollama/recommendations` Ollama Recommendations
- `GET /api/ollama/show` Ollama Show
- `GET /api/ollama/status` Ollama Status
- `POST /api/ollama/call` Ollama Call
- `POST /api/ollama/copy` Ollama Copy
- `POST /api/ollama/create` Ollama Create
- `POST /api/ollama/delete` Ollama Delete
- `POST /api/ollama/import-gguf` Ollama Import
- `POST /api/ollama/load` Ollama Load
- `POST /api/ollama/move-models` Ollama Move
- `POST /api/ollama/pull` Ollama Pull
- `POST /api/ollama/push` Ollama Push
- `POST /api/ollama/unload` Ollama Unload

### overview (1)

- `GET /api/overview` Api Overview

### pairing (3)

- `GET /api/pairing` Api Pairing List
- `POST /api/pairing/{pairing_id}/approve` Api Pairing Approve
- `POST /api/pairing/{pairing_id}/deny` Api Pairing Deny

### peers (16)

- `DELETE /api/peers/{peer_id}` Api Peers Unlink
- `GET /api/peers` Api Peers List
- `GET /api/peers/control` Api Peers Control
- `GET /api/peers/firewall-status` Api Peers Firewall Status
- `GET /api/peers/routes` Api Peers Routes
- `GET /api/peers/self-address` Api Peers Self Address
- `GET /api/peers/transport` Api Peers Transport
- `GET /api/peers/{peer_id}/bots` Api Peers Bots
- `GET /api/peers/{peer_id}/overview` Api Peers Overview
- `POST /api/peers/firewall-open` Api Peers Firewall Open
- `POST /api/peers/handshake` Api Peers Handshake
- `POST /api/peers/link` Api Peers Link
- `POST /api/peers/pairing-token` Api Peers Pairing Token
- `POST /api/peers/{peer_id}/bots/{instance_id}/{action}` Api Peers Bot Action
- `POST /api/peers/{peer_id}/proxy` Api Peers Proxy
- `PUT /api/peers/control` Api Peers Control Set

### personas (1)

- `GET /api/personas` Api Personas

### platform-guides (1)

- `GET /api/platform-guides` Api Platform Guides

### platforms (2)

- `GET /api/platforms/status` Api Platforms Status
- `POST /api/platforms/apply` Api Platforms Apply

### plugins (6)

- `DELETE /api/plugins/{name}` Api Plugins Delete
- `GET /api/plugins` Api Plugins List
- `POST /api/plugins` Api Plugins Install
- `POST /api/plugins/create` Api Plugins Create
- `POST /api/plugins/{name}/disable` Api Plugins Disable
- `POST /api/plugins/{name}/enable` Api Plugins Enable

### power (7)

- `GET /api/power/info` Power Info
- `GET /api/power/status` Power Status
- `POST /api/power/hold` Power Hold
- `POST /api/power/learn/{peer}` Power Learn
- `POST /api/power/release` Power Release
- `POST /api/power/wake` Power Wake
- `PUT /api/power/settings` Power Settings

### privacy (2)

- `GET /api/privacy` Privacy Get
- `PUT /api/privacy` Privacy Put

### providers (10)

- `DELETE /api/providers/store/{name}` Api Providers Purge
- `DELETE /api/providers/{name}` Api Providers Delete
- `GET /api/providers` Api Providers List
- `GET /api/providers/catalog` Api Providers Catalog
- `GET /api/providers/store` Api Providers Store
- `GET /api/providers/{name}/models` Api Provider Models
- `POST /api/providers` Api Providers Set
- `POST /api/providers/store/{name}/restore` Api Providers Restore
- `POST /api/providers/{name}/models/toggle` Api Provider Model Toggle
- `POST /api/providers/{name}/models/toggle-paid` Api Provider Models Toggle Paid

### push (1)

- `POST /api/push/register` Api Push Register

### redblue (1)

- `GET /api/redblue/summary` Redblue Summary

### router (18)

- `DELETE /api/router/examples/{example_id}` Router Example Delete
- `GET /api/router/decisions` Router Decisions
- `GET /api/router/decisions/{decision_id}` Router Decision
- `GET /api/router/events` Router Events
- `GET /api/router/examples` Router Examples
- `GET /api/router/models` Router Models
- `GET /api/router/overview` Router Overview
- `GET /api/router/policy` Router Policy
- `GET /api/router/policy/history` Router Policy History
- `POST /api/router/decisions/{decision_id}/feedback` Router Feedback
- `POST /api/router/examples` Router Example Add
- `POST /api/router/models/forget` Router Model Forget
- `POST /api/router/models/release` Router Model Release
- `POST /api/router/models/rest` Router Model Rest
- `POST /api/router/policy/rollback` Router Policy Rollback
- `POST /api/router/reset` Router Reset
- `POST /api/router/simulate` Router Simulate
- `PUT /api/router/policy` Router Policy Save

### routines (8)

- `DELETE /api/routines/{routine_id}` Delete Routine
- `GET /api/routines` List Routines
- `GET /api/routines/{routine_id}` Get Routine
- `GET /api/routines/{routine_id}/history` Get Routine History
- `POST /api/routines/{routine_id}/pause` Pause Routine
- `POST /api/routines/{routine_id}/resume` Resume Routine
- `POST /api/routines/{routine_id}/run` Run Routine
- `PUT /api/routines/{routine_id}/schedule` Set Routine Schedule

### rules (10)

- `DELETE /api/rules/{rule_id}` Delete Rule
- `GET /api/rules` List Rules
- `GET /api/rules/status` Rules Status
- `GET /api/rules/{rule_id}` Get Rule
- `POST /api/rules` Create Rule
- `POST /api/rules/test` Test Rule
- `POST /api/rules/validate` Validate Rule
- `POST /api/rules/{rule_id}/enabled` Set Rule Enabled
- `POST /api/rules/{rule_id}/test` Test Saved Rule
- `PUT /api/rules/{rule_id}` Update Rule

### runtime (13)

- `DELETE /api/runtime/preview/{branch}` End Preview
- `GET /api/runtime` Runtime Status
- `GET /api/runtime/branches` List Branches
- `GET /api/runtime/events` Runtime Events
- `GET /api/runtime/plan` Runtime Plan
- `POST /api/runtime/branches` Fork Branch
- `POST /api/runtime/instances` Add Instance
- `POST /api/runtime/preview` Preview
- `POST /api/runtime/rollback` Rollback
- `POST /api/runtime/switch` Switch
- `POST /api/runtime/units/{unit}/load` Load Unit
- `POST /api/runtime/units/{unit}/swap` Swap Unit
- `POST /api/runtime/units/{unit}/unload` Unload Unit

### sandbox (7)

- `GET /api/sandbox/cells` Sandbox Cells
- `GET /api/sandbox/events` Sandbox Events
- `GET /api/sandbox/processes` Sandbox Processes
- `GET /api/sandbox/status` Sandbox Status
- `GET /api/sandbox/summary` Sandbox Summary
- `POST /api/sandbox/cells/{cell_id}/kill` Sandbox Kill Cell
- `POST /api/sandbox/estop` Sandbox Estop

### security (3)

- `DELETE /api/security/allowed-users/{telegram_id}` Api Remove Allowed User
- `GET /api/security/allowed-users` Api Allowed Users
- `POST /api/security/allowed-users/{telegram_id}` Api Add Allowed User

### sentinel (10)

- `GET /api/sentinel/backups` Sentinel Backups
- `GET /api/sentinel/cve` Sentinel Cve
- `GET /api/sentinel/issues` Sentinel Issues
- `GET /api/sentinel/journal` Sentinel Journal
- `GET /api/sentinel/status` Sentinel Status
- `POST /api/sentinel/backups/{name}/restore` Sentinel Restore
- `POST /api/sentinel/backups/{name}/verify` Sentinel Verify
- `POST /api/sentinel/cve/fix` Sentinel Cve Fix
- `POST /api/sentinel/issues/status` Sentinel Issue Status
- `POST /api/sentinel/run/{duty}` Sentinel Run

### server-chat (15)

- `DELETE /api/server-chat/conversations/{conversation_id}` Api Server Chat Clear
- `DELETE /api/server-chat/messages/{message_id}` Api Server Chat Delete Message
- `GET /api/server-chat/attachments/{message_id}` Api Server Chat Attachment
- `GET /api/server-chat/attachments/{message_id}/thumbnail` Api Server Chat Attachment Thumbnail
- `GET /api/server-chat/conversations` Api Server Chat Conversations
- `GET /api/server-chat/conversations/{conversation_id}/export` Api Server Chat Export
- `GET /api/server-chat/messages` Api Server Chat Messages
- `GET /api/server-chat/whoami` Api Server Chat Whoami
- `POST /api/server-chat/approvals/{approval_id}/resolve` Api Server Chat Approval Resolve
- `POST /api/server-chat/conversations` Api Server Chat Open
- `POST /api/server-chat/send` Api Server Chat Send
- `POST /api/server-chat/send-file` Api Server Chat Send File
- `POST /api/server-chat/uploads/init` Api Server Chat Uploads Init
- `POST /api/server-chat/uploads/{session_id}/complete` Api Server Chat Uploads Complete
- `PUT /api/server-chat/uploads/{session_id}/chunk/{index}` Api Server Chat Uploads Chunk

### sessions (5)

- `DELETE /api/sessions/{session_id}` Api Session Delete
- `GET /api/sessions` Api Sessions
- `GET /api/sessions/export` Api Sessions Export All
- `GET /api/sessions/{session_id}` Api Session Detail
- `GET /api/sessions/{session_id}/export` Api Session Export

### setup (4)

- `GET /api/setup/detect-desktop` Api Setup Detect Desktop
- `GET /api/setup/status` Api Setup Status
- `POST /api/setup/apply` Api Setup Apply
- `POST /api/setup/install-cli` Api Setup Install Cli

### shortcuts (4)

- `DELETE /api/shortcuts` Delete Shortcuts
- `GET /api/shortcuts` Get Shortcuts
- `POST /api/shortcuts/check` Check Shortcut
- `PUT /api/shortcuts` Put Shortcuts

### skills (13)

- `DELETE /api/skills/packs/{name}` Remove Pack
- `DELETE /api/skills/{name}` Api Skills Delete
- `GET /api/skills` Api Skills List
- `GET /api/skills/drafts` Get Drafts
- `GET /api/skills/drafts/{name}/evaluation` Evaluate Draft
- `GET /api/skills/packs` Get Packs
- `GET /api/skills/quarantine` Get Quarantine
- `POST /api/skills` Api Skills Create
- `POST /api/skills/drafts/approve` Approve Draft
- `POST /api/skills/drafts/reject` Reject Draft
- `POST /api/skills/fetch` Fetch Pack
- `POST /api/skills/quarantine/approve` Approve Pack
- `POST /api/skills/quarantine/reject` Reject Pack

### sms (1)

- `POST /webhooks/sms` Sms Webhook

### snapshots (4)

- `DELETE /api/snapshots/{name}` Api Snapshots Delete
- `GET /api/snapshots` Api Snapshots List
- `POST /api/snapshots` Api Snapshots Create
- `POST /api/snapshots/{name}/restore` Api Snapshots Restore

### ssh-toolkit (23)

- `DELETE /api/ssh-toolkit/connections/{name}` Api Ssh Toolkit Connection Remove
- `DELETE /api/ssh-toolkit/recordings/{recording_id}` Api Ssh Recording Delete
- `GET /api/ssh-toolkit/auto-update` Api Ssh Toolkit Auto Update Get
- `GET /api/ssh-toolkit/connections` Api Ssh Toolkit Connections
- `GET /api/ssh-toolkit/connections/{name}` Api Ssh Toolkit Connection Get
- `GET /api/ssh-toolkit/graph` Api Ssh Toolkit Graph
- `GET /api/ssh-toolkit/recordings` Api Ssh Recordings List
- `GET /api/ssh-toolkit/recordings/{recording_id}` Api Ssh Recording Get
- `GET /api/ssh-toolkit/session` Api Ssh Session List
- `GET /api/ssh-toolkit/status` Api Ssh Toolkit Status
- `GET /api/ssh-toolkit/status-all` Api Ssh Toolkit Status All
- `GET /api/ssh-toolkit/update/check` Api Ssh Toolkit Update Check
- `POST /api/ssh-toolkit/auto-update` Api Ssh Toolkit Auto Update Set
- `POST /api/ssh-toolkit/connections` Api Ssh Toolkit Connection Add
- `POST /api/ssh-toolkit/connections/{name}/run` Api Ssh Toolkit Connection Run
- `POST /api/ssh-toolkit/connections/{name}/test` Api Ssh Toolkit Connection Test
- `POST /api/ssh-toolkit/session/start` Api Ssh Session Start
- `POST /api/ssh-toolkit/session/{session_id}/record/pause` Api Ssh Session Record Pause
- `POST /api/ssh-toolkit/session/{session_id}/record/resume` Api Ssh Session Record Resume
- `POST /api/ssh-toolkit/session/{session_id}/record/start` Api Ssh Session Record Start
- `POST /api/ssh-toolkit/session/{session_id}/record/stop` Api Ssh Session Record Stop
- `POST /api/ssh-toolkit/session/{session_id}/stop` Api Ssh Session Stop
- `POST /api/ssh-toolkit/update/apply` Api Ssh Toolkit Update Apply

### studio (23)

- `DELETE /api/studio/variants/{vid}/file` Studio Reset
- `GET /api/studio` Studio Overview
- `GET /api/studio/jobs/{jid}` Studio Job
- `GET /api/studio/km` Studio Km Status
- `GET /api/studio/log` Studio Log
- `GET /api/studio/variants/{vid}` Studio Variant
- `GET /api/studio/variants/{vid}/file` Studio Read
- `GET /api/studio/variants/{vid}/tokens` Studio Tokens
- `GET /api/studio/variants/{vid}/version` Studio Version
- `POST /api/studio/datasets` Studio Datasets
- `POST /api/studio/generate` Studio Generate
- `POST /api/studio/km/build` Studio Km Build
- `POST /api/studio/km/export` Studio Km Export
- `POST /api/studio/revert` Studio Revert
- `POST /api/studio/variants` Studio Create
- `POST /api/studio/variants/{vid}/apply` Studio Apply
- `POST /api/studio/variants/{vid}/discard` Studio Discard
- `POST /api/studio/variants/{vid}/gate` Studio Gate
- `POST /api/studio/variants/{vid}/rate` Studio Rate
- `POST /api/studio/variants/{vid}/shot` Studio Shot
- `POST /api/studio/variants/{vid}/tokens` Studio Set Tokens
- `POST /api/studio/variants/{vid}/validate` Studio Validate
- `PUT /api/studio/variants/{vid}/file` Studio Write

### support-bot (20)

- `DELETE /api/support-bot/training/{phrase_id}` Api Support Bot Training Delete
- `GET /api/support-bot/health` Api Support Bot Health
- `GET /api/support-bot/manifest` Api Support Bot Manifest
- `GET /api/support-bot/misses` Api Support Bot Misses
- `GET /api/support-bot/model` Api Support Bot Model
- `GET /api/support-bot/pending` Api Support Bot Pending List
- `GET /api/support-bot/training` Api Support Bot Training List
- `POST /api/support-bot/ask` Api Support Bot Ask
- `POST /api/support-bot/classify` Api Support Bot Classify
- `POST /api/support-bot/confirm` Api Support Bot Confirm
- `POST /api/support-bot/generate` Api Support Bot Generate
- `POST /api/support-bot/generate/run` Api Support Bot Generate Run
- `POST /api/support-bot/misses/{classification_id}/label` Api Support Bot Misses Label
- `POST /api/support-bot/modules/{module_id}/enabled` Api Support Bot Module Set Enabled
- `POST /api/support-bot/modules/{module_id}/retrain` Api Support Bot Module Retrain
- `POST /api/support-bot/pending/{pending_id}/approve` Api Support Bot Pending Approve
- `POST /api/support-bot/pending/{pending_id}/reject` Api Support Bot Pending Reject
- `POST /api/support-bot/pending/{pending_id}/revert` Api Support Bot Pending Revert
- `POST /api/support-bot/retrain` Api Support Bot Retrain
- `POST /api/support-bot/training` Api Support Bot Training Add

### swarm-budget (2)

- `GET /api/swarm-budget` Api Get Swarm Budget
- `POST /api/swarm-budget` Api Set Swarm Budget

### swarms (11)

- `DELETE /api/swarms/{swarm_id}` Api Swarms Delete
- `GET /api/swarms` Api Swarms List
- `GET /api/swarms/runs` Api Swarm Runs List
- `GET /api/swarms/runs/{swarm_run_id}` Api Swarm Run Get
- `GET /api/swarms/{swarm_id}` Api Swarms Get
- `POST /api/swarms` Api Swarms Create
- `POST /api/swarms/runs/{swarm_run_id}/cancel` Api Swarm Run Cancel
- `POST /api/swarms/{swarm_id}/disable` Api Swarms Disable
- `POST /api/swarms/{swarm_id}/enable` Api Swarms Enable
- `POST /api/swarms/{swarm_id}/run` Api Swarms Run
- `PUT /api/swarms/{swarm_id}` Api Swarms Update

### tailscale (52)

- `DELETE /api/tailscale/api/devices/{device_id}` Ts Device Delete
- `DELETE /api/tailscale/api/keys/{key_id}` Ts Key Delete
- `GET /api/tailscale/accounts` Handler
- `GET /api/tailscale/api/acl` Ts Acl Get
- `GET /api/tailscale/api/devices` Ts Devices
- `GET /api/tailscale/api/devices/{device_id}` Ts Device
- `GET /api/tailscale/api/devices/{device_id}/routes` Ts Device Routes
- `GET /api/tailscale/api/dns` Ts Dns Get
- `GET /api/tailscale/api/keys` Ts Keys
- `GET /api/tailscale/api/settings` Ts Settings Get
- `GET /api/tailscale/api/users` Ts Users
- `GET /api/tailscale/api/webhooks` Ts Webhooks
- `GET /api/tailscale/app-connector-routes` Handler
- `GET /api/tailscale/dns-status` Handler
- `GET /api/tailscale/drive` Handler
- `GET /api/tailscale/exit-nodes` Handler
- `GET /api/tailscale/file-targets` Handler
- `GET /api/tailscale/ips` Handler
- `GET /api/tailscale/lock` Handler
- `GET /api/tailscale/metrics` Handler
- `GET /api/tailscale/netcheck` Handler
- `GET /api/tailscale/overview` Ts Overview
- `GET /api/tailscale/ping` Ts Ping
- `GET /api/tailscale/prefs` Handler
- `GET /api/tailscale/serve` Ts Serve Status
- `GET /api/tailscale/serve/config` Ts Serve Get Config
- `GET /api/tailscale/status` Handler
- `GET /api/tailscale/update-check` Handler
- `GET /api/tailscale/version` Handler
- `GET /api/tailscale/whois` Ts Whois
- `PATCH /api/tailscale/api/settings` Ts Settings Set
- `POST /api/tailscale/api/acl` Ts Acl Set
- `POST /api/tailscale/api/acl/validate` Ts Acl Validate
- `POST /api/tailscale/api/devices/{device_id}/{action}` Ts Device Action
- `POST /api/tailscale/api/dns` Ts Dns Set
- `POST /api/tailscale/api/keys` Ts Key Create
- `POST /api/tailscale/cert` Ts Cert
- `POST /api/tailscale/down` Ts Down
- `POST /api/tailscale/drive/share` Ts Drive Share
- `POST /api/tailscale/drive/unshare` Ts Drive Unshare
- `POST /api/tailscale/file/receive` Ts File Receive
- `POST /api/tailscale/file/send` Ts File Send
- `POST /api/tailscale/login` Ts Login
- `POST /api/tailscale/logout` Ts Logout
- `POST /api/tailscale/prefs` Ts Set Prefs
- `POST /api/tailscale/serve` Ts Serve Set
- `POST /api/tailscale/serve/config` Ts Serve Set Config
- `POST /api/tailscale/serve/off` Ts Serve Off
- `POST /api/tailscale/serve/reset` Ts Serve Reset
- `POST /api/tailscale/switch` Ts Switch
- `POST /api/tailscale/up` Ts Up
- `POST /api/tailscale/update` Ts Update

### teams (1)

- `POST /webhooks/teams` Teams Webhook

### telemetry (1)

- `GET /api/telemetry` Api Telemetry

### terminal (1)

- `POST /api/terminal/exec` Api Terminal Exec

### terminals (1)

- `GET /api/terminals` Term List

### transferdaemon (13)

- `GET /api/transferdaemon/audit` Td Audit
- `GET /api/transferdaemon/jobs` Td Jobs
- `GET /api/transferdaemon/operations` Td Operations
- `GET /api/transferdaemon/status` Td Status
- `POST /api/transferdaemon/call` Td Call
- `POST /api/transferdaemon/daemon/start` Td Start
- `POST /api/transferdaemon/daemon/stop` Td Stop
- `POST /api/transferdaemon/mcp` Td Mcp
- `POST /api/transferdaemon/send` Td Send
- `POST /api/transferdaemon/setup` Td Setup
- `POST /api/transferdaemon/tui` Td Tui
- `POST /api/transferdaemon/update` Td Update
- `POST /api/transferdaemon/window` Td Window

### turn (1)

- `GET /api/turn/credentials` Api Turn Credentials

### ui (1)

- `GET /api/ui/manifest` Ui Manifest

### ui-customize (4)

- `GET /api/ui-customize/history` Api Ui Customize History
- `POST /api/ui-customize/apply` Api Ui Customize Apply
- `POST /api/ui-customize/generate` Api Ui Customize Generate
- `POST /api/ui-customize/revert` Api Ui Customize Revert

### unsloth (23)

- `GET /api/unsloth/download-status` Unsloth Download Status
- `GET /api/unsloth/downloads` Unsloth Downloads
- `GET /api/unsloth/export/status` Unsloth Export Status
- `GET /api/unsloth/models` Unsloth Models
- `GET /api/unsloth/operations` Unsloth Operations
- `GET /api/unsloth/recommended` Unsloth Recommended
- `GET /api/unsloth/status` Unsloth Status
- `GET /api/unsloth/storage` Unsloth Storage
- `GET /api/unsloth/train/metrics` Unsloth Train Metrics
- `GET /api/unsloth/train/runs` Unsloth Train Runs
- `GET /api/unsloth/train/schema` Unsloth Train Schema
- `GET /api/unsloth/train/status` Unsloth Train Status
- `GET /api/unsloth/variants` Unsloth Variants
- `POST /api/unsloth/call` Unsloth Call
- `POST /api/unsloth/download` Unsloth Download
- `POST /api/unsloth/estimate` Unsloth Estimate
- `POST /api/unsloth/export` Unsloth Export
- `POST /api/unsloth/load` Unsloth Load
- `POST /api/unsloth/train/start` Unsloth Train Start
- `POST /api/unsloth/train/stop` Unsloth Train Stop
- `POST /api/unsloth/unload` Unsloth Unload
- `POST /api/unsloth/upload` Unsloth Upload
- `PUT /api/unsloth/storage` Unsloth Set Storage

### uploads (3)

- `POST /api/uploads/init` Api Uploads Init
- `POST /api/uploads/{session_id}/complete` Api Uploads Complete
- `PUT /api/uploads/{session_id}/chunk/{index}` Api Uploads Chunk

### user-model (8)

- `DELETE /api/user-model/items/{item_id}` Um Forget
- `DELETE /api/user-model/profile` Um Forget All
- `GET /api/user-model` Um Overview
- `GET /api/user-model/export` Um Export
- `GET /api/user-model/profile` Um Profile
- `POST /api/user-model/items/{item_id}/approve` Um Approve
- `POST /api/user-model/items/{item_id}/reject` Um Reject
- `PUT /api/user-model/items/{item_id}` Um Edit

### v (3)

- `GET /studio/v/{vid}/` Studio Preview
- `GET /studio/v/{vid}/components` Studio Components
- `GET /studio/v/{vid}/static/{path}` Studio Static

### validate-field (1)

- `POST /api/validate-field` Api Validate Field

### vault (3)

- `DELETE /api/vault/{name}` Vault Remove
- `GET /api/vault` Vault List
- `POST /api/vault` Vault Add

### vision (8)

- `GET /api/vision` Vision Status
- `GET /api/vision/out/{name}` Vision Out
- `POST /api/vision/analyze` Vision Analyze
- `POST /api/vision/compare` Vision Compare
- `POST /api/vision/edit` Vision Edit
- `POST /api/vision/faces` Vision Faces
- `POST /api/vision/find` Vision Find
- `POST /api/vision/models/{model}/fetch` Vision Fetch

### vm-harness (12)

- `GET /api/vm-harness/audit` Vmh Audit
- `GET /api/vm-harness/jobs` Vmh Jobs
- `GET /api/vm-harness/operations` Vmh Operations
- `GET /api/vm-harness/status` Vmh Status
- `GET /api/vm-harness/vms` Vmh Vms
- `POST /api/vm-harness/call` Vmh Call
- `POST /api/vm-harness/hub/start` Vmh Start
- `POST /api/vm-harness/hub/stop` Vmh Stop
- `POST /api/vm-harness/mcp` Vmh Mcp
- `POST /api/vm-harness/setup` Vmh Setup
- `POST /api/vm-harness/update` Vmh Update
- `POST /api/vm-harness/window` Vmh Window

### vms (14)

- `DELETE /api/vms/qemu/{name}` Vm Delete
- `GET /api/vms` Vm List
- `GET /api/vms/backends` Vm Backends
- `GET /api/vms/hyperv-switches` Hv Switches
- `GET /api/vms/hyperv/{name}/checkpoints` Hv Checkpoints
- `GET /api/vms/libvirt/{name}` Lv Info
- `GET /api/vms/paths` Vm Paths
- `GET /api/vms/qemu/{name}` Vm Get
- `POST /api/vms/disks/{op}` Vm Disk
- `POST /api/vms/hyperv` Hv Create
- `POST /api/vms/hyperv/{name}/{action}` Hv Action
- `POST /api/vms/libvirt/{name}/{action}` Lv Action
- `POST /api/vms/qemu` Vm Define
- `POST /api/vms/qemu/{name}/{action}` Vm Action

### wasm-plugins (7)

- `GET /api/wasm-plugins` Api Wasm Plugins List
- `GET /api/wasm-plugins/status` Api Wasm Plugins Status
- `POST /api/wasm-plugins/install` Api Wasm Plugins Install
- `POST /api/wasm-plugins/{name}/disable` Api Wasm Plugins Disable
- `POST /api/wasm-plugins/{name}/enable` Api Wasm Plugins Enable
- `POST /api/wasm-plugins/{name}/invoke` Api Wasm Plugins Invoke
- `POST /api/wasm-plugins/{name}/uninstall` Api Wasm Plugins Uninstall

### whatsapp (2)

- `GET /webhooks/whatsapp` Whatsapp Verify
- `POST /webhooks/whatsapp` Whatsapp Webhook

### {name} (4)

- `GET /canvas/{name}` Canvas Page
- `GET /canvas/{name}/version` Canvas Version
- `GET /canvas/{name}/view` Canvas View
- `GET /extensions/{name}/{path}` Extension File
