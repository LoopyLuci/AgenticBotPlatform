"""The memory fabric: one memory and one conversation for every model ABP runs, so switching models never loses
what was learned or said.

    Memories        long-term facts. Shared ones (store.SHARED, instance 0 of bot/memory.py's table) reach every bot
                    and every model; a bot's own ones reach that bot. Same review gate, de-duplication and fading as
                    bot/memory.py, which owns them.
    Recall          the memories relevant to what is being asked, found by meaning (embeddings from ABP's local model
                    server, a local Ollama, or hashed TF-IDF when neither runs) on top of the most confirmed ones.
    Threads         every turn of every conversation, whichever backend answered, recorded at the router. When the
                    next turn goes to a different model, or to a backend that keeps no history of its own (a CLI,
                    Hermes, Kestrion, an external agent), it gets the conversation so far: a running summary plus the
                    latest turns, sized to that model's context window.
    Everywhere      the dashboard API (/api/memory), MCP tools, the agents' tools, and ABP's Ollama-compatible server
                    (a model named "<model>+memory", or the X-ABP-Memory header) so Kestrion, mesh-llm, openhuman,
                    9router or any Ollama/OpenAI client sees the same memories.

    store.py    tables, recall, threads, the context block       extract.py  memories proposed from a conversation
"""
