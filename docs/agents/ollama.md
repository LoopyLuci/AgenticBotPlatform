# Ollama from ABP

ABP drives the [Ollama](https://ollama.com) running on this machine, from the dashboard's **Ollama** page and the agent's
`ollama_*` tools. It can:

- pull and load models and serve them to your bots;
- use Ollama's cloud models;
- build models from a Modelfile's parts or from a GGUF file;
- bring old models into Ollama's models folder;
- call every route Ollama serves.

## Setting it up

ABP finds Ollama by itself. Any provider on the Models page whose address answers Ollama's `/api/version` is used,
usually `http://127.0.0.1:11434/v1`. If nothing is configured, ABP tries `http://127.0.0.1:11434`, and the environment
variable `ABP_OLLAMA_URL` overrides both.

Two optional settings go in `config/backends.yaml`:

```yaml
ollama:
  context_length: 32768     # the context ABP loads models at, and gives its -abp serving models
```

Where Ollama keeps models is Ollama's own setting: `OLLAMA_MODELS`, or Ollama's Settings > Model location. The Overview
tab shows it and its free space. If older models are left in the previous folder (`~/.ollama/models`), **Move them
here** brings them across. Each file is copied and checked against its checksum before the old copy is removed, and
existing models are never overwritten.

## Using an Ollama model in a bot

Set a bot's model to `<provider>/<model>`, for example `ollama/qwen3.5:9b`, or leave it on `auto`: the model router
offers every model Ollama really has (and nothing it lacks), including `:cloud` models.

ABP also protects bots from Ollama's default context. Requests through Ollama's OpenAI-compatible endpoint carry no
context setting, so Ollama loads a model at the model's own `num_ctx` or, failing that, at `OLLAMA_CONTEXT_LENGTH`. On
this machine that is 262,144 tokens:

- `qwen3.5:9b` loaded that way took 15.3 GB of VRAM;
- a larger model would not fit at all.

When a model has no sane context of its own, ABP creates `<model>-abp` once. It is built on the model and shares its
files, so it takes no space, and its context is set to 32,768. Bots are served by it: the same model took 6.6 GB. The
router and the logs still name the model you chose.

## The Ollama page

| Tab | What it does |
|---|---|
| Overview | Version, your ollama.com account and plan, whether cloud models are on, loaded models (VRAM, context, unload), the models folder (and moving old models in), running jobs, the server's `OLLAMA_*` settings, and warnings about them |
| Models | Installed models (family, size, quantization, cloud). Load at a chosen context, unload, details (capabilities, native context, parameters, system prompt, Modelfile, template), copy, delete |
| Get models | Pull any model by name, with live progress; Ollama's own recommendations (local and cloud, with the plan each needs); import a `.gguf` file, including ones Unsloth Studio downloaded, without downloading it again |
| Create | A new model on top of an installed one, with your system prompt, parameters, template, example messages, license, and optional quantization: what a Modelfile does |
| All features | Every route Ollama serves, as a form: models, blobs, generate / chat / embed, the OpenAI-compatible API (chat, completions, embeddings, Responses), the Anthropic-compatible Messages API, tokenize, your account (me, sign out, keys) and web search / fetch. Anything that changes Ollama asks first and is audited |

## The agent's tools

The tools are offered only while Ollama is running. Anything that changes Ollama asks for approval first.

| Tool | What it does |
|---|---|
| `ollama_status` | Version, account and plan, loaded models, storage, warnings, running jobs |
| `ollama_models` | Installed models. With `model`: its capabilities, context, parameters and Modelfile. With `recommended`: Ollama's picks |
| `ollama_pull` | Pull (in the background, or wait), or push to ollama.com |
| `ollama_load` | Load at a context that fits, with keep-alive and options, or unload |
| `ollama_manage` | Create, import a GGUF file, copy, delete, move old models in |
| `ollama_operations` | Browse every route, and run the read-only ones (show, chat, embed, web search, tokenize...) |
| `ollama_call` | Call any route by id or `METHOD /path` |

## How complete it is

Ollama publishes no machine-readable API description. ABP's table of routes (`bot/ollama/client.py`) was taken from
Ollama 0.34.4's own binary and its API documentation, and every one of them can be called. A route added in a later
Ollama release is still callable as `METHOD /path` before the table learns its name.

What ABP does not copy is Ollama's own interactive CLI and chat window. For chatting, point a bot at the model and talk
to it through any ABP channel.

## Checked on this machine (2026-09-28, Ollama 0.34.4, RX 7900 XTX)

- **Moving old models.** Six older models (6 GB, 18 files) were moved from `C:\Users\limpi\.ollama\models` into
  `E:\AIModels\Ollama\Models`, each file verified, and Ollama listed them again.
- **Pulling.** `gemma4:31b-cloud` was registered, `qwen3.5:9b` (6.6 GB) was pulled in 140 s, and `gemma4:26b` (17 GB)
  was pulled too, all through ABP.
- **Agent tasks.** An ABP agent on `gemma4:31b-cloud` read a file with its tool and answered correctly in 7 s. On
  `qwen3.5:9b`, served as `qwen3.5-abp:9b` at 32,768 context, the same task took 30 s including the load.
