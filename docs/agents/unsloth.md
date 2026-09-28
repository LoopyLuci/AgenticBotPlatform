# Unsloth Studio from ABP

ABP drives [Unsloth Studio](https://unsloth.ai) running on the same machine. It can:

- put models on the GPU and serve them to your bots;
- download models from the Hugging Face hub;
- fine-tune, export, and reach every other feature Studio has.

The dashboard's **Unsloth** page and the agent's `unsloth_*` tools do all of this. You never need to open Studio's own
UI, though the page links to it.

## Setting it up

ABP finds Studio by itself. Any provider on the Models page whose address answers as Unsloth Studio is used, and that
provider's API key is Studio's key. A Hermes import sets this up as `unsloth` at `http://127.0.0.1:8888/v1`.

- If nothing is configured, ABP tries `http://127.0.0.1:8888`.
- The environment variables `ABP_UNSLOTH_URL` and `ABP_UNSLOTH_KEY` override both.
- If Studio refuses the key, make one in Studio under Settings > API and put it on the provider.

Two optional settings go in `config/backends.yaml`:

```yaml
unsloth:
  context_length: 32768          # the context a model is loaded with (Studio's own default can be 2048, far too small for an agent)
  models_dir: E:\AIModels\Unsloth   # where Studio downloads models; ABP can set it (Unsloth page > Overview)
```

## Using a Studio model in a bot

Studio serves only models it has loaded; ABP deals with that for you.

- **Load a model** on the Unsloth page (Models tab), or ask the agent to (`unsloth_load`). Any bot can then use it as
  `unsloth/<model>`, for example `unsloth/unsloth/Qwen3.5-9B-MTP-GGUF`.
- **Load on demand.** When a bot's model is not loaded, Studio answers "No model loaded". ABP then loads that model (for
  a GGUF repo, the quant already on disk) and asks again. Several turns asking at once share one load.
- **Automatic routing.** Bots on `auto` are offered only models Studio already has loaded, because loading another takes
  too long to do inside someone's turn. The model router learns their reliability and speed like any other model's.
  The same applies to other servers on this machine (Ollama, LM Studio): the router lists what they really serve,
  never the catalog's guesses.

## The Unsloth page

| Tab | What it does |
|---|---|
| Overview | Studio's address, GPU and VRAM in use, loaded models (unload), training state, and where models are downloaded (change it here) |
| Models | What Studio can serve (load with a chosen context, or unload); a repo's GGUF files with sizes, whether each is already downloaded, "Fits?" (Studio's memory estimate), and Download with live progress; Unsloth's recommended models |
| Train | Every training setting Studio offers, built from its own schema, with its defaults; only what you change is sent. Live status and a loss chart, Stop, and past runs |
| Export | Export the trained model as GGUF (with a quantization), LoRA adapter, merged or base model, to a folder or the Hugging Face hub; export status |
| All features | Every operation Studio describes in its own API (hundreds: chat, models, hub, training, export, datasets, data recipes, RAG, MCP, settings, audio, images, video...). Each gets a form built from its description. Reads run at once; anything that changes Studio asks you first and is written to the audit log. Uploads (datasets, documents, audio) take files from your computer. |

## The agent's tools

The tools are offered only while Studio is running. Anything that changes Studio asks for approval first.

| Tool | What it does |
|---|---|
| `unsloth_status` | GPU and VRAM, loaded models, training, backend, the models folder and its free space |
| `unsloth_models` | What Studio serves and has cached. With `repo_id`: that repo's GGUF files. With `recommended`: Unsloth's picks |
| `unsloth_load` | Load a model (variant, context, any other load setting), or unload it |
| `unsloth_download` | Download from the hub, optionally waiting until it finishes; or report progress |
| `unsloth_train` | Start (any training setting), status, metrics, stop, runs |
| `unsloth_export` | Export as gguf, lora, merged or base; or report status |
| `unsloth_operations` | Browse every Studio operation, and run the read-only ones |
| `unsloth_call` | Call any Studio operation, including uploads of files from the agent's working folder (never outside it) |

## How complete it is

ABP reads Studio's own API description (`/openapi.json`), so every operation Studio offers can be called from the
agent and from the page. That includes the ones ABP has no dedicated screen for, and features added in a newer Studio
release. The Overview, Models, Train and Export tabs are shortcuts for the everyday workflows.

What ABP does not copy is Studio's own chat UI. For chatting, point a bot at a loaded model and talk to it through any
ABP channel.

## Checked on real hardware (2026-09-28)

These were run against Unsloth Studio 2026.9 on an RX 7900 XTX (ROCm):

- **Download.** `unsloth/Qwen3.5-9B-MTP-GGUF` (Q4_K_M) was downloaded through ABP into `E:\AIModels\Unsloth` in about
  2 minutes.
- **Load.** It was loaded through ABP at 32,768 tokens of context in 24 s, using about 12 GB of VRAM.
- **Agent task.** An ABP agent on that model read a file with its tool and answered correctly in 12 s.
- **Load on demand.** The same task with the model unloaded took 20 s, including ABP loading it.
- **Upload.** A dataset file was uploaded through the bridge.

A 1.7B model loaded and answered but would not use its tools, which is expected at that size.
