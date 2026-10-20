# Local Chat

ABP's chat with local models on ModelMistress, with ABP's own agent (`bot/localchat`, `/api/local-chat`). It needs no cloud
backend and no network: with a model on the hub it works offline.

## How a turn runs

Every turn goes through one internal **"Local Chat" app instance** (platform `app`: no external chat platform), so tool
approvals, traces, the memory fabric, the e-stop and privacy mode work exactly as for every other bot. The session chooses
the model per turn (`router.ask(model_override="modelmistress/<id>")`) and the workspace (`cwd`). Modes:

| Mode | Tools |
|---|---|
| `chat` | none |
| `build` | the agent's tools; edits and commands ask as usual |
| `plan` | proposes a plan for approval first (the plan gate) |
| `review` | read-only tools only |

`POST /api/local-chat/sessions/{sid}/send` starts the turn and returns at once. The reply streams on `/api/ws` as
`chat_stream` frames (`chat_id` `localchat:<sid>`); a tool that needs a person arrives as a `local_chat_approval` frame
(decide it through `/api/approvals`); the end is a `local_chat_turn` frame (`done`, `cancelled` or `error`). `cancel` stops
it. A turn left running by a process that died is marked interrupted at the next start, never shown as complete.

## The model picker's catalog

`GET /api/local-chat/models` lists every model the hub can run, grouped `loaded`, `gpu`, `cpu`, `too_big`, `other`. Every
figure says where it came from:

- **metadata**: the model file (architecture, size, quantization, context, mixture of experts, vision)
- **planned**: the hub's `model.plan` against this machine's GPU (fits / fits after unloading another / does not fit, VRAM)
- **measured**: speed from real replies (llama.cpp's own `timings.predicted_per_second`, reported by the transport) and from
  benchmarks (`POST /api/local-chat/models/benchmark`); quality from ABP's eval reports (`docs/agents/eval-reports`)
- **estimated** / **rough_estimate**: speed where nothing was measured, from memory bandwidth / weights read per token,
  calibrated on the models measured on the same device (rough: nothing measured there yet)

Warnings: a chat model served with less context per request than `conductor.min_context`, and a model whose loading would
unload another.

## Config

```yaml
local_chat:
  instance_name: Local Chat
  default_model: ""        # "" = the loaded chat model, else the best measured local coder that fits the GPU
  default_mode: build
  default_autonomy: 2      # 0 Locked .. 5 Sovereign (applied to permissions in a later phase)
  cloud_assist: ask        # off | ask | auto
  catalog_ttl_s: 60
```
