"""Unsloth Studio, driven from ABP: find it, load and serve its models to ABP's agents, train, export, and reach every
other feature it has.

    client.py   finding Studio (a configured provider whose address answers as "Unsloth UI Backend"), its key, HTTP, and
                its own OpenAPI description: every one of its operations, callable by id (the parity layer)
    harness.py  the workflows that matter day to day: status, models (on disk, cached, loaded), GGUF variants, download,
                memory estimate, load and unload, making sure a model is loaded before ABP talks to it, training, export
    tools.py    the agent's tools for all of the above; anything that changes Studio asks first

See docs/agents/unsloth.md.
"""
