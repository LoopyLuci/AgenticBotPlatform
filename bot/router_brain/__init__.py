"""The router's memory and judgement: what bot/model_router.py learns, and how a person teaches and edits it.

    store.py   one SQLite file (data/agent/router.db): every decision with its full reasoning, what happened next,
               per-model statistics, cooldowns, a log of everything the router learned, training examples, and each
               version of the policy
    policy.py  how the router thinks, as one editable, versioned document: score weights per task class, extra
               classification keywords, rules (pin, prefer, avoid, block), and the learning settings
    learn.py   learning from outcomes (reliability, speed, cooldowns after 429/403/404), from feedback (quality), and
               from training examples (a small classifier and model preferences)

Nothing here ever raises into a user's turn: a locked or broken router.db costs the learning, never the reply.
See docs/agents/router.md.
"""
