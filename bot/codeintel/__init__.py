"""Code intelligence: an index of a repository's symbols, references, imports and tests, and token-minimal tools on top.

Library use (everything is synchronous and thread-safe; call it with `asyncio.to_thread` from async code):

    from bot.codeintel import index_repo, outline, find_symbol, read_symbol, context_pack
    index_repo(root)                       # incremental; unchanged files are not re-read
    print(outline(root, "pkg/mod.py").render())

See docs/agents/codeintel.md.
"""

from bot.codeintel.indexer import update as index_repo
from bot.codeintel.tools import (CodeIntelError, Result, callers, context_pack, find_symbol, impact, outline, read_symbol,
                                 references, related_tests, stats, usage)

__all__ = ["index_repo", "outline", "find_symbol", "read_symbol", "references", "callers", "related_tests", "impact",
           "context_pack", "stats", "usage", "Result", "CodeIntelError"]
