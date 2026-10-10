"""ModelMistress, driven from ABP: find its hub (this machine's, a configured one, or a linked peer's), list, pull, load and
unload models, chat through its OpenAI-compatible API, and prefer it over Ollama / Unsloth Studio for local models while
it runs. ABP works the same on a machine without it: every caller falls back to the old paths.

    client.py   finding the hub (env, config, the module's control file), calls to its operations and /v1 routes
    harness.py  the everyday workflows: status, models, load / unload, pull, jobs
    tools.py    the agent's tools; anything that changes the hub asks first

Configuration (config/backends.yaml, all optional):

    modelmistress:
      url: http://127.0.0.1:8765         # a hub that is not the module's own (token: token_env, or token)
      token_env: MODELMISTRESS_TOKEN
      prefer: true                       # false: keep using Ollama / Unsloth Studio even while the hub runs
      peers:                             # other machines' hubs, reached with the key that machine's owner made for this one
        bob: {url: "http://bob:8765", key_env: MM_BOB_KEY}

See docs/agents/modelmistress.md.
"""
