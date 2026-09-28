"""Ollama, driven from ABP: find it, pull and load models and serve them to ABP's agents, build models from Modelfiles or
GGUF files, manage storage, and reach every other feature its API offers.

    client.py   finding Ollama (a configured provider whose address answers /api/version), HTTP, streamed progress, and
                a table of every route Ollama 0.34 serves (the parity layer; Ollama publishes no API description itself)
    harness.py  the everyday workflows: status, models and their capabilities, pull / push / copy / delete / create,
                importing a GGUF file, loading with a context that fits, unloading, moving old models into its folder
    tools.py    the agent's tools; anything that changes Ollama asks first

See docs/agents/ollama.md.
"""
