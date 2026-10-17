# ModelMistress (local models)

ModelMistress is a separate program (an ABP module) that loads and serves local models behind one OpenAI-compatible API.
While its hub runs, ABP prefers it over Ollama and Unsloth Studio for local models. Without it nothing changes: Ollama,
Unsloth Studio and ABP's own local AI work as before (`bot/ollama`, `bot/unsloth` stay).

## Finding the hub

In this order: `ABP_MODELMISTRESS_URL` / `ABP_MODELMISTRESS_TOKEN`; `modelmistress.url` in the config (token from
`token_env`, or `token`); the module's control file (a hub started from the Modules page); then the hub's own home (`modelmistress.home`, `$MM_HOME`,
`%LOCALAPPDATA%/ModelMistress`, `~/.local/share/ModelMistress`), where the desktop app and a headless `model-mistress serve` write
theirs. Every candidate is checked with `GET /v1/health` (and its pid); stale files are skipped.

## Always on

With `modelmistress.autostart` (default true) ABP starts the hub at boot with `--home` set to that shared home (one hub and
model store for the desktop app, headless runs and ABP), reuses a healthy hub instead of starting a second, and a watchdog
restarts it with back-off (30 s doubling to 15 min). A sandboxed instance never starts it. `require()` tries one start before
failing. State: `GET /api/modelmistress/hub` (up, url, version, autostart, home, last restart). `ABP_NO_MODELMISTRESS=1` turns the
whole area off.

```yaml
modelmistress:
  url: http://127.0.0.1:8765        # optional: a hub that is not the module's own
  token_env: MODELMISTRESS_TOKEN
  autostart: true                   # start + keep the hub running
  home: ""                          # empty: the OS default home above
  allow_ollama: false               # true: keep Ollama in the default rotation while the hub runs
  allow_unsloth: false              # true: same for Unsloth Studio (and its auto-load)
  prefer: true                      # false: keep Ollama / Unsloth Studio even while the hub runs
  peers:                            # other machines' hubs, with the peer key their owner made for this machine
    bob: {url: "http://bob:8765", key_env: MM_BOB_KEY}
```

Ollama and Unsloth Studio are an opt-in fallback: with a hub up and the flags false they are off the default path (candidate
lists, auto routing, Studio auto-load); with no hub, or a flag true, they work exactly as before.

## What uses it

- **Chat.** The running hub is the provider `modelmistress`, so `modelmistress/<model>` works anywhere a provider model does.
- **Tools** (offered while a hub answers): `modelmistress_status`, `modelmistress_models`, `modelmistress_load` (or unload),
  `modelmistress_pull`. Loading and pulling ask first. `peer` reads a linked machine's model list.
- **Routes:** `/api/modelmistress/{status,models,jobs,pull,load,unload}`.
- **Auto routing** (`model_router.candidate_models`): ModelMistress's models come first; Ollama and Unsloth Studio providers are left
  out while it runs, because it serves their models itself (its catalog reads the Ollama store and the Hugging Face cache).
- **Local AI:** ABP's own model server is not auto-started while a hub serves; the page shows the hub.
- **Cluster `inference` jobs:** `spec.model`, `spec.messages`, optional `spec.hub` (a peer in `modelmistress.peers`). The node
  that runs the job uses its own hub, or the named peer's hub with the configured peer key; Ollama only when no hub answers.

## Fallback

A hub that is not running (or `prefer: false`) is simply not there: tools are not offered, the router keeps Ollama and Unsloth
Studio, inference jobs go to Ollama. A hub that answers with an error (unknown model, refused key) is reported as it is and not
retried elsewhere. A peer key reaches only the OpenAI routes (models, chat, completions, embeddings); pulling and loading work
on a hub of this machine only.

## Context window

For `modelmistress/<model>` the agent's context budget uses the window the model is loaded with, read from the hub (cached
30 s), not a family default; see "Context windows" in `models.md` for the order and fallbacks.
