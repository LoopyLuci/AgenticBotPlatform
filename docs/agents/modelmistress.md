# ModelMistress (local models)

ModelMistress is a separate program (an ABP module) that loads and serves local models behind one OpenAI-compatible API.
While its hub runs, ABP prefers it over Ollama and Unsloth Studio for local models. Without it nothing changes: Ollama,
Unsloth Studio and ABP's own local AI work as before (`bot/ollama`, `bot/unsloth` stay).

## Finding the hub

In this order: `ABP_MODELMISTRESS_URL` / `ABP_MODELMISTRESS_TOKEN`; `modelmistress.url` in the config (token from
`token_env`, or `token`); the module's control file (a hub started from the Modules page). `ABP_NO_MODELMISTRESS=1` turns the
whole area off.

```yaml
modelmistress:
  url: http://127.0.0.1:8765        # optional: a hub that is not the module's own
  token_env: MODELMISTRESS_TOKEN
  prefer: true                      # false: keep Ollama / Unsloth Studio even while the hub runs
  peers:                            # other machines' hubs, with the peer key their owner made for this machine
    bob: {url: "http://bob:8765", key_env: MM_BOB_KEY}
```

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
